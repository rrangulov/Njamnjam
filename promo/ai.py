"""Channel analysis through any OpenAI-compatible chat API."""
import json
import re

import httpx

PRODUCT = (
    "Нямням (@ruslan_njamnjam_bot) — Telegram-бот: присылаешь фото еды, бот за несколько секунд "
    "отвечает калориями, белками, жирами и углеводами (КБЖУ), ведёт дневник питания, показывает "
    "норму белка по весу, работает и в личке, и в групповых чатах. Бесплатно до 6 расчётов в день."
)

PROMPT = """Ты помогаешь продвигать Telegram-бота в небольших Telegram-каналах.

Продукт: {product}

Ниже данные канала @{username}.
Название: {title}
Подписчиков: {subscribers}
Описание:
{description}

Последние посты (старые сверху):
{posts}

Оцени, насколько этот канал подходит, чтобы рассказать его аудитории про бота.
Ответь ТОЛЬКО JSON-объектом без пояснений вокруг, поля:
{{
  "topic": "тематика канала, коротко",
  "audience": "кто читает, коротко",
  "language": "язык постов",
  "activity": "как часто выходят посты, живой ли канал",
  "score": целое 0-100, насколько канал подходит для рассказа о боте,
  "reason": "почему такая оценка, 1-3 предложения",
  "approach": "как лучше зайти: платная реклама, взаимопиар, личное сообщение админу, комментарии и т.п.",
  "hook": "какой угол подачи бота зайдёт именно этой аудитории, 1-2 предложения",
  "ad_info": "есть ли признаки, что канал продаёт рекламу, контакты/прайс, если видны",
  "risks": "риски и минусы, коротко"
}}"""


class AIError(Exception):
    pass


def build_prompt(ch, posts) -> str:
    lines = []
    for p in sorted(posts, key=lambda p: p["post_id"]):
        text = (p["text"] or "").replace("\n", " ")
        lines.append(f"[{p['posted_at']}, просмотры {p['views'] or '?'}] {text[:600]}")
    return PROMPT.format(
        product=PRODUCT,
        username=ch["username"],
        title=ch["title"] or "?",
        subscribers=ch["subscribers"] if ch["subscribers"] is not None else "?",
        description=ch["description"] or "(нет)",
        posts="\n".join(lines) or "(нет постов)",
    )


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError(f"Модель ответила не JSON: {text[:200]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise AIError(f"Не удалось разобрать JSON: {e}")
    try:
        data["score"] = max(0, min(100, int(data.get("score"))))
    except (TypeError, ValueError):
        data["score"] = None
    return data


async def analyze(ch, posts, base_url: str, api_key: str, model: str) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(ch, posts)}],
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as e:
        raise AIError(f"Сеть: {type(e).__name__}")
    if r.status_code != 200:
        raise AIError(f"API ответил {r.status_code}: {r.text[:200]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError(f"Неожиданный ответ API: {r.text[:200]}")
    return parse_json(content)
