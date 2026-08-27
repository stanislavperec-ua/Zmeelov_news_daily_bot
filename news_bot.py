import os
import re
import time
import requests
from groq import Groq
from datetime import datetime, timedelta

# ── Модель Groq. При следующей замене менять только эту строку. ──
# Актуальные модели: https://console.groq.com/docs/models
GROQ_MODEL = "openai/gpt-oss-120b"

# Сколько запасных статей запрашивать сверх нужного количества:
# если анализ одной статьи не удался, берём следующую из запаса.
RESERVE = 3

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

print(f"UTC: {utc_hour}, Киев: {kyiv_hour}, блок: {BLOCK}, тест: {TEST_MODE}")

LAST_RUN_FILE = "last_run.txt"
current_run_key = f"{utc_now.strftime('%Y-%m-%d')}-{BLOCK}"

if not TEST_MODE:
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

SENT_URLS_FILE = "sent_urls.txt"
LOG_FILE       = "log.txt"


def log(msg):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_sent_urls():
    if not os.path.exists(SENT_URLS_FILE):
        return set()
    with open(SENT_URLS_FILE, "r") as f:
        urls = set(line.strip() for line in f if line.strip())
    log(f"Загружено {len(urls)} уже отправленных новостей")
    return urls


def save_sent_url(url, sent_urls):
    sent_urls.add(url)
    if TEST_MODE:
        return
    with open(SENT_URLS_FILE, "a") as f:
        f.write(url + "\n")


sent_urls   = load_sent_urls()
sent_titles = []


def tg_send(chat_id, text):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "Markdown"
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


def tg_photo_with_caption(image_url, caption):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            json={
                "chat_id": TARGET_CHAT_ID,
                "photo": image_url,
                "caption": caption[:1024],
                "parse_mode": "Markdown"
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


def is_blocked_source(article):
    source_name = (article.get("source", {}).get("name") or "").lower()
    url = (article.get("url") or "").lower()
    for blocked in BLOCKED_SOURCES:
        if blocked in source_name or blocked in url:
            log(f"Заблокированный источник ({source_name}): {article.get('title', '')[:40]}")
            return True
    return False


def is_trusted_source(article):
    source_name = (article.get("source", {}).get("name") or "").lower()
    if is_blocked_source(article):
        return False
    for trusted in TRUSTED_SOURCES:
        if trusted in source_name:
            return True
    log(f"Неизвестный источник ({source_name}): {article.get('title', '')[:40]}")
    return False


def normalize_title(title):
    title = title.lower()
    title = re.sub(r'[^a-zа-я0-9\s]', '', title)
    return set(title.split())


def is_similar_title(title1, title2):
    """Проверяем похожесть двух заголовков"""
    words1 = normalize_title(title1)
    words2 = normalize_title(title2)
    if len(words1) < 3 or len(words2) < 3:
        return False
    intersection = words1 & words2
    similarity = len(intersection) / max(len(words1), len(words2))
    return similarity > 0.6


def is_duplicate_by_title(title):
    """Проверяем против уже отправленных"""
    for sent_title in sent_titles:
        if is_similar_title(title, sent_title):
            log(f"Дубль по смыслу: {title[:50]}")
            return True
    return False


def deduplicate_articles(articles):
    """Убираем дубли внутри одного списка статей"""
    unique = []
    for article in articles:
        title = article.get("title", "")
        is_dup = False
        for u in unique:
            if is_similar_title(title, u.get("title", "")):
                log(f"Внутренний дубль: {title[:50]}")
                is_dup = True
                break
        if not is_dup:
            unique.append(article)
    return unique


def is_relevant(article, require_ukraine=False, require_kharkiv=False, skip_source_check=False):
    title = (article.get("title") or "").lower()
    description = (article.get("description") or "").lower()
    text = title + " " + description

    if article.get("title") == "[Removed]":
        return False
    if article.get("description") == "[Removed]":
        return False
    if not article.get("description"):
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


def clean_model_output(text):
    """Убираем markdown-разметку, которую модель может добавить вопреки промпту"""
    text = text.replace("**", "").replace("###", "").replace("##", "")
    return text.strip()


def analyze(title, description, source_name, published_at=None):
    """Возвращает готовый текст новости или None, если анализ не удался.
    При None статья не публикуется и не считается использованной."""
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

Напиши ответ на русском языке строго в таком формате: три блока.

Первая строка: литературный перевод заголовка на русский язык. Передавай смысл точно и красиво, избегай дословного перевода если он звучит неестественно. Заголовок должен читаться как заголовок качественного русскоязычного издания.

Суть: начни с даты "{date_str}." затем напиши 6-7 содержательных предложений которые полностью раскрывают новость. Указывай конкретные имена людей, названия стран, организаций, цифры и факты. Пиши живым литературным языком как журналист качественного издания. Не домысливай: только то что есть в новости.

Прогноз: напиши 2-3 конкретных и обоснованных предложения о возможных последствиях этого события для стран, людей, рынков или политики. Прогноз должен быть логически связан с фактами из новости и звучать профессионально.

Весь ответ не длиннее 1000 символов. Никаких звёздочек и никакой разметки."""

    for attempt in range(1, 4):
        try:
            log(f"Попытка {attempt} для: {title[:40]}")
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=2000,
                temperature=0.6
            )
            raw = (response.choices[0].message.content or "").strip()
            raw = clean_model_output(raw)

            if len(raw) < 150 or "Суть" not in raw:
                log(f"Ответ модели слишком короткий или без структуры ({len(raw)} символов), пробую ещё раз")
                time.sleep(5)
                continue

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

    log(f"Анализ не удался, статья пропущена: {title[:60]}")
    return None


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
        articles = deduplicate_articles(articles)
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
            if not is_relevant(a, require_ukraine=True, skip_source_check=True):
                continue
            title = (a.get("title") or "").lower()
            description = (a.get("description") or "").lower()
            text = title + " " + description
            russia_count = sum(1 for w in EXCLUDE_RUSSIA_FOCUS if w in text)
            if russia_count >= 2 and text.count("ukraine") < 2:
                log(f"Пропускаю российский фокус: {a.get('title', '')[:50]}")
                continue
            filtered.append(a)
        filtered = deduplicate_articles(filtered)
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
        articles = [a for a in articles if is_relevant(a, require_kharkiv=True, skip_source_check=True)]
        if articles:
            log(f"Харьков: найдена новость: {articles[0].get('title', '')[:50]}")
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
        articles = deduplicate_articles(articles)
        log(f"AI новости: найдено {len(articles)} после фильтрации")
        return articles[:count]
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
        ukraine_urls = [a.get("url") for a in ukraine]
        kharkiv_title = kharkiv.get("title", "")
        is_dup = (kharkiv.get("url") in ukraine_urls) or any(
            is_similar_title(kharkiv_title, a.get("title", "")) for a in ukraine
        )
        if not is_dup:
            candidates = ukraine[:count] + [kharkiv] + ukraine[count:]
            return candidates, count + 1

    return ukraine, count


def send_news_block(articles, needed, header=None, add_goodbye=False, block_name=""):
    """Сначала готовим тексты всех новостей, и только потом публикуем.
    Так заголовок блока не уходит в канал, если новостей в итоге нет."""
    if not articles:
        msg = f"⚠️ Блок *{block_name}* ({today_str}): новостей не найдено!"
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

        log(f"Обрабатываю: {title[:60]}")
        analysis = analyze(title, description, source_name, published_at)
        if analysis is None:
            continue
        posts.append((article, analysis))

    if not posts:
        msg = f"⚠️ Блок *{block_name}* ({today_str}): ни одна новость не обработалась, проверь Groq!"
        log(msg)
        tg_notify_me(msg)
        return

    # ── Этап публикации ──
    if header:
        tg_text(header)
        time.sleep(2)

    for i, (article, analysis) in enumerate(posts):
        title       = article.get("title", "").split(" - ")[0].strip()
        image_url   = article.get("urlToImage")
        source_name = article.get("source", {}).get("name", "Unknown")
        article_url = article.get("url", "")

        is_last = (i == len(posts) - 1)
        goodbye = "\n\n✅ Это все новости на сегодня. Хорошего вечера! 🙂" if (add_goodbye and is_last) else ""

        message = f"{analysis}\n\n🔗 {source_name}: {article_url}{goodbye}"

        sent = False
        if image_url:
            sent = tg_photo_with_caption(image_url, message)
        if not sent:
            sent = tg_text(message)

        if sent:
            save_sent_url(article_url, sent_urls)
            sent_titles.append(title)
        else:
            log(f"Новость не отправилась ни фото, ни текстом: {title[:60]}")

        if not is_last:
            log(f"Пауза {PAUSE_BETWEEN} секунд...")
            time.sleep(PAUSE_BETWEEN)


log(f"=== Запуск блока: {BLOCK} (тест: {TEST_MODE}) ===")

# ── УТРЕННИЙ БЛОК 08:00 ──
if BLOCK == "morning":
    world = get_world_news(4 + RESERVE)
    ukraine_candidates, ukraine_needed = build_ukraine_block(2)

    send_news_block(world, 4, header=f"🌍 *УТРЕННИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}", block_name="Утренний мир")
    if ukraine_candidates:
        send_news_block(ukraine_candidates, ukraine_needed, header="🇺🇦 *НОВОСТИ УКРАИНЫ*", block_name="Утренняя Украина")

# ── AI БЛОК 10:00 ──
elif BLOCK == "ai_morning":
    ai_news = get_ai_news(3 + RESERVE)
    send_news_block(ai_news, 3, header=f"🤖 *AI NEWS*\n{today_str}", block_name="AI утро")

# ── ДНЕВНОЙ БЛОК 13:00 ──
elif BLOCK == "midday":
    world = get_world_news(4 + RESERVE)
    send_news_block(world, 4, header=f"🌍 *ДНЕВНОЙ ОБЗОР НОВОСТЕЙ*\n{today_str}", block_name="Дневной мир")

# ── ВЕЧЕРНИЙ БЛОК 18:00 ──
elif BLOCK == "evening":
    world = get_world_news(4 + RESERVE)
    ukraine_candidates, ukraine_needed = build_ukraine_block(2)

    send_news_block(world, 4, header=f"🌍 *ВЕЧЕРНИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}", block_name="Вечерний мир")
    if ukraine_candidates:
        send_news_block(ukraine_candidates, ukraine_needed, header="🇺🇦 *НОВОСТИ УКРАИНЫ*", block_name="Вечерняя Украина")

# ── AI БЛОК 20:00 ──
elif BLOCK == "ai_evening":
    ai_news = get_ai_news(3 + RESERVE)
    send_news_block(ai_news, 3, header=f"🤖 *AI NEWS*\n{today_str}", add_goodbye=True, block_name="AI вечер")

log(f"=== Блок {BLOCK} завершён ===")
