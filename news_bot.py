import os
import requests
from datetime import datetime

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY   = os.environ["NEWS_API_KEY"]

today_str = datetime.now().strftime("%d.%m.%Y")


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
            json={
                "chat_id": TG_CHAT_ID,
                "photo": image_url
            },
            timeout=15
        )
        return resp.status_code == 200
    except Exception:
        return False


def gemini_analyze(title, description):
    models = [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]

    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
Описание: {description}

Напиши ответ на русском языке в таком виде:

Заголовок: (переведи на русский)
Суть: (2-3 предложения — что произошло и почему важно)
Прогноз: (2-3 предложения — возможные последствия для мира или России)

Только чистый текст, никаких звёздочек и специальных символов."""

    for model in models:
        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model}:generateContent?key={GEMINI_KEY}"
            )
            resp = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.6, "maxOutputTokens": 400}
            }, timeout=30)

            data = resp.json()

            if "candidates" in data:
                print(f"Работает модель: {model}")
                return data["candidates"][0]["content"]["parts"][0]["text"]
            else:
                error = data.get("error", {}).get("message", "")
                print(f"Модель {model} не сработала: {error}")
                continue

        except Exception as e:
            print(f"Ошибка с моделью {model}: {e}")
            continue

    return "Не удалось получить анализ — все модели Gemini недоступны."


# 1. Получаем новости
try:
    news_resp = requests.get(
        "https://newsapi.org/v2/top-headlines",
        params={
            "apiKey": NEWS_KEY,
            "language": "en",
            "pageSize": 10,
            "category": "general"
        },
        timeout=15
    )
    result = news_resp.json()
except Exception as e:
    tg_text(f"Не удалось получить новости: {e}")
    exit()

all_articles = result.get("articles", [])

articles = [
    a for a in all_articles
    if a.get("title") and a.get("title") != "[Removed]"
    and a.get("description") and a.get("description") != "[Removed]"
]

if not articles:
    tg_text(f"Нет доступных новостей. Ответ API: {str(result)[:500]}")
    exit()

articles = articles[:5]

# 2. Заголовок рассылки
tg_text(f"ОБЗОР МИРОВЫХ НОВОСТЕЙ\n{today_str}\n\nГотовлю топ-{len(articles)} событий дня...")

# 3. Каждая новость
for i, article in enumerate(articles, 1):
    title       = article.get("title", "").split(" - ")[0].strip()
    description = article.get("description", "No description")
    image_url   = article.get("urlToImage")

    analysis = gemini_analyze(title, description)
    message  = f"Новость {i} из {len(articles)}\n\n{analysis}"

    # Сначала картинка отдельно, потом полный текст отдельно
    if image_url:
        tg_photo(image_url)

    tg_text(message)

# 4. Финал
tg_text("Это все главные события дня. Хорошего дня!")
print("Готово!")




  
