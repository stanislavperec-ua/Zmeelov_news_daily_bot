import os
import time
import requests
from groq import Groq
from datetime import datetime, timedelta

GROQ_KEY   = os.environ["GROQ_API_KEY"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY   = os.environ["NEWS_API_KEY"]

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

today_str = datetime.now().strftime("%d.%m.%Y")
date_from = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

client = Groq(api_key=GROQ_KEY)

EXCLUDE_KEYWORDS = [
    "wwe", "nfl", "nba", "spoiler", "wrestling", "celebrity",
    "kardashian", "taylor swift", "oscar", "grammy", "box office",
    "recipe", "horoscope", "zodiac"
]

SENT_URLS_FILE = "sent_urls.txt"


def load_sent_urls():
    if not os.path.exists(SENT_URLS_FILE):
        return set()
    with open(SENT_URLS_FILE, "r") as f:
        urls = set(line.strip() for line in f if line.strip())
    print(f"Загружено {len(urls)} уже отправленных новостей")
    if len(urls) > 200:
        urls = set(list(urls)[-200:])
    return urls


def save_sent_url(url, sent_urls):
    sent_urls.add(url)
    with open(SENT_URLS_FILE, "a") as f:
        f.write(url + "\n")


sent_urls = load_sent_urls()


def tg_text(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text[:4000],
                "parse_mode": "Markdown"
            },
            timeout=15
        )
    except Exception as e:
        print(f"Ошибка отправки текста: {e}")


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
        print(f"Фото не отправилось: {resp.status_code}")
        return False
    except Exception as e:
        print(f"Ошибка отправки фото: {e}")
        return False


def is_fresh(article):
    """Только проверка даты публикации — самый надёжный способ"""
    published = article.get("publishedAt", "")
    if not published:
        return False
    try:
        pub_date = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
        age = datetime.utcnow() - pub_date
        if age.total_seconds() > 24 * 3600:
            print(f"Старая новость ({published}): {article.get('title', '')[:40]}")
            return False
        return True
    except Exception:
        return True


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

    if not is_fresh(article):
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
        print(f"Пропускаю уже отправленную: {article.get('title', '')[:50]}")
        return False

    return True


def analyze(title, description, source_name):
    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
Описание: {description}
Источник: {source_name}

Напиши ответ на русском языке строго в таком формате — три блока, каждый с новой строки:

Первая строка: только переведённый заголовок на русском
Суть: (обязательно укажи дату события, затем 2-3 предложения — конкретные имена, страны, организации, цифры. Не пиши "правительство" — пиши "правительство США". Не пиши "компания" — пиши название компании.)
Прогноз: (2-3 предложения — конкретные последствия для стран, людей, рынков.)

Весь ответ не длиннее 800 символов. Никаких звёздочек."""

    for attempt in range(1, 4):
        try:
            print(f"Попытка {attempt}...")
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

            print(f"Успешно, получено {len(result)} символов")
            return result

        except Exception as e:
            error = str(e)
            print(f"Ошибка (попытка {attempt}): {error[:150]}")
            if "rate" in error.lower() or "429" in error:
                print("Лимит запросов, ждём 60 секунд...")
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
        return articles[:count]
    except Exception as e:
        print(f"Ошибка получения мировых новостей: {e}")
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
                print(f"Пропускаю российский фокус: {a.get('title', '')[:50]}")
                continue
            filtered.append(a)
        return filtered[:count]
    except Exception as e:
        print(f"Ошибка получения новостей по Украине: {e}")
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
            return articles[0]
        return None
    except Exception as e:
        print(f"Ошибка получения новостей Харькова: {e}")
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
        return articles[:count]
    except Exception as e:
        print(f"Ошибка получения AI новостей: {e}")
        return []


def build_ukraine_block(count):
    ukraine = get_ukraine_news(count)
    kharkiv = get_kharkiv_news()
    ukraine_urls = [a.get("url") for a in ukraine]
    if kharkiv and kharkiv.get("url") not in ukraine_urls:
        return ukraine + [kharkiv]
    return ukraine


def send_news_block(articles, header=None, add_goodbye=False):
    if header:
        tg_text(header)
        time.sleep(2)

    for i, article in enumerate(articles):
        title       = article.get("title", "").split(" - ")[0].strip()
        description = article.get("description", "")
        image_url   = article.get("urlToImage")
        source_name = article.get("source", {}).get("name", "Unknown")
        article_url = article.get("url", "")

        print(f"\nОбрабатываю: {title[:60]}")

        analysis = analyze(title, description, source_name)

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

        if not is_last:
            print("Пауза 60 секунд...")
            time.sleep(60)


# ── УТРЕННИЙ БЛОК 08:00 ──
if BLOCK == "morning":
    world         = get_world_news(4)
    ukraine_block = build_ukraine_block(2)

    send_news_block(world, header=f"🌍 *УТРЕННИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}")
    if ukraine_block:
        send_news_block(ukraine_block, header="🇺🇦 *НОВОСТИ УКРАИНЫ*")

# ── AI БЛОК 10:00 ──
elif BLOCK == "ai_morning":
    ai_news = get_ai_news(3)
    send_news_block(ai_news, header=f"🤖 *AI NEWS*\n{today_str}")

# ── ДНЕВНОЙ БЛОК 13:00 ──
elif BLOCK == "midday":
    world = get_world_news(4)
    send_news_block(world, header=f"🌍 *ДНЕВНОЙ ОБЗОР НОВОСТЕЙ*\n{today_str}")

# ── ВЕЧЕРНИЙ БЛОК 18:00 ──
elif BLOCK == "evening":
    world         = get_world_news(4)
    ukraine_block = build_ukraine_block(2)

    send_news_block(world, header=f"🌍 *ВЕЧЕРНИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}")
    if ukraine_block:
        send_news_block(ukraine_block, header="🇺🇦 *НОВОСТИ УКРАИНЫ*")

# ── AI БЛОК 20:00 ──
elif BLOCK == "ai_evening":
    ai_news = get_ai_news(3)
    send_news_block(ai_news, header=f"🤖 *AI NEWS*\n{today_str}", add_goodbye=True)

print("Готово!")
