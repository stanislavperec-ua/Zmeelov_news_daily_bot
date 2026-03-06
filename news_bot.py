import os
import time
import requests
from groq import Groq
from datetime import datetime

GROQ_KEY   = os.environ["GROQ_API_KEY"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY   = os.environ["NEWS_API_KEY"]

utc_now     = datetime.utcnow()
utc_hour    = utc_now.hour
kyiv_offset = 2
kyiv_hour   = (utc_hour + kyiv_offset) % 24

if kyiv_hour == 8:
    BLOCK = "morning"
elif kyiv_hour == 10:
    BLOCK = "ai_morning"
elif kyiv_hour == 13:
    BLOCK = "midday"
elif kyiv_hour == 18:
    BLOCK = "evening"
elif kyiv_hour == 20:
    BLOCK = "ai_evening"
else:
    BLOCK = "morning"

print(f"UTC: {utc_hour}, Киев: {kyiv_hour}, блок: {BLOCK}")

today_str = datetime.now().strftime("%d.%m.%Y")

client = Groq(api_key=GROQ_KEY)

EXCLUDE_KEYWORDS = [
    "wwe", "nfl", "nba", "spoiler", "wrestling", "celebrity",
    "kardashian", "taylor swift", "oscar", "grammy", "box office",
    "recipe", "horoscope", "zodiac"
]


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


def is_relevant(article):
    title = (article.get("title") or "").lower()
    description = (article.get("description") or "").lower()
    text = title + " " + description

    if article.get("title") == "[Removed]":
        return False
    if article.get("description") == "[Removed]":
        return False
    if not article.get("description"):
        return False

    for word in EXCLUDE_KEYWORDS:
        if word in text:
            return False

    return True


def analyze(title, description, source_name):
    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
Описание: {description}
Источник: {source_name}

Напиши ответ на русском языке строго в таком формате — три блока, каждый с новой строки:

Первая строка: только переведённый заголовок на русском
Суть: (2-3 предложения — конкретные имена, страны, организации, цифры. Не пиши "правительство" — пиши "правительство США". Не пиши "компания" — пиши название компании.)
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
                "pageSize": 20,
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
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "apiKey": NEWS_KEY,
                "q": "Ukraine",
                "language": "en",
                "pageSize": 20,
                "sortBy": "publishedAt"
            },
            timeout=15
        )
        articles = resp.json().get("articles", [])
        articles = [a for a in articles if is_relevant(a)]
        return articles[:count]
    except Exception as e:
        print(f"Ошибка получения новостей по Украине: {e}")
        return []


def get_ai_news(count):
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "apiKey": NEWS_KEY,
                "q": "artificial intelligence OR AI OR robotics OR machine learning OR ChatGPT OR OpenAI OR Gemini OR neural network",
                "language": "en",
                "pageSize": 20,
                "sortBy": "publishedAt"
            },
            timeout=15
        )
        articles = resp.json().get("articles", [])
        articles = [a for a in articles if is_relevant(a)]
        return articles[:count]
    except Exception as e:
        print(f"Ошибка получения AI новостей: {e}")
        return []


def send_news_block(articles):
    for i, article in enumerate(articles):
        title       = article.get("title", "").split(" - ")[0].strip()
        description = article.get("description", "")
        image_url   = article.get("urlToImage")
        source_name = article.get("source", {}).get("name", "Unknown")
        article_url = article.get("url", "")

        print(f"\nОбрабатываю: {title[:60]}")

        analysis = analyze(title, description, source_name)

        message = (
            f"{analysis}\n\n"
            f"🔗 {source_name}: {article_url}"
        )

        if image_url:
            sent = tg_photo_with_caption(image_url, message)
            if not sent:
                tg_text(message)
        else:
            tg_text(message)

        if i < len(articles) - 1:
            print("Пауза 60 секунд...")
            time.sleep(60)


# ── УТРЕННИЙ БЛОК 08:00 ──
if BLOCK == "morning":
    world   = get_world_news(4)
    ukraine = get_ukraine_news(3)

    tg_text(f"🌍 *УТРЕННИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}")

    send_news_block(world)
    if ukraine:
        tg_text("🇺🇦 *НОВОСТИ УКРАИНЫ*")
        send_news_block(ukraine)

# ── AI БЛОК 10:00 ──
elif BLOCK == "ai_morning":
    ai_news = get_ai_news(3)

    tg_text(f"🤖 *AI NEWS*\n{today_str}")

    send_news_block(ai_news)

# ── ДНЕВНОЙ БЛОК 13:00 ──
elif BLOCK == "midday":
    world = get_world_news(4)

    tg_text(f"🌍 *ДНЕВНОЙ ОБЗОР НОВОСТЕЙ*\n{today_str}")

    send_news_block(world)

# ── ВЕЧЕРНИЙ БЛОК 18:00 ──
elif BLOCK == "evening":
    world   = get_world_news(4)
    ukraine = get_ukraine_news(3)

    tg_text(f"🌍 *ВЕЧЕРНИЙ ОБЗОР НОВОСТЕЙ*\n{today_str}")

    send_news_block(world)
    if ukraine:
        tg_text("🇺🇦 *НОВОСТИ УКРАИНЫ*")
        send_news_block(ukraine)

    tg_text("✅ Это все новости на сегодня. Хорошего вечера! 🙂")

# ── AI БЛОК 20:00 ──
elif BLOCK == "ai_evening":
    ai_news = get_ai_news(3)

    tg_text(f"🤖 *AI NEWS*\n{today_str}")

    send_news_block(ai_news)

print("Готово!")





  
