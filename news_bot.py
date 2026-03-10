import os
import re
import time
import requests
from groq import Groq
from datetime import datetime, timedelta

GROQ_KEY   = os.environ["GROQ_API_KEY"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY   = os.environ["NEWS_API_KEY"]
# Добавь в GitHub Secrets свой личный Telegram ID
MY_CHAT_ID = os.environ.get("MY_CHAT_ID", "")

utc_now     = datetime.utcnow()
utc_hour    = utc_now.hour
kyiv_offset = 2
kyiv_hour   = (utc_hour + kyiv_offset) % 24

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

print(f"UTC: {utc_hour}, Киев: {kyiv_hour}, блок: {BLOCK}")

# Защита от повторного запуска
LAST_RUN_FILE = "last_run.txt"
current_run_key = f"{utc_now.strftime('%Y-%m-%d')}-{BLOCK}"

if os.path.exists(LAST_RUN_FILE):
    with open(LAST_RUN_FILE, "r") as f:
        last_run = f.read().strip()
    if last_run == current_run_key:
        print(f"Блок {BLOCK} уже выполнялся сегодня, пропускаю.")
        exit(0)

with open(LAST_RUN_FILE, "w") as f:
    f.write(current_run_key)

today_str = datetime.now().strftime("%d.%m.%Y")
date_from = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

client = Groq(api_key=GROQ_KEY)

# ── Надёжные источники ──
TRUSTED_SOURCES = {
    "reuters", "bbc news", "bbc sport", "associated press", "ap news",
    "bloomberg", "the guardian", "the new york times", "washington post",
    "the wall street journal", "financial times", "al jazeera",
    "cnn", "nbc news", "abc news", "cbs news", "npr",
    "the economist", "time", "newsweek", "foreign policy",
    "politico", "axios", "the hill", "the atlantic",
    "wired", "techcrunch", "the verge", "ars technica", "mit technology review",
    "science", "nature", "new scientist",
    "kyiv independent", "ukrinform", "ukrainska pravda",
    "detroit free press", "irish times", "globesecurity.org",
    "le monde", "der spiegel", "el pais"
}

# ── Ненадёжные источники ──
BLOCKED_SOURCES = {
    "naturalnews", "breitbart", "infowars", "dailywire",
    "thegatewaypundit", "zerohedge", "rt.com", "sputnik",
    "tass", "ria novosti", "pravda"
}

EXCLUDE_KEYWORDS = [
    "wwe", "nfl", "nba", "spoiler", "wrestling", "celebrity",
    "kardashian", "taylor swift", "oscar", "grammy", "box office",
    "recipe", "horoscope", "zodiac"
]

SENT_URLS_FILE  = "sent_urls.txt"
LOG_FILE        = "log.txt"


# ── Логирование ──
def log(msg):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_sent_urls():
    if not os.path.exists(SENT_URLS_FILE):
        return set()
    with open(SENT_URLS_FILE, "r") as f:
        urls = set(line.strip() for line in f if line.strip())
    log(f"Загружено {len(urls)} уже отправленных новостей")
    if len(urls) > 200:
        urls = set(list(urls)[-200:])
    return urls


def save_sent_url(url, sent_urls):
    sent_urls.add(url)
    with open(SENT_URLS_FILE, "a") as f:
        f.write(url + "\n")


sent_urls = load_sent_urls()
# Заголовки отправленных новостей для проверки дублей по смыслу
sent_titles = []


def tg_send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "Markdown"
            },
            timeout=15
        )
    except Exception as e:
        log(f"Ошибка отправки текста: {e}")


def tg_text(text):
    tg_send(TG_CHAT_ID, text)


def tg_notify_me(text):
    """Личное уведомление если что-то пошло не так"""
    if MY_CHAT_ID:
        tg_send(MY_CHAT_ID, text)


def tg_photo_with_caption(image_url, caption):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            json={
                "chat_id": TG_CHAT_ID,
                "photo": image_url,
                "caption": caption[:1024],
                "parse_mode": "Markdown"
            },
            timeout=15
        )
        if resp.status_code == 200:
            return True
        log(f"Фото не отправилось: {resp.status_code}")
        return False
    except Exception as e:
        log(f"Ошибка отправки фото: {e}")
        return False


def is_fresh(article):
    published = article.get("publishedAt", "")
    if not published:
        return False
    try:
        pub_date = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
        age = datetime.utcnow() - pub_date
        if age.total_seconds() > 48 * 3600:
            log(f"Старая новость ({published}): {article.get('title', '')[:40]}")
            return False
        return True
    except Exception:
        return True


def is_trusted_source(article):
    source_name = (article.get("source", {}).get("name") or "").lower()
    url = (article.get("url") or "").lower()

    # Проверяем заблокированные источники
    for blocked in BLOCKED_SOURCES:
        if blocked in source_name or blocked in url:
            log(f"Заблокированный источник ({source_name}): {article.get('title', '')[:40]}")
            return False

    # Проверяем надёжные источники
    for trusted in TRUSTED_SOURCES:
        if trusted in source_name:
            return True

    # Если источник не в списке — пропускаем
    log(f"Неизвестный источник ({source_name}): {article.get('title', '')[:40]}")
    return False


def normalize_title(title):
    """Нормализуем заголовок для сравнения дублей"""
    title = title.lower()
    title = re.sub(r'[^a-zа-я0-9\s]', '', title)
    words = set(title.split())
    return words


def is_duplicate_by_title(title):
    """Проверяем похожесть заголовка с уже отправленными"""
    new_words = normalize_title(title)
    if len(new_words) < 3:
        return False
    for sent_title in sent_titles:
        sent_words = normalize_title(sent_title)
        if len(sent_words) < 3:
            continue
        # Если больше 60% слов совпадают — дубль
        intersection = new_words & sent_words
        similarity = len(intersection) / max(len(new_words), len(sent_words))
        if similarity > 0.6:
            log(f"Дубль по смыслу: {title[:50]}")
            return True
    return False


def is_relevant(article, require_ukraine=False, require_kharkiv=False):
    title = (article.get("title") or "").lower()
    description = (article.get("description") or "").lower()
    text = title + " " + description

    if article.get("title") == "[Removed]":
        return False
    if article.get("description") == "[Removed]":
        return False
    if not article.get("description"):
        return False

    # Только статьи с фото
    if not article.get("urlToImage"):
        log(f"Нет фото: {article.get('title', '')[:40]}")
        return False

    if not is_fresh(article):
        return False

    if not is_trusted_source(article):
        return False

    for word in EXCLUDE_KEYWORDS:
        if word in text:
            return False

    if require_ukraine:
        ukraine_count = text.count("ukraine") + text.count("ukrainian")
        if ukraine_count < 2:
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


def analyze(title, description, source_name, published_at=None):
    if published_at:
        try:
            pub_date = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
            date_str = pub_date.strftime("%d.%m.%Y")
        except Exception:
            date_str = today_str
    else:
        date_str = today_str

    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
Описание: {description}
Источник: {source_name}
Дата публикации: {date_str}

Напиши ответ на русском языке строго в таком формате — три блока, каждый с новой строки:

Первая строка: переведи заголовок на русский язык точно передавая смысл. Если дословный перевод звучит абсурдно или вводит в заблуждение — перефразируй так чтобы было понятно о чём новость.
Суть: обязательно начни с "Дата: {date_str}." затем 2-3 предложения — конкретные имена, страны, организации, цифры. Не пиши "правительство" — пиши "правительство США". Не пиши "компания" — пиши название компании. Описывай только то что реально написано в новости, не домысливай.
Прогноз: только если в новости есть реальные факты для прогноза — напиши 2-3 конкретных последствия. Если это мнение аналитика или блогера без фактов — напиши "Прогноз: требует дополнительного подтверждения."

Весь ответ не длиннее 800 символов. Никаких звёздочек."""

    for attempt in range(1, 4):
        try:
            log(f"Попытка {attempt} для: {title[:40]}")
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.5
            )
            raw = response.choices[0].message.content.strip()

            lines = [l.strip() for l in raw.split("\n") if l.strip()]
            if lines:
                lines[0] = f"🔴 *{lines[0]}*"
            result = "\n\n".join(lines)

            log(f"Успешно, получено {len(result)} символов")
            return result

        except Exception as e:
            error = str(e)
            log(f"Ошибка (попытка {attempt}): {error[:150]}")
            if "rate" in error.lower() or "429" in error:
                log("Лимит запросов, ждём 60 секунд...")
                time.sleep(60)
            else:
                time.sleep(10)

    return "Анализ временно недоступен."


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
        articles = [a for a in articles if is_relevant(a)]
        log(f"Мировые новости: найдено {len(articles)} после фильтрации")
        return articles[:count]
    except Exception as e:
        log(f"Ошибка получения мировых новостей: {e}")
        return []


def get_ukraine_news(count):
    EXCLUDE_RUSSIA_FOCUS = [
        "russia", "kremlin", "putin", "russian army", "russian forces",
        "moscow", "russian troops", "russian military"
    ]
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "apiKey": NEWS_KEY,
                "q": "Ukraine",
                "language": "en",
                "pageSize": 40,
                "sortBy": "publishedAt",
                "from": date_from
            },
            timeout=15
        )
        articles = resp.json().get("articles", [])
        filtered = []
        for a in articles:
            if not is_relevant(a, require_ukraine=True):
                continue
            title = (a.get("title") or "").lower()
            description = (a.get("description") or "").lower()
            text = title + " " + description
            russia_count = sum(1 for w in EXCLUDE_RUSSIA_FOCUS if w in text)
            if russia_count >= 2 and text.count("ukraine") < 2:
                log(f"Пропускаю российский фокус: {a.get('title', '')[:50]}")
                continue
            filtered.append(a)
        log(f"Украинские новости: найдено {len(filtered)} после фильтрации")
        return filtered[:count]
    except Exception as e:
        log(f"Ошибка получения новостей по Украине: {e}")
        return []


def get_kharkiv_news():
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "apiKey": NEWS_KEY,
                "q": "Kharkiv OR Kharkov",
                "language": "en",
                "pageSize": 20,
                "sortBy": "publishedAt",
                "from": date_from
            },
            timeout=15
        )
        articles = resp.json().get("articles", [])
        articles = [a for a in articles if is_relevant(a, require_kharkiv=True)]
        if articles:
            log(f"Харьков: найдена новость — {articles[0].get('title', '')[:50]}")
            return articles[0]
        log("Харьков: новостей не найдено")
        return None
    except Exception as e:
        log(f"Ошибка получения новостей Харькова: {e}")
        return None


def get_ai_news(count):
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "apiKey": NEWS_KEY,
                "q": "artificial intelligence OR AI OR robotics OR machine learning OR ChatGPT OR OpenAI OR Gemini OR neural network",
                "language": "en",
                "pageSize": 40,
                "sortBy": "publishedAt",
                "from": date_from
            },
            timeout=15
        )
        articles = resp.json().get("articles", [])
        articles = [a for a in articles if is_relevant(a)]
        log(f"AI новости: найдено {len(articles)} после фильтрации")
        return articles[:count]
    except Exception as e:
        log(f"Ошибка получения AI новостей: {e}")
        return []


def build_ukraine_block(count):
    ukraine = get_ukraine_news(count)
    kharkiv = get_kharkiv_news()
    ukraine_urls = [a.get("url") for a in ukraine]
    if kharkiv and kharkiv.get("url") not in ukraine_urls:
        return ukraine + [kharkiv]
    return ukraine


def send_news_block(articles, header=None, add_goodbye=False, block_name=""):
    if not articles:
        msg = f"⚠️ Блок *{block_name}* ({today_str}) — новостей не найдено!"
        log(msg)
        tg_notify_me(msg)
        return

    if header:
        tg_text(header)
        time.sleep(2)

    for i, article in enumerate(articles):
        title        = article.get("title", "").split(" - ")[0].strip()
        description  = article.get("description", "")
        image_url    = article.get("urlToImage")
        source_name  = article.get("source", {}).get("name", "Unknown")
        article_url  = article.get("url", "")
        published_at = article.get("publishedAt")

        log(f"Обрабатываю: {title[:60]}")

        analysis = analyze(title, description, source_name, published_at)

        is_last = (i == len(articles) - 1)
        goodbye = "\n\n✅ Это все новости на сегодня. Хорошего вечера! 🙂" if (add_goodbye and is_last) else ""

        message = f"{analysis}\n\n🔗 {source_name}: {article_url}{goodbye}"

        if image_url:
            sent = tg_photo_with_caption(image_url, message)
            if not sent:
                tg_text(message)
        else:
            tg_text(message)

        save_sent_url(article_url, sent_urls)
        sent_titles.append(title)

        if not is_last:
            log("Пауза 60 секунд...")
            time.sleep(60)


log(f"=== Запуск блока: {BLOCK} ===")

# ── УТРЕННИЙ БЛОК 08:00 ──
if BLOCK == "morning":
    world         = get_world_news(4)
    ukraine_block = build_ukraine_block(2)

    send_news_block(world, header=f"🌍 *УТРЕННИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}", block_name="Утренний мир")
    if ukraine_block:
        send_news_block(ukraine_block, header="🇺🇦 *НОВОСТИ УКРАИНЫ*", block_name="Утренняя Украина")

# ── AI БЛОК 10:00 ──
elif BLOCK == "ai_morning":
    ai_news = get_ai_news(3)
    send_news_block(ai_news, header=f"🤖 *AI NEWS*\n{today_str}", block_name="AI утро")

# ── ДНЕВНОЙ БЛОК 13:00 ──
elif BLOCK == "midday":
    world = get_world_news(4)
    send_news_block(world, header=f"🌍 *ДНЕВНОЙ ОБЗОР НОВОСТЕЙ*\n{today_str}", block_name="Дневной мир")

# ── ВЕЧЕРНИЙ БЛОК 18:00 ──
elif BLOCK == "evening":
    world         = get_world_news(4)
    ukraine_block = build_ukraine_block(2)

    send_news_block(world, header=f"🌍 *ВЕЧЕРНИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}", block_name="Вечерний мир")
    if ukraine_block:
        send_news_block(ukraine_block, header="🇺🇦 *НОВОСТИ УКРАИНЫ*", block_name="Вечерняя Украина")

# ── AI БЛОК 20:00 ──
elif BLOCK == "ai_evening":
    ai_news = get_ai_news(3)
    send_news_block(ai_news, header=f"🤖 *AI NEWS*\n{today_str}", add_goodbye=True, block_name="AI вечер")

log(f"=== Блок {BLOCK} завершён ===")
