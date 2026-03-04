Скрипт получает топ-5 мировых новостей с картинками,
# просит Gemini перевести заголовок на русский,
# написать суть и прогноз — и отправляет всё в Telegram.

import os
import requests
from datetime import datetime, timedelta

# Секретные ключи (берём из настроек GitHub)
GEMINI_KEY  = os.environ["GEMINI_API_KEY"]
TG_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
NEWS_KEY    = os.environ["NEWS_API_KEY"]

today_str  = datetime.now().strftime("%d.%m.%Y")
yesterday  = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


# ── Вспомогательные функции ───────────────────────

def tg_text(text):
    """Отправляет текстовое сообщение в Telegram"""
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        },
        timeout=15
    )

def tg_photo(image_url, caption):
    """Отправляет картинку с подписью в Telegram"""
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
        json={
            "chat_id": TG_CHAT_ID,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "Markdown"
        },
        timeout=15
    )
    return resp.status_code == 200

def gemini_analyze(title, description):
    """Просит Gemini перевести заголовок и написать суть + прогноз по-русски"""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"
    )
    prompt = f"""Вот новость на английском языке.
Заголовок: {title}
Описание: {description}

Напиши ответ СТРОГО на русском языке в следующем формате:

🗞 *Заголовок:* (переведи заголовок на русский, адаптируй естественно)
📌 *Суть:* (2-3 предложения — что произошло и почему это важно)
🔮 *Прогноз:* (2-3 предложения — к чему это может привести, какие последствия для мира или России)

Пиши нейтрально, информативно, без вводных слов и приветствий.
Весь ответ должен быть исключительно на русском языке."""

    resp = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 400
        }
    }, timeout=30)

    data = resp.json()

    # Если Gemini вернул ошибку — показываем её текст
    if "candidates" not in data:
        error_msg = data.get("error", {}).get("message", str(data))
        return f"⚠️ Ошибка Gemini: {error_msg}"

    return data["candidates"][0]["content"]["parts"][0]["text"]


# ── Основной код ──────────────────────────────────

# 1. Получаем топ-5 мировых новостей за вчера
news_resp = requests.get(
    "https://newsapi.org/v2/top-headlines",
    params={
        "apiKey": NEWS_KEY,
        "language": "en",
        "pageSize": 5,
        "from": yesterday,
        "sortBy": "popularity",
        "category": "general"
    },
    timeout=15
)
articles = news_resp.json().get("articles", [])

if not articles:
    tg_text("⚠️ Сегодня не удалось получить новости. Попробуй позже.")
    exit()

# 2. Отправляем заголовок рассылки
tg_text(
    f"🌍 *ОБЗОР МИРОВЫХ НОВОСТЕЙ*\n"
    f"📅 {today_str}\n\n"
    f"Готовлю топ-{len(articles)} событий дня..."
)

# 3. Обрабатываем каждую новость
for i, article in enumerate(articles, 1):
    title       = article.get("title", "").split(" - ")[0].strip()
    description = article.get("description") or "No description"
    image_url   = article.get("urlToImage")

    # Gemini переводит и анализирует (всё по-русски)
    analysis = gemini_analyze(title, description)

    # Собираем финальное сообщение
    message = f"*Новость {i} из {len(articles)}*\n\n{analysis}"

    # Отправляем: картинка + текст (или только текст если картинки нет)
    if image_url:
        ok = tg_photo(image_url, message)
        if not ok:
            tg_text(message)  # картинка недоступна — шлём без неё
    else:
        tg_text(message)

# 4. Финальное сообщение
tg_text("✅ Это все главные события дня. Хорошего дня! 🙂")
print("Обзор успешно отправлен!")
