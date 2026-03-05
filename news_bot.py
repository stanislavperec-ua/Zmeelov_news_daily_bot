import os
import time
import requests
from datetime import datetime

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY   = os.environ["NEWS_API_KEY"]

today_str = datetime.now().strftime("%d.%m.%Y")

EXCLUDE_KEYWORDS = [
    "wwe", "nfl", "nba", "spoiler", "wrestling", "celebrity",
    "kardashian", "taylor swift", "oscar", "grammy", "box office",
    "recipe", "horoscope", "zodiac"
]

# Список моделей — пробуем по очереди если предыдущая не работает
MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
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


def call_gemini(model, prompt):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent?key={GEMINI_KEY}"
    )
    resp = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 1000
        }
    }, timeout=60)

    data = resp.json()

    if "candidates" in data:
        return data["candidates"][0]["content"]["parts"][0]["text"]

    error_msg = data.get("error", {}).get("message", "Неизвестная ошибка")
    raise Exception(error_msg)


def gemini_analyze(title, description):
    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
Описание: {description}

Напиши полный ответ на русском языке в таком формате:

Заголовок: (переведи на русский)

Суть: (напиши 2-3 полных предложения о том что произошло и почему важно)

Прогноз: (напиши 2-3 полных предложения о возможных последствиях для мира или России)

Каждый раздел должен быть полным и завершённым. Только чистый текст без звёздочек."""

    for model in MODELS:
        print(f"Пробую модель: {model}")
        for attempt in range(1, 3):
            try:
                result = call_gemini(model, prompt)
                print(f"Успешно! Модель {model}, получено {len(result)} символов")
                return result

            except Exception as e:
                error = str(e)
                print(f"Ошибка ({model}, попытка {attempt}): {error[:120]}")

                # Если лимит запросов — ждём и повторяем эту же модель
                if "quota" in error.lower() or "rate" in error.lower():
                    print("Лимит запросов, ждём 60 секунд...")
                    time.sleep(60)
                    continue

                # Если модель не найдена — сразу переходим к следующей
                if "not found" in error.lower() or "not supported" in error.lower():
                    print(f"Модель {model} недоступна, переходим к следующей")
                    break

                # Другая ошибка — пробуем ещё раз
                time.sleep(10)
                continue

    return "Анализ недоступен — все модели Gemini не отвечают."


# 1. Получаем новости
try:
    news_resp = requests.get(
        "https://newsapi.org/v2/top-headlines",
        params={
            "apiKey": NEWS_KEY,
            "language": "en",
            "pageSize": 20,
            "category": "general"
        },
        timeout=15
    )
    result = news_resp.json()
except Exception as e:
    tg_text(f"Не удалось получить новости: {e}")
    exit()

all_articles = result.get("articles", [])
articles = [a for a in all_articles if is_relevant(a)]

print(f"Всего: {len(all_articles)}, после фильтра: {len(articles)}")

if not articles:
    tg_text("Нет подходящих новостей сегодня.")
    exit()

articles = articles[:7]

# 2. Заголовок
tg_text(f"ОБЗОР МИРОВЫХ НОВОСТЕЙ\n{today_str}\n\nГотовлю топ-{len(articles)} событий дня...")

# 3. Каждая новость
for i, article in enumerate(articles, 1):
    title       = article.get("title", "").split(" - ")[0].strip()
    description = article.get("description", "")
    image_url   = article.get("urlToImage")

    print(f"\nНовость {i}: {title[:60]}")

    analysis = gemini_analyze(title, description)
    message  = f"Новость {i} из {len(articles)}\n\n{analysis}"

    if image_url:
        tg_photo(image_url)

    tg_text(message)

    if i < len(articles):
        print("Пауза 60 секунд...")
        time.sleep(60)

# 4. Финал
tg_text("Это все главные события дня. Хорошего дня!")
print("Готово!")




  
