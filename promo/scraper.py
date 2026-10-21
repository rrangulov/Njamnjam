"""Reads public Telegram channel previews from https://t.me/s/USERNAME."""
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from db import MSK

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([^/?#\s]+)", re.I)
CONTACT_RE = re.compile(
    r"@[A-Za-z][A-Za-z0-9_]{3,31}"
    r"|(?:https?://)?(?:t\.me|telegram\.me)/[A-Za-z0-9_+/]+"
    r"|[\w.+-]+@[\w-]+\.[\w.]+"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}


class FetchError(Exception):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def normalize(raw: str) -> str | None:
    """'@name', 't.me/name', 'https://t.me/s/name/123' -> 'name'; None if not a public channel."""
    s = raw.strip().strip(",;")
    if not s:
        return None
    m = LINK_RE.search(s)
    if m:
        s = m.group(1)
    s = s.lstrip("@")
    if s.startswith("+") or s.lower() == "joinchat":
        return None  # private invite link
    return s.lower() if USERNAME_RE.match(s) else None


def parse_count(text: str) -> int | None:
    t = text.strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    m = re.match(r"^([\d.]+)([KMkm]?)$", t)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(n * mult)


def find_contacts(text: str, own: str) -> list[str]:
    seen = []
    for c in CONTACT_RE.findall(text or ""):
        c = c.rstrip(".,)")
        if normalize(c) == own:
            continue
        if c not in seen:
            seen.append(c)
    return seen


ROLE_HINTS = [
    ("реклам", "реклама"),
    ("сотруднич", "сотрудничество"),
    ("бартер", "сотрудничество"),
    ("админ", "админ"),
    ("связь", "связь"),
    ("вопрос", "связь"),
    ("консульт", "консультации"),
    ("запис", "консультации"),
]
HANDLE_RE = re.compile(r"(?:@|(?:https?://)?(?:t\.me|telegram\.me)/)([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_/])")


def find_admins(description: str, own: str) -> list[tuple[str, str]]:
    """People to contact: @handles and t.me/handle links from the description, with a guessed role.
    Bots, the channel itself and private invite links are skipped."""
    found: dict[str, str] = {}
    for line in (description or "").splitlines():
        low = line.lower()
        role = next((r for hint, r in ROLE_HINTS if hint in low), "контакт")
        for m in HANDLE_RE.finditer(line):
            start = m.start()
            if line[start] == "@" and start > 0 and (line[start - 1].isalnum() or line[start - 1] in "._-"):
                continue  # part of an e-mail address
            handle = m.group(1).lower()
            if handle == own or handle.endswith("bot") or handle in ("joinchat", "addlist", "share", "s"):
                continue
            if handle not in found or found[handle] == "контакт":
                found[handle] = role
    return list(found.items())


def parse(html: str, username: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    if not title_el:
        # t.me/s/ redirects to the plain page for users, groups, private or missing channels
        if soup.select_one(".tgme_page_title"):
            raise FetchError("private", "Не публичный канал (пользователь, группа или закрыт предпросмотр)")
        raise FetchError("not_found", "Канал не найден")

    desc_el = soup.select_one(".tgme_channel_info_description")
    description = desc_el.get_text("\n", strip=True) if desc_el else ""

    subscribers = None
    for counter in soup.select(".tgme_channel_info_counter"):
        kind = counter.select_one(".counter_type")
        value = counter.select_one(".counter_value")
        if kind and value and kind.get_text(strip=True).startswith("subscriber"):
            subscribers = parse_count(value.get_text())

    posts = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        try:
            post_id = int(msg["data-post"].rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        time_el = msg.select_one(".tgme_widget_message_date time[datetime]")
        views_el = msg.select_one(".tgme_widget_message_views")
        posted_at = None
        if time_el:
            try:
                dt = datetime.fromisoformat(time_el["datetime"])
                posted_at = dt.astimezone(MSK).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        posts.append({
            "id": post_id,
            "posted_at": posted_at,
            "views": views_el.get_text(strip=True) if views_el else None,
            "text": text_el.get_text("\n", strip=True) if text_el else "[медиа без текста]",
        })
    posts.sort(key=lambda p: p["id"])

    return {
        "title": title_el.get_text(strip=True),
        "description": description,
        "subscribers": subscribers,
        "contacts": find_contacts(description, username),
        "admins": find_admins(description, username),
        "posts": posts[-20:],
    }


async def fetch(username: str, proxy: str | None = None) -> dict:
    url = f"https://t.me/s/{username}"
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, proxy=proxy or None, timeout=20, follow_redirects=True
        ) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        raise FetchError("error", f"Сеть: {type(e).__name__}: {e}")
    if r.status_code != 200:
        raise FetchError("error", f"HTTP {r.status_code}")
    return parse(r.text, username)
