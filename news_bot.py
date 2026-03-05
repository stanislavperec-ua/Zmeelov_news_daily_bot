import os
import time
import requests
from groq import Groq
from datetime import datetime

GROQ_KEY   = os.environ["GROQ_API_KEY"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY   = os.environ["NEWS_API_KEY"]

# Определяем киевское время и выбираем блок
utc_hour  = datetime.utcnow().hour
kyiv_hour = (utc_hour + 2) % 24  # Киев = UTC+2

if kyiv_hour < 12:
    BLOCK = "morning"    # 08:00 Киев — 4 мировых + 3 Украина
elif kyiv_hour < 17:
    BLOCK = "midday"     # 14:00 Киев — 4 мировых
else:
    BLOCK = "evening"    # 19:00 Киев — 4 мировых + 3 Украина + прощание

print(f"UTC час: {utc_hour}, Киев час: {kyiv_hour}, блок: {BLOCK}")

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
            json={"chat_id": TG_CHAT_ID, "text": text[:4000]},
            timeout=15
        )
    except Exception as e:
        print(f"Ошибка отправки текста: {e}")


def tg_photo(image_url):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            json={"chat_id": TG_CHAT_ID, "photo": image_url},
            timeout=15
        )
        return resp.status_code == 200
    except Exception:
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


def analyze(title, description):
    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
Описание: {description}

Напиши полный ответ на русском языке в таком формате:

Заголовок: (переведи на русский)

Суть: (напиши 2-3 полных предложения о том что произошло и почему важно)

Прогноз: (напиши 2-3 полных предложения о возможных последствиях для мира или России)

Каждый раздел должен быть полным и завершённым. Только чистый текст без звёздочек."""

    for attempt in range(1, 4):
        try:
            print(f"Попытка {attempt}...")
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.5
            )
            result = response.choices[0].message.content
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


def send_news_block(articles, start_index, total):
    for i, article in enumerate(articles, start_index):
        title       = article.get("title", "").split(" - ")[0].strip()
        description = article.get("description", "")
        image_url   = article.get("urlToImage")

        print(f"\nНовость {i}/{total}: {title[:60]}")

        analysis = analyze(title, description)
        message  = f"Новость {i} из {total}\n\n{analysis}"

        if image_url:
            tg_photo(image_url)

        tg_text(message)

        if i < total:
            print("Пауза 60 секунд...")
            time.sleep(60)


# ── УТРЕННИЙ БЛОК 08:00 — 4 мировых + 3 Украина ──
if BLOCK == "morning":
    world   = get_world_news(4)
    ukraine = get_ukraine_news(3)
    total   = len(world) + len(ukraine)

    tg_text(f"🌍 УТРЕННИЙ ОБЗОР НОВОСТЕЙ\n{today_str}\n\n🌐 Мировые события + 🇺🇦 Украина\nГотовлю {total} новостей...")

    send_news_block(world, 1, total)
    if ukraine:
        tg_text("🇺🇦 НОВОСТИ УКРАИНЫ")
        send_news_block(ukraine, len(world) + 1, total)

# ── ДНЕВНОЙ БЛОК 14:00 — 4 мировых ──
elif BLOCK == "midday":
    world = get_world_news(4)
    total = len(world)

    tg_text(f"🌍 ДНЕВНОЙ ОБЗОР НОВОСТЕЙ\n{today_str}\n\nГотовлю {total} новости...")

    send_news_block(world, 1, total)

# ── ВЕЧЕРНИЙ БЛОК 19:00 — 4 мировых + 3 Украина + прощание ──
elif BLOCK == "evening":
    world   = get_world_news(4)
    ukraine = get_ukraine_news(3)
    total   = len(world) + len(ukraine)

    tg_text(f"🌍 ВЕЧЕРНИЙ ОБЗОР НОВОСТЕЙ\n{today_str}\n\n🌐 Мировые события + 🇺🇦 Украина\nГотовлю {total} новостей...")

    send_news_block(world, 1, total)
    if ukraine:
        tg_text("🇺🇦 НОВОСТИ УКРАИНЫ")
        send_news_block(ukraine, len(world) + 1, total)

    tg_text("✅ Это все новости на сегодня. Хорошего вечера! 🙂")

print("Готово!")




  
