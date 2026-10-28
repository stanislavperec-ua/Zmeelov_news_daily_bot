import os
import re
import html
import math
import time
import requests
import trafilatura
from groq import Groq
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from collections import Counter

# ── Модель Groq. При следующей замене менять только эту строку. ──
# Актуальные модели: https://console.groq.com/docs/models
GROQ_MODEL = "openai/gpt-oss-120b"

# Сколько запасных статей запрашивать сверх нужного количества:
# если анализ одной статьи не удался, берём следующую из запаса.
RESERVE = 3

# Сколько новостей в каждом блоке про искусственный интеллект
AI_COUNT = 4

GROQ_KEY   = os.environ["GROQ_API_KEY"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY   = os.environ["NEWS_API_KEY"]
MY_CHAT_ID = os.environ.get("MY_CHAT_ID", "")

# ── Тестовый режим: выпуск уходит в личку, состояние не трогается ──
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() in ("1", "true", "yes")

if TEST_MODE:
    if not MY_CHAT_ID:
        print("TEST_MODE включён, но MY_CHAT_ID не задан. Останавливаюсь.")
        exit(1)
    TARGET_CHAT_ID = MY_CHAT_ID
else:
    TARGET_CHAT_ID = TG_CHAT_ID

PAUSE_BETWEEN = 3 if TEST_MODE else 60

# ── Время: всегда киевское, летнее/зимнее учитывается автоматически ──
KYIV_TZ   = ZoneInfo("Europe/Kyiv")
now_utc   = datetime.now(timezone.utc)
now_kyiv  = now_utc.astimezone(KYIV_TZ)
kyiv_hour = now_kyiv.hour

if 5 <= kyiv_hour < 10:
    BLOCK = "morning"
elif 10 <= kyiv_hour < 12:
    BLOCK = "ai_morning"
elif 12 <= kyiv_hour < 16:
    BLOCK = "midday"
elif 16 <= kyiv_hour < 19:
    BLOCK = "evening"
elif 19 <= kyiv_hour < 23:
    BLOCK = "ai_evening"
else:
    BLOCK = "morning"

# In test mode a block can be forced, to check e.g. the AI block in the morning.
# Only in test mode: a production run always takes the block from the clock.
TEST_BLOCK = os.environ.get("TEST_BLOCK", "").strip()
if TEST_MODE and TEST_BLOCK in ("morning", "ai_morning", "midday", "evening", "ai_evening"):
    BLOCK = TEST_BLOCK

print(f"UTC: {now_utc.hour}, Киев: {kyiv_hour}, блок: {BLOCK}, тест: {TEST_MODE}")

LAST_RUN_FILE = "last_run.txt"
current_run_key = f"{now_kyiv.strftime('%Y-%m-%d')}-{BLOCK}"

if not TEST_MODE:
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE, "r") as f:
            last_run = f.read().strip()
        if last_run == current_run_key:
            print(f"Блок {BLOCK} уже выполнялся сегодня, пропускаю.")
            exit(0)

    with open(LAST_RUN_FILE, "w") as f:
        f.write(current_run_key)

today_str = now_kyiv.strftime("%d.%m.%Y")
date_from = (now_utc - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

client = Groq(api_key=GROQ_KEY)

# ── Источники: сравнение по домену из URL, а не по подстроке в названии ──
TRUSTED_DOMAINS = {
    "reuters.com", "bbc.com", "bbc.co.uk", "apnews.com", "bloomberg.com",
    "theguardian.com", "nytimes.com", "washingtonpost.com", "wsj.com",
    "ft.com", "aljazeera.com", "cnn.com", "nbcnews.com", "abcnews.go.com",
    "cbsnews.com", "npr.org", "economist.com", "time.com", "newsweek.com",
    "foreignpolicy.com", "politico.com", "politico.eu", "axios.com",
    "thehill.com", "theatlantic.com", "wired.com", "techcrunch.com",
    "theverge.com", "arstechnica.com", "technologyreview.com",
    "science.org", "nature.com", "newscientist.com",
    "kyivindependent.com", "ukrinform.net", "ukrinform.ua", "pravda.com.ua",
    "freep.com", "irishtimes.com", "globalsecurity.org",
    "lemonde.fr", "spiegel.de", "elpais.com",
    "dw.com", "france24.com", "euronews.com", "cnbc.com",
    "tomshardware.com", "fortune.com", "zdnet.com", "engadget.com",
    "venturebeat.com", "cnet.com", "businessinsider.com", "theconversation.com",
}

BLOCKED_DOMAINS = {
    "naturalnews.com", "breitbart.com", "infowars.com", "dailywire.com",
    "thegatewaypundit.com", "zerohedge.com",
    "rt.com", "sputnikglobe.com", "sputniknews.com", "tass.com", "tass.ru",
    "ria.ru", "pravda.ru", "lenta.ru", "gazeta.ru", "iz.ru", "kp.ru",
    "mk.ru", "vesti.ru", "smotrim.ru", "1tv.ru", "rbc.ru",
    # пресс-релизные и биржевые мельницы, таблоиды
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "marketbeat.com", "dailymail.co.uk", "dailymail.com",
    "nypost.com", "thesun.co.uk", "mirror.co.uk", "express.co.uk",
    "dailystar.co.uk", "metro.co.uk",
    # агрегаторы: перепечатывают чужое куском, полного текста у них нет
    "biztoc.com", "msn.com", "news.google.com", "yahoo.com", "flipboard.com",
    "newsbreak.com", "smartnews.com",
    # RT mirrors outside the .ru zone
    "rt-english.com", "swentr.site", "actualidad-rt.com", "rt.rs",
    # scientific papers and preprints: not news
    "journals.plos.org", "arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com",
}

# Пресс-релизы, публикуемые на доверенных доменах, отсекаем по адресу страницы
PR_URL_PARTS = ["/press-release", "/pressrelease", "/earnings-call", "/press_release"]

# Ленты живых обновлений. На одной такой странице десятки не связанных между
# собой новостей, поэтому в текст попадают посторонние события, а заголовок и
# фото относятся только к верхнему обновлению.
LIVE_URL_PARTS = [
    "/liveblog/", "/newsblogs/", "/live-updates/", "/live-news/",
    "/live-blog/", "/liveupdates/", "/live/",
]

LIVE_TITLE_PARTS = [
    "live updates", "live blog", "liveblog", "breaking news live",
    "live news", "as it happened", "latest updates", "live: ",
]

BLOCKED_NAME_PARTS = ["sputnik", "tass", "ria novosti", "russia today"]

# RT publishes through mirror domains (rt-english.com got into the channel
# on 27.10.2026), so whole Russian and Belarusian zones are blocked too
BLOCKED_TLDS = (".ru", ".su", ".by")
BLOCKED_EXACT_NAMES = {"rt", "rt.com", "rt english", "rt news"}

# Not news: job ads, event calendars, scientific papers, forum posts
LISTING_URL_PARTS = ["/vacancies/", "/vacancy/", "/careers/", "/calendar/"]
NON_NEWS_TITLE_PREFIXES = ("show hn", "ask hn", "tell hn")

# Украинские издания читаются напрямую через RSS. Через NewsAPI их не достать:
# параметр domains на нашем плане молча возвращает ноль статей по любому
# домену, а поиска по слову Ukraine в мировой прессе не хватает, украинский
# блок оставался полупустым.
UA_FEEDS = [
    ("https://www.ukrinform.net/rss/block-lastnews", "Ukrinform"),
    # Военная рубрика: именно в ней чаще всего попадаются харьковские сюжеты
    ("https://www.ukrinform.net/rss/rubric-ato", "Ukrinform"),
    ("https://euromaidanpress.com/feed/", "Euromaidan Press"),
]

# Quality AI and tech outlets, read directly. Through NewsAPI almost every AI
# story came from second-tier sites (pymnts, digitaljournal): the trusted
# outlets rarely show up in its search results for AI topics.
AI_FEEDS = [
    ("https://www.theguardian.com/technology/artificialintelligenceai/rss", "The Guardian"),
    ("https://www.wired.com/feed/tag/ai/latest/rss", "Wired"),
    ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch"),
    ("https://arstechnica.com/ai/feed/", "Ars Technica"),
    ("https://www.technologyreview.com/topic/artificial-intelligence/feed", "MIT Technology Review"),
    ("https://feeds.bbci.co.uk/news/technology/rss.xml", "BBC News"),
    ("https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss", "IEEE Spectrum"),
]

# ── Спорт и развлечения отсекаем по словам, доменам и разделам сайтов ──
EXCLUDE_KEYWORDS = [
    "wwe", "nfl", "nba", "spoiler", "wrestling", "celebrity",
    "kardashian", "taylor swift", "oscar", "grammy", "box office",
    "recipe", "horoscope", "zodiac",
    "boxing", "fight night", "match report", "premier league",
    "champions league", "world cup qualifier", "playoff", "season finale",
    "grand slam", "ufc", "mlb", "nhl", "touchdown", "league cup",
    "serie a", "la liga", "bundesliga", "transfer window", "cricket",
    "rugby", "vs.",
]

SPORT_DOMAINS = {
    "espn.com", "sports.yahoo.com", "skysports.com", "bleacherreport.com",
    "cbssports.com", "goal.com", "marca.com", "si.com",
}

SPORT_URL_PARTS = ["/sport/", "/sports/"]

SENT_URLS_FILE = "sent_urls.txt"
SENT_URLS_KEEP = 300
LOG_FILE       = "log.txt"
LOG_KEEP       = 3000


def log(msg):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def esc(text):
    """Экранирование для parse_mode=HTML"""
    return html.escape(str(text), quote=False)


def get_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def domain_in(domain, domain_set):
    return any(domain == d or domain.endswith("." + d) for d in domain_set)


def load_sent_urls():
    if not os.path.exists(SENT_URLS_FILE):
        return set()
    with open(SENT_URLS_FILE, "r") as f:
        urls = [line.strip() for line in f if line.strip()]
    log(f"Загружено {len(urls)} уже отправленных новостей")
    return set(urls)


def save_sent_url(url, sent_urls):
    sent_urls.add(url)
    if TEST_MODE:
        return
    with open(SENT_URLS_FILE, "a") as f:
        f.write(url + "\n")


def trim_log():
    """Держим в логе последние LOG_KEEP строк: файл коммитится в репозиторий
    и без обрезки растёт без предела"""
    if TEST_MODE or not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) > LOG_KEEP:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-LOG_KEEP:])
            print(f"log.txt обрезан: {len(lines)} -> {LOG_KEEP}")
    except Exception as e:
        print(f"Не удалось обрезать log.txt: {e}")


def trim_sent_urls():
    """Держим в файле только последние SENT_URLS_KEEP строк, с сохранением порядка"""
    if TEST_MODE or not os.path.exists(SENT_URLS_FILE):
        return
    with open(SENT_URLS_FILE, "r") as f:
        lines = [line.strip() for line in f if line.strip()]
    if len(lines) > SENT_URLS_KEEP:
        with open(SENT_URLS_FILE, "w") as f:
            f.write("\n".join(lines[-SENT_URLS_KEEP:]) + "\n")
        log(f"sent_urls.txt обрезан: {len(lines)} -> {SENT_URLS_KEEP}")


sent_urls = load_sent_urls()


# ── Story memory: catches the same event published from another outlet ──
# URLs alone are not enough: the same event comes from NPR today and AP
# tomorrow, or as a follow-up article from the same outlet under a new URL.
SENT_STORIES_FILE  = "sent_stories.txt"
STORY_MEMORY_HOURS = 96
STORY_SIMILARITY   = 0.55   # shared part vs the shorter headline
STORY_SIMILARITY_LONG = 0.4 # shared part vs the longer headline

TITLE_STOPWORDS = set("""
the a an of to in on for and or with at by from as is are was were be been being
after over into its his her their our your new says said say will would could
should can may might has have had than this that these those about amid against
between during under up down out off why how what who when where which while not
no now more most first last also just still back report reports via inside
one two three four five six seven eight nine ten
""".split())

NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

# Two headlines naming different Ukrainian cities describe different events,
# even when the wording is identical: "Russian strike on Kharkiv / on Kyiv".
UA_PLACES = {
    "kharkiv", "kharkov", "kyiv", "kiev", "odesa", "odessa", "dnipro", "lviv",
    "zaporizhzhia", "kherson", "sumy", "mykolaiv", "poltava", "chernihiv",
    "donetsk", "luhansk", "kramatorsk", "sloviansk", "pokrovsk", "kupiansk",
    "izium", "khmelnytskyi", "vinnytsia", "zhytomyr", "rivne", "lutsk",
    "ternopil", "uzhhorod", "chernivtsi", "kropyvnytskyi", "cherkasy",
    "kryvyi", "nikopol", "bucha", "irpin", "bakhmut", "avdiivka",
}

_title_df = Counter()
_title_seen = set()


def title_tokens(title):
    """Meaningful words of a headline, with plural endings folded"""
    t = (title or "").lower().replace("’", "'")
    t = re.sub(r"'s\b", "", t)
    words = set()
    for w in re.sub(r"[^a-z0-9\s]", " ", t).split():
        if w in TITLE_STOPWORDS or len(w) < 3:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.add(w)
    return words


def title_numbers(title):
    """Numbers in a headline, digits and words alike: "kills two" == "kills 2" """
    t = (title or "").lower()
    found = set(re.findall(r"\b\d+\b", t))
    for word, digit in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", t):
            found.add(digit)
    return found


def learn_titles(titles):
    """Feed headlines into word statistics: words seen in many headlines
    (russian, attack, says) weigh less than rare ones (shamonda, alberta)"""
    for t in titles:
        key = (t or "").strip().lower()
        if key and key not in _title_seen:
            _title_seen.add(key)
            _title_df.update(title_tokens(t))


def word_weight(word):
    return math.log((len(_title_seen) + 1) / (_title_df.get(word, 0) + 1)) + 1


def is_same_story(title_a, title_b):
    ta, tb = title_tokens(title_a), title_tokens(title_b)
    if len(ta) < 3 or len(tb) < 3:
        return False
    shared = ta & tb
    if len(shared) < 3:
        return False
    na, nb = title_numbers(title_a), title_numbers(title_b)
    if na and nb and na != nb:
        return False
    pa, pb = ta & UA_PLACES, tb & UA_PLACES
    if pa and pb and pa.isdisjoint(pb):
        return False
    weight_a = sum(word_weight(w) for w in ta)
    weight_b = sum(word_weight(w) for w in tb)
    weight_shared = sum(word_weight(w) for w in shared)
    # Both conditions: a short headline of generic words ("Air defenses
    # neutralize 71 Russian drones") is fully covered by many long ones,
    # so the shared part must also be a real share of the longer headline
    return (weight_shared / min(weight_a, weight_b) >= STORY_SIMILARITY
            and weight_shared / max(weight_a, weight_b) >= STORY_SIMILARITY_LONG)


def load_story_memory():
    """Headlines published in the last STORY_MEMORY_HOURS, as (time, title)"""
    memory = []
    if not os.path.exists(SENT_STORIES_FILE):
        return memory
    cutoff = now_utc - timedelta(hours=STORY_MEMORY_HOURS)
    with open(SENT_STORIES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            stamp, _, title = line.rstrip("\r\n").partition("\t")
            when = parse_published(stamp)
            if when and when >= cutoff and title:
                memory.append((when, title))
    learn_titles(t for _, t in memory)
    log(f"Story memory: {len(memory)} headlines from the last {STORY_MEMORY_HOURS} h")
    return memory


def remember_story(title):
    story_memory.append((now_utc, title))
    learn_titles([title])
    if TEST_MODE:
        return
    # A failed write must not stop the run: the news is already published,
    # and the state commit after the run still has to save sent_urls.txt
    try:
        with open(SENT_STORIES_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\t{title}\n")
    except Exception as e:
        log(f"Could not save story memory: {e}")


def trim_story_memory():
    """Rewrite the file keeping only entries inside the memory window"""
    if TEST_MODE:
        return
    cutoff = now_utc - timedelta(hours=STORY_MEMORY_HOURS)
    try:
        with open(SENT_STORIES_FILE, "w", encoding="utf-8") as f:
            for when, title in story_memory:
                if when >= cutoff:
                    f.write(f"{when.strftime('%Y-%m-%dT%H:%M:%SZ')}\t{title}\n")
    except Exception as e:
        log(f"Could not trim story memory: {e}")


def recent_story_titles(hours=48, limit=40):
    cutoff = now_utc - timedelta(hours=hours)
    return [t for when, t in story_memory if when >= cutoff][-limit:]


def tg_send(chat_id, text):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4096],
                "parse_mode": "HTML"
            },
            timeout=15
        )
        if resp.status_code == 200:
            return True
        log(f"Текст не отправился: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"Ошибка отправки текста: {e}")
        return False


def tg_text(text):
    return tg_send(TARGET_CHAT_ID, text)


def tg_notify_me(text):
    if MY_CHAT_ID:
        tg_send(MY_CHAT_ID, text)


def tg_message_with_preview(text, image_url):
    """Основной формат: текст до 4096 символов, фото крупным превью над текстом"""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TARGET_CHAT_ID,
                "text": text[:4096],
                "parse_mode": "HTML",
                "link_preview_options": {
                    "url": image_url,
                    "prefer_large_media": True,
                    "show_above_text": True
                }
            },
            timeout=15
        )
        if resp.status_code == 200:
            return True
        log(f"Сообщение с превью не отправилось: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"Ошибка отправки сообщения с превью: {e}")
        return False


def tg_photo_with_caption(image_url, caption):
    """Запасной формат: фото с подписью, лимит 1024 символа"""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            json={
                "chat_id": TARGET_CHAT_ID,
                "photo": image_url,
                "caption": caption[:1024],
                "parse_mode": "HTML"
            },
            timeout=15
        )
        if resp.status_code == 200:
            return True
        log(f"Фото не отправилось: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"Ошибка отправки фото: {e}")
        return False


def parse_published(published):
    try:
        return datetime.fromisoformat(published.replace("Z", "+00:00"))
    except Exception:
        return None


def is_fresh(article):
    published = article.get("publishedAt", "")
    if not published:
        return False
    pub_date = parse_published(published)
    if pub_date is None:
        return True
    age = now_utc - pub_date
    if age.total_seconds() > 48 * 3600:
        log(f"Старая новость ({published}): {article.get('title', '')[:40]}")
        return False
    return True


def is_blocked_source(article):
    source_name = (article.get("source", {}).get("name") or "").lower()
    domain = get_domain(article.get("url") or "")
    if domain_in(domain, BLOCKED_DOMAINS):
        log(f"Заблокированный источник ({domain}): {article.get('title', '')[:40]}")
        return True
    for part in BLOCKED_NAME_PARTS:
        if part in source_name:
            log(f"Заблокированный источник ({source_name}): {article.get('title', '')[:40]}")
            return True
    if domain.endswith(BLOCKED_TLDS) or source_name.strip() in BLOCKED_EXACT_NAMES:
        log(f"Blocked source ({domain or source_name}): {article.get('title', '')[:40]}")
        return True
    return False


def is_mostly_latin(text):
    """NewsAPI's language=en filter leaks other scripts: a Japanese page got
    into the Ukrainian block on 26.10.2026"""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.8


def is_not_news(article):
    url = (article.get("url") or "").lower()
    title = (article.get("title") or "").lower().strip()
    if any(part in url for part in LISTING_URL_PARTS):
        return True
    return title.startswith(NON_NEWS_TITLE_PREFIXES)


def is_trusted_source(article):
    if is_blocked_source(article):
        return False
    domain = get_domain(article.get("url") or "")
    if domain_in(domain, TRUSTED_DOMAINS):
        return True
    log(f"Неизвестный источник ({domain}): {article.get('title', '')[:40]}")
    return False


AI_TERMS = [
    "artificial intelligence", "machine learning", "neural network",
    "deep learning", "generative ai", "large language model",
    "chatgpt", "openai", "anthropic", "deepmind", "midjourney", "copilot",
    "chatbot", "robotics", "humanoid robot", "ai model", "ai agent",
    "ai chip", "ai startup", "ai tool",
]


def ai_mentions(text):
    """Считаем признаки новости про искусственный интеллект. Отдельное слово
    ai ищем строго по границам: иначе оно находится внутри said, again,
    maintain и даже Ukraine."""
    count = sum(text.count(term) for term in AI_TERMS)
    count += len(re.findall(r"\bai\b", text))
    return count


def ukraine_mentions(text):
    """Считаем признаки украинской новости. Одного слова Ukraine мало:
    статьи про Киев и Зеленского тоже про Украину."""
    return (text.count("ukraine") + text.count("ukrainian")
            + text.count("kyiv") + text.count("zelensky"))


def is_liveblog(article):
    url = (article.get("url") or "").lower()
    title = (article.get("title") or "").lower()
    for part in LIVE_URL_PARTS:
        if part in url:
            return True
    for part in LIVE_TITLE_PARTS:
        if part in title:
            return True
    return False


def is_sport(article):
    url = (article.get("url") or "").lower()
    domain = get_domain(url)
    if domain_in(domain, SPORT_DOMAINS):
        return True
    for part in SPORT_URL_PARTS:
        if part in url:
            return True
    return False


def is_duplicate_by_title(title):
    """Same event already published within the memory window"""
    for _, old_title in story_memory:
        if is_same_story(title, old_title):
            log(f"Same story already published: {title[:50]} ~ {old_title[:50]}")
            return True
    return False


def deduplicate_articles(articles):
    """Keep one article per event inside a single candidate list"""
    learn_titles(a.get("title", "") for a in articles)
    unique = []
    for article in articles:
        title = article.get("title", "")
        if any(is_same_story(title, u.get("title", "")) for u in unique):
            log(f"Same story twice in candidates: {title[:50]}")
            continue
        unique.append(article)
    return unique


def is_relevant(article, require_ukraine=False, require_kharkiv=False,
                require_ai=False, skip_source_check=False):
    title = (article.get("title") or "").lower()
    description = (article.get("description") or "").lower()
    text = title + " " + description

    if article.get("title") == "[Removed]":
        return False
    if article.get("description") == "[Removed]":
        return False
    if not article.get("description"):
        return False

    if not is_mostly_latin(article.get("title") or ""):
        log(f"Not an English headline, skipping: {article.get('title', '')[:40]}")
        return False

    if is_not_news(article):
        log(f"Not a news item (listing, forum post), skipping: {article.get('title', '')[:40]}")
        return False

    if not article.get("urlToImage"):
        log(f"Нет фото: {article.get('title', '')[:40]}")
        return False

    if not is_fresh(article):
        return False

    if skip_source_check:
        if is_blocked_source(article):
            return False
    else:
        if not is_trusted_source(article):
            return False

    if is_liveblog(article):
        log(f"Лента обновлений, пропускаю: {article.get('title', '')[:40]}")
        return False

    if is_sport(article):
        log(f"Спорт, пропускаю: {article.get('title', '')[:40]}")
        return False

    url_lower = (article.get("url") or "").lower()
    for part in PR_URL_PARTS:
        if part in url_lower:
            log(f"Пресс-релиз, пропускаю: {article.get('title', '')[:40]}")
            return False

    for word in EXCLUDE_KEYWORDS:
        if word in text:
            return False

    if require_ukraine and ukraine_mentions(text) < 2:
        return False

    if require_ai and ai_mentions(text) < 2:
        log(f"Не про ИИ, пропускаю: {article.get('title', '')[:50]}")
        return False

    if require_kharkiv and "kharkiv" not in text and "kharkov" not in text:
        return False

    url = article.get("url", "")
    if url in sent_urls:
        log(f"Пропускаю уже отправленную: {article.get('title', '')[:50]}")
        return False

    if is_duplicate_by_title(article.get("title", "")):
        return False

    return True


def looks_like_feed(text):
    """Лента обновлений выдаёт себя частыми метками времени между записями"""
    stamps = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\b", text)
    return len(stamps) >= 6


def text_matches_title(text, title):
    """Проверяем, что скачанный текст действительно про эту новость.
    Если совпадений мало, страница отдала не то, что обещал заголовок."""
    words = set(re.findall(r"[a-zA-Z]{5,}", title.lower()))
    if len(words) < 3:
        return True
    head = text[:2000].lower()
    hits = sum(1 for w in words if w in head)
    return hits / len(words) >= 0.4


def fetch_article_text(url, title=""):
    """Скачиваем полный текст статьи. Если не получилось или текст не про эту
    новость, вернём None и анализ пойдёт по краткому описанию из NewsAPI."""
    try:
        resp = requests.get(
            url,
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0 Safari/537.36"
            }
        )
        if resp.status_code != 200:
            return None
        text = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
        if not text or len(text) <= 300:
            return None
        text = text[:3000]

        if looks_like_feed(text):
            log(f"Текст похож на ленту обновлений, беру описание: {url[:60]}")
            return None

        if title and not text_matches_title(text, title):
            log(f"Текст не совпадает с заголовком, беру описание: {url[:60]}")
            return None

        return text
    except Exception as e:
        log(f"Полный текст не скачался ({e.__class__.__name__}): {url[:60]}")
        return None


def clean_model_output(text):
    """Убираем markdown-разметку, которую модель может добавить вопреки промпту"""
    text = text.replace("**", "").replace("###", "").replace("##", "")
    return text.strip()


def select_top_articles(articles, count, theme):
    """Просим модель выбрать самые значимые новости из кандидатов.
    Возвращаем переупорядоченный список: выбранные первыми, остальные запасом.
    При любой ошибке возвращаем исходный порядок.

    Важно: count это сколько новостей реально пойдёт в выпуск, без запаса.
    Раньше сюда передавали количество с запасом, и модель получала
    бессмысленную задачу вида "выбери 7 лучших из 9", после чего отвечала
    невнятно, номера не разбирались, и отбор молча не работал."""
    count = max(1, count)
    if len(articles) <= count:
        return articles

    listing = []
    for i, a in enumerate(articles[:20]):
        title = a.get("title", "")
        source = a.get("source", {}).get("name", "")
        desc = (a.get("description") or "")[:120]
        listing.append(f"{i + 1}. {title} ({source}). {desc}")

    # Semantic repeat guard: the model sees what was already published and
    # catches the same event told in different words by another outlet
    recent = recent_story_titles()
    recent_part = ""
    if recent:
        recent_part = (
            "\nЭти события уже опубликованы в канале за последние двое суток:\n"
            + "\n".join(f"- {t}" for t in recent)
            + "\n\nНе выбирай кандидатов об этих же событиях, даже если заголовок "
            "сформулирован иначе или это другое издание. Исключение: существенно "
            "новый поворот (новые жертвы, решение суда, отставка, итог голосования).\n"
        )

    prompt = f"""Ты выпускающий редактор новостного канала. Тема выпуска: {theme}.
Вот список свежих новостей-кандидатов:

{chr(10).join(listing)}
{recent_part}
Сначала отбрось всё, что не относится к теме выпуска, даже если новость важная
сама по себе. Из оставшихся выбери {count} самых важных и общественно значимых.
Критерии: масштаб события, влияние на людей и страны, новизна. Отсеивай
кликбейт, мелкие происшествия, пресс-релизы компаний, биржевые отчёты,
крипто-прогнозы, рекламные и проходные материалы. Отдельно отсеивай светскую
хронику и курьёзы: истории про домашних животных, личную жизнь, наряды,
милые случаи с политиками. Это новостной канал, а не развлекательный.
Не выбирай несколько новостей об одном и том же событии: одно событие, одна
новость, даже если издания рассказывают о нём с разных сторон. Отдавай
предпочтение новостям, а не колонкам мнений, рецензиям и обзорам.

Ответь ТОЛЬКО номерами выбранных новостей через запятую, по убыванию важности.
Например: 3, 1, 7, 5"""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            # The model reasons before answering and the reasoning shares this
            # limit: at 1000 tokens a long candidate list left an empty answer
            max_completion_tokens=3000,
            temperature=0.2
        )
        raw = (response.choices[0].message.content or "").strip()
        numbers = [int(n) for n in re.findall(r"\d+", raw)]
        chosen_idx = [n - 1 for n in numbers if 1 <= n <= len(articles)]
        # убираем повторы, сохраняя порядок
        seen = set()
        chosen_idx = [i for i in chosen_idx if not (i in seen or seen.add(i))]
        if not chosen_idx:
            preview = " ".join(raw.split())[:200]
            log(f"Отбор не дал номеров, беру по порядку. Ответ модели: {preview}")
            return articles
        chosen = [articles[i] for i in chosen_idx]
        rest = [a for i, a in enumerate(articles) if i not in set(chosen_idx)]
        log(f"Редакторский отбор: выбраны позиции {[i + 1 for i in chosen_idx[:count]]}")
        return chosen + rest
    except Exception as e:
        log(f"Отбор по важности не удался, беру по порядку: {str(e)[:100]}")
        return articles


def analyze(title, description, source_name, published_at=None, article_url=None):
    """Возвращает (заголовок_ру, текст) или None, если анализ не удался.
    При None статья не публикуется и не считается использованной."""
    date_str = today_str
    if published_at:
        pub_date = parse_published(published_at)
        if pub_date:
            date_str = pub_date.astimezone(KYIV_TZ).strftime("%d.%m.%Y")

    full_text = fetch_article_text(article_url, title) if article_url else None

    if full_text:
        material = (
            f"Полный текст статьи (может быть обрезан):\n{full_text}\n\n"
            "ВАЖНО: пиши строго про то событие, которое названо в заголовке. "
            "Если в тексте попались посторонние события, не относящиеся к "
            "заголовку, полностью игнорируй их."
        )
        size_rule = ("Суть: начни с даты \"{d}.\" затем напиши 7-9 содержательных предложений, "
                     "которые полностью раскрывают новость по фактам из статьи.").format(d=date_str)
        limit_rule = "Весь ответ не длиннее 2200 символов."
        log(f"Полный текст получен ({len(full_text)} символов)")
    else:
        material = f"Описание: {description}"
        size_rule = ("Суть: начни с даты \"{d}.\" затем напиши 5-6 содержательных предложений "
                     "строго по фактам из описания, без воды и домыслов.").format(d=date_str)
        limit_rule = "Весь ответ не длиннее 1200 символов."

    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
{material}
Источник: {source_name}
Дата публикации: {date_str}

Напиши ответ на русском языке строго в таком формате: три блока, разделённые пустой строкой.

Первая строка: литературный перевод заголовка на русский язык. Передавай смысл точно и красиво, избегай дословного перевода если он звучит неестественно. Заголовок должен читаться как заголовок качественного русскоязычного издания. Без кавычек, без слова "Заголовок".

{size_rule} Указывай конкретные имена людей, названия стран, организаций, цифры и факты. Пиши живым литературным языком как журналист качественного издания. Не домысливай: только то, что есть в материале.

Прогноз: напиши 2-3 конкретных и обоснованных предложения о возможных последствиях этого события для стран, людей, рынков или политики. Прогноз должен быть логически связан с фактами и звучать профессионально.

{limit_rule} Никаких звёздочек и никакой разметки. Тире не использовать, вместо него запятая или двоеточие.

Соблюдай точный шаблон ответа, слова "Суть:" и "Прогноз:" обязательны:

Литературный заголовок новости

Суть: {date_str}. Первое предложение. Второе предложение.

Прогноз: Первое предложение. Второе предложение."""

    for attempt in range(1, 4):
        try:
            log(f"Попытка {attempt} для: {title[:40]}")
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=2500,
                temperature=0.6
            )
            raw = (response.choices[0].message.content or "").strip()
            raw = clean_model_output(raw)

            lines = [l.strip() for l in raw.split("\n") if l.strip()]

            # Требуем только содержательную часть. Метку "Суть:" модель иногда
            # опускает, хотя текст правильный, и из-за этого терялись готовые
            # новости, в том числе харьковские.
            if len(raw) < 200 or "Прогноз" not in raw or len(lines) < 3:
                preview = " ".join(raw.split())[:200]
                log(f"Ответ модели без нужной структуры ({len(raw)} символов): {preview}")
                time.sleep(5)
                continue

            title_ru = re.sub(r"^(Заголовок|Первая строка)\s*:\s*", "", lines[0]).strip()
            body_lines = lines[1:]
            if body_lines and not body_lines[0].lower().startswith("суть"):
                body_lines[0] = f"Суть: {body_lines[0]}"
            body = "\n\n".join(body_lines)

            if not title_ru or len(title_ru) > 250:
                log("Заголовок не распознан, пробую ещё раз")
                time.sleep(5)
                continue

            log(f"Успешно, получено {len(body)} символов")
            return title_ru, body

        except Exception as e:
            error = str(e)
            # Лимит логируем целиком: в тексте написано, какой именно предел
            # исчерпан и через сколько сбросится
            log(f"Ошибка (попытка {attempt}): {error[:400]}")
            if "rate" in error.lower() or "429" in error:
                log("Лимит запросов, ждём 60 секунд...")
                time.sleep(60)
            else:
                time.sleep(10)

    log(f"Анализ не удался, статья пропущена: {title[:60]}")
    return None


def smart_truncate(text, max_len):
    """Обрезаем по границе предложения, а не посреди фразы"""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    best = -1
    for sep in [". ", "! ", "? ", ".\n", "!\n", "?\n"]:
        pos = cut.rfind(sep)
        if pos > best:
            best = pos
    if best > 200:
        return cut[:best + 1].rstrip()
    return cut.rsplit(" ", 1)[0] + "…"


def build_message(title_ru, body, source_name, article_url, goodbye, limit):
    """Собираем сообщение под лимит: если не влезает, тело режется по предложению"""
    head = f"🔴 <b>{esc(title_ru)}</b>\n\n"
    tail = f"\n\n🔗 {esc(source_name)}: {esc(article_url)}{goodbye}"
    room = limit - len(head) - len(tail)
    body_esc = esc(smart_truncate(body, max(room, 200)))
    body_esc = body_esc.replace("Суть:", "<b>Суть:</b>", 1).replace("Прогноз:", "<b>Прогноз:</b>", 1)
    return head + body_esc + tail


def get_world_news(count):
    try:
        resp = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={
                "apiKey": NEWS_KEY,
                "language": "en",
                "pageSize": 40,
                "category": "general"
            },
            timeout=15
        )
        articles = resp.json().get("articles", [])
        learn_titles(a.get("title", "") for a in articles)
        articles = [a for a in articles if is_relevant(a)]
        articles = deduplicate_articles(articles)
        log(f"Мировые новости: найдено {len(articles)} после фильтрации")
        articles = select_top_articles(articles, count - RESERVE, "главные мировые события")
        return articles[:count]
    except Exception as e:
        log(f"Ошибка получения мировых новостей: {e}")
        return []


def strip_tags(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(xml, source_name):
    """Разбираем RSS в тот же формат, в котором статьи приходят от NewsAPI,
    чтобы дальше работали те же фильтры."""
    articles = []
    for raw in re.findall(r"<item[^>]*>(.*?)</item>", xml, re.S):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", raw, re.S)
            if not m:
                return ""
            value = m.group(1).strip()
            cdata = re.match(r"<!\[CDATA\[(.*?)\]\]>", value, re.S)
            return (cdata.group(1) if cdata else value).strip()

        link = tag("link")
        title = strip_tags(tag("title"))
        if not link or not title:
            continue

        image = ""
        enclosure = re.search(r"<enclosure[^>]*url=[\"']([^\"']+)[\"'][^>]*>", raw)
        if enclosure:
            image = enclosure.group(1)
        if not image:
            media = re.search(r"<media:(?:content|thumbnail)[^>]*url=[\"']([^\"']+)[\"']", raw)
            if media:
                image = media.group(1)
        if not image:
            inline = re.search(r"<img[^>]*src=[\"']([^\"']+)[\"']", raw)
            if inline:
                image = inline.group(1)

        published = ""
        pub_raw = tag("pubDate")
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                published = ""

        articles.append({
            "title": title,
            "description": strip_tags(tag("description"))[:600],
            "url": link,
            "urlToImage": image,
            "publishedAt": published,
            "source": {"name": source_name},
        })
    return articles


def fetch_feed(url, source_name):
    """Читаем RSS украинского издания. Пустой список при любой ошибке."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            log(f"RSS {source_name}: код {resp.status_code}")
            return []
        articles = parse_rss(resp.text, source_name)
        learn_titles(a.get("title", "") for a in articles)
        log(f"RSS {source_name}: {len(articles)} записей")
        return articles
    except Exception as e:
        log(f"RSS {source_name} не прочитался: {e.__class__.__name__}")
        return []


def newsapi_everything(params, label):
    """Запрос к NewsAPI с общими параметрами. Пустой список при любой ошибке,
    чтобы сбой одного запроса не ронял весь блок."""
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "apiKey": NEWS_KEY,
                "language": "en",
                "sortBy": "publishedAt",
                "from": date_from,
                **params
            },
            timeout=15
        )
        data = resp.json()
        if data.get("status") != "ok":
            log(f"NewsAPI ({label}) отказал: {data.get('code')} {str(data.get('message'))[:120]}")
            return []
        articles = data.get("articles", [])
        learn_titles(a.get("title", "") for a in articles)
        log(f"NewsAPI ({label}): пришло {len(articles)} статей")
        return articles
    except Exception as e:
        log(f"Ошибка запроса NewsAPI ({label}): {e}")
        return []


def get_ukraine_news(count):
    """Два источника: напрямую украинские издания и широкий поиск по мировым.
    Одного запроса по слову Ukraine не хватает, блок оставался полупустым."""
    EXCLUDE_RUSSIA_FOCUS = [
        "russia", "kremlin", "putin", "russian army", "russian forces",
        "moscow", "russian troops", "russian military"
    ]

    filtered = []

    # Украинские издания пишут про Украину по определению, поэтому проверку
    # на количество упоминаний страны к ним не применяем
    for feed_url, feed_name in UA_FEEDS:
        for a in fetch_feed(feed_url, feed_name):
            if is_relevant(a, skip_source_check=True):
                filtered.append(a)

    # Мировые издания: тут проверка на упоминания и на российский фокус нужна
    for a in newsapi_everything(
        {"q": "Ukraine OR Ukrainian OR Kyiv OR Zelensky", "pageSize": 50},
        "мировые про Украину"
    ):
        if not is_relevant(a, require_ukraine=True, skip_source_check=True):
            continue
        title = (a.get("title") or "").lower()
        description = (a.get("description") or "").lower()
        text = title + " " + description
        russia_count = sum(1 for w in EXCLUDE_RUSSIA_FOCUS if w in text)
        if russia_count >= 2 and ukraine_mentions(text) < 2:
            log(f"Пропускаю российский фокус: {a.get('title', '')[:50]}")
            continue
        filtered.append(a)

    filtered = deduplicate_articles(filtered)
    log(f"Украинские новости: найдено {len(filtered)} после фильтрации")
    filtered = select_top_articles(filtered, count - RESERVE, "жизнь Украины: политика, экономика, города, люди")
    return filtered[:count]


def get_kharkiv_news():
    """Ищем и по всем изданиям, и отдельно по украинским: харьковские сюжеты
    мировая пресса берёт редко, а местные издания пишут о них постоянно."""
    candidates = newsapi_everything(
        {"q": "Kharkiv OR Kharkov", "pageSize": 40}, "Харьков, все издания"
    )
    for feed_url, feed_name in UA_FEEDS:
        candidates += fetch_feed(feed_url, feed_name)

    articles = [a for a in candidates if is_relevant(a, require_kharkiv=True, skip_source_check=True)]
    articles = deduplicate_articles(articles)
    if articles:
        log(f"Харьков: найдена новость: {articles[0].get('title', '')[:50]}")
        return articles[0]
    log("Харьков: новостей не найдено")
    return None


def fetch_og_image(url):
    """Preview picture from the article page, for feeds without images
    (TechCrunch publishes none in its feed)"""
    try:
        resp = requests.get(
            url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0 Safari/537.36"
            }
        )
        if resp.status_code != 200:
            return ""
        head = resp.text[:300000]
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', head)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', head))
        return html.unescape(m.group(1)) if m else ""
    except Exception:
        return ""


def get_ai_feed_articles():
    """Fresh AI stories from the quality feeds, pictures filled in"""
    cutoff = now_utc - timedelta(hours=48)
    picked = []
    for feed_url, feed_name in AI_FEEDS:
        for a in fetch_feed(feed_url, feed_name):
            when = parse_published(a.get("publishedAt") or "")
            text = (a["title"] + " " + a["description"]).lower()
            # Cheap checks first: pages are fetched only for real candidates
            if a["url"] in sent_urls or not when or when < cutoff or ai_mentions(text) < 2:
                continue
            if not a["urlToImage"]:
                a["urlToImage"] = fetch_og_image(a["url"])
            picked.append(a)
    return picked


def interleave_by_source(articles):
    """Round-robin by outlet: the editor sees only the first 20 candidates,
    and without mixing one outlet that came first would take them all"""
    queues = {}
    for a in articles:
        queues.setdefault(get_domain(a.get("url", "")), []).append(a)
    mixed = []
    while any(queues.values()):
        for queue in queues.values():
            if queue:
                mixed.append(queue.pop(0))
    return mixed


def get_ai_news(count):
    """Quality AI outlets first, NewsAPI adds to them, and second-tier sites
    are used only when both together are not enough"""
    try:
        from_feeds = [a for a in get_ai_feed_articles()
                      if is_relevant(a, require_ai=True, skip_source_check=True)]
        log(f"AI feeds: {len(from_feeds)} candidates from quality outlets")

        articles = newsapi_everything(
            {"q": "artificial intelligence OR AI OR robotics OR machine learning OR ChatGPT "
                  "OR OpenAI OR Anthropic OR Gemini OR neural network",
             "pageSize": 60},
            "AI"
        )
        trusted = [a for a in articles if is_relevant(a, require_ai=True)]
        candidates = deduplicate_articles(from_feeds + trusted)
        log(f"AI news: {len(candidates)} candidates from quality and trusted outlets")

        if len(candidates) < count + 2:
            log("Not enough quality AI candidates, adding second-tier outlets")
            known = {a.get("url") for a in candidates}
            extra = [a for a in articles
                     if a.get("url") not in known
                     and is_relevant(a, require_ai=True, skip_source_check=True)]
            candidates = deduplicate_articles(candidates + extra)

        candidates = interleave_by_source(candidates)
        log(f"AI news: {len(candidates)} candidates after filtering")
        candidates = select_top_articles(
            candidates, count - RESERVE,
            "искусственный интеллект: модели, компании, исследования, влияние ИИ на общество"
        )
        return candidates[:count]
    except Exception as e:
        log(f"Ошибка получения AI новостей: {e}")
        return []


def build_ukraine_block(count):
    """Возвращает (кандидаты, сколько публиковать).
    Харьковская новость ставится третьей, чтобы попасть в выпуск обязательно,
    запасные украинские статьи идут после неё."""
    ukraine = get_ukraine_news(count + RESERVE)
    kharkiv = get_kharkiv_news()

    if kharkiv:
        # The Kharkiv story is mandatory, so on overlap drop the general
        # Ukrainian article, not the Kharkiv one
        kharkiv_title = kharkiv.get("title", "")
        ukraine = [
            a for a in ukraine
            if a.get("url") != kharkiv.get("url")
            and not is_same_story(kharkiv_title, a.get("title", ""))
        ]
        candidates = ukraine[:count] + [kharkiv] + ukraine[count:]
        return candidates, count + 1

    return ukraine, count


def send_news_block(articles, needed, header=None, add_goodbye=False, block_name=""):
    """Сначала готовим тексты всех новостей, и только потом публикуем.
    Так заголовок блока не уходит в канал, если новостей в итоге нет."""
    if not articles:
        msg = f"⚠️ Блок <b>{esc(block_name)}</b> ({today_str}): новостей не найдено!"
        log(msg)
        tg_notify_me(msg)
        return

    # ── Этап подготовки: анализируем, неудачные пропускаем ──
    posts = []
    for article in articles:
        if len(posts) >= needed:
            break
        title        = article.get("title", "").split(" - ")[0].strip()
        description  = article.get("description", "")
        source_name  = article.get("source", {}).get("name", "Unknown")
        published_at = article.get("publishedAt")
        article_url  = article.get("url", "")

        log(f"Обрабатываю: {title[:60]} | {article_url[:70]}")
        result = analyze(title, description, source_name, published_at, article_url)
        if result is None:
            continue
        posts.append((article, result))

    if not posts:
        msg = f"⚠️ Блок <b>{esc(block_name)}</b> ({today_str}): ни одна новость не обработалась, проверь Groq!"
        log(msg)
        tg_notify_me(msg)
        return

    # ── Этап публикации ──
    if header:
        tg_text(header)
        time.sleep(2)

    for i, (article, (title_ru, body)) in enumerate(posts):
        title       = article.get("title", "").split(" - ")[0].strip()
        image_url   = article.get("urlToImage")
        source_name = article.get("source", {}).get("name", "Unknown")
        article_url = article.get("url", "")

        is_last = (i == len(posts) - 1)
        goodbye = "\n\n✅ Это все новости на сегодня. Хорошего вечера! 🙂" if (add_goodbye and is_last) else ""

        sent = False
        if image_url:
            # Основной формат: фото крупным превью над развёрнутым текстом
            message = build_message(title_ru, body, source_name, article_url, goodbye, 4096)
            sent = tg_message_with_preview(message, image_url)
            if not sent:
                # Запасной формат: классическое фото с подписью
                caption = build_message(title_ru, body, source_name, article_url, goodbye, 1024)
                sent = tg_photo_with_caption(image_url, caption)
        if not sent:
            message = build_message(title_ru, body, source_name, article_url, goodbye, 4096)
            sent = tg_text(message)

        if sent:
            save_sent_url(article_url, sent_urls)
            remember_story(title)
        else:
            log(f"Новость не отправилась ни одним способом: {title[:60]}")

        if not is_last:
            log(f"Пауза {PAUSE_BETWEEN} секунд...")
            time.sleep(PAUSE_BETWEEN)


log(f"=== Запуск блока: {BLOCK} (тест: {TEST_MODE}) ===")
story_memory = load_story_memory()

# ── УТРЕННИЙ БЛОК 08:00 ──
if BLOCK == "morning":
    world = get_world_news(4 + RESERVE)
    ukraine_candidates, ukraine_needed = build_ukraine_block(2)

    send_news_block(world, 4, header=f"🌍 <b>УТРЕННИЙ ОБЗОР НОВОСТЕЙ</b>\n{today_str}", block_name="Утренний мир")
    if ukraine_candidates:
        send_news_block(ukraine_candidates, ukraine_needed, header="🇺🇦 <b>НОВОСТИ УКРАИНЫ</b>", block_name="Утренняя Украина")

# ── AI БЛОК 10:00 ──
elif BLOCK == "ai_morning":
    ai_news = get_ai_news(AI_COUNT + RESERVE)
    send_news_block(ai_news, AI_COUNT, header=f"🤖 <b>AI NEWS</b>\n{today_str}", block_name="AI утро")

# ── ДНЕВНОЙ БЛОК 13:00 ──
elif BLOCK == "midday":
    world = get_world_news(4 + RESERVE)
    send_news_block(world, 4, header=f"🌍 <b>ДНЕВНОЙ ОБЗОР НОВОСТЕЙ</b>\n{today_str}", block_name="Дневной мир")

# ── ВЕЧЕРНИЙ БЛОК 18:00 ──
elif BLOCK == "evening":
    world = get_world_news(4 + RESERVE)
    ukraine_candidates, ukraine_needed = build_ukraine_block(2)

    send_news_block(world, 4, header=f"🌍 <b>ВЕЧЕРНИЙ ОБЗОР НОВОСТЕЙ</b>\n{today_str}", block_name="Вечерний мир")
    if ukraine_candidates:
        send_news_block(ukraine_candidates, ukraine_needed, header="🇺🇦 <b>НОВОСТИ УКРАИНЫ</b>", block_name="Вечерняя Украина")

# ── AI БЛОК 20:00 ──
elif BLOCK == "ai_evening":
    ai_news = get_ai_news(AI_COUNT + RESERVE)
    send_news_block(ai_news, AI_COUNT, header=f"🤖 <b>AI NEWS</b>\n{today_str}", add_goodbye=True, block_name="AI вечер")

trim_sent_urls()
trim_story_memory()
log(f"=== Блок {BLOCK} завершён ===")
trim_log()
