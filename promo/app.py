"""Нямням promo panel: Telegram channel list, scraping and AI analysis."""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import ai
import scraper
from db import ADMIN_STATUSES, DB, STATUSES, now

BASE = Path(__file__).parent


def load_env(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:  # missing, or root-only when systemd passes EnvironmentFile
        return
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(BASE / ".env")

PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
TG_PROXY = os.environ.get("TG_PROXY", "")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "")
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "100"))
FETCH_PAUSE = float(os.environ.get("FETCH_PAUSE", "3"))
SESSION_DAYS = 30

if not PANEL_PASSWORD:
    raise SystemExit("PANEL_PASSWORD is not set in .env")

db = DB(os.environ.get("DB_PATH", str(BASE / "promo.db")))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals.update(STATUSES=STATUSES, ADMIN_STATUSES=ADMIN_STATUSES)

# fill the admin directory from descriptions already collected (idempotent)
for _c in db.channels(sort="added"):
    if _c["fetch_state"] == "ok":
        db.sync_auto_admins(_c["id"], scraper.find_admins(_c["description"], _c["username"]))
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None

# background bulk job state (one at a time)
job = {"running": False, "kind": "", "done": 0, "total": 0, "current": "", "log": []}
login_fails: dict[str, list[float]] = {}


def go(path: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(msg)}" if msg else path, status_code=303)


def ai_ready() -> bool:
    return bool(AI_BASE_URL and AI_API_KEY and AI_MODEL)


# --- auth -----------------------------------------------------------------

def sign(value: str) -> str:
    mac = hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def check_cookie(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    value, mac = cookie.rsplit(".", 1)
    good = hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good):
        return False
    try:
        return float(value.split(":", 1)[1]) > time.time()
    except (IndexError, ValueError):
        return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path not in ("/login", "/static/style.css") and not check_cookie(
        request.cookies.get("session")
    ):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.client.host if request.client else "?"
    recent = [t for t in login_fails.get(ip, []) if t > time.time() - 600]
    login_fails[ip] = recent
    if len(recent) >= 5:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Слишком много попыток, подождите 10 минут"}, status_code=429
        )
    ok_user = hmac.compare_digest(username.encode(), PANEL_USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), PANEL_PASSWORD.encode())
    if not (ok_user and ok_pass):
        recent.append(time.time())
        await asyncio.sleep(1)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401
        )
    login_fails.pop(ip, None)
    resp = RedirectResponse("/", status_code=303)
    expires = time.time() + SESSION_DAYS * 86400
    resp.set_cookie(
        "session", sign(f"{PANEL_USER}:{expires}"), max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="strict",
    )
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# --- work -----------------------------------------------------------------

async def refresh_one(cid: int) -> str:
    ch = db.channel(cid)
    try:
        info = await scraper.fetch(ch["username"], TG_PROXY)
    except scraper.FetchError as e:
        db.save_fetch_error(cid, e.state, str(e))
        return f"@{ch['username']}: {e}"
    db.save_fetch(cid, info)
    db.sync_auto_admins(cid, info["admins"])
    return f"@{ch['username']}: ок, постов {len(info['posts'])}"


async def analyze_one(cid: int) -> str:
    ch = db.channel(cid)
    if not ai_ready():
        return "нет ключа ИИ"
    if db.ai_used_today() >= AI_DAILY_LIMIT:
        return f"дневной лимит ИИ ({AI_DAILY_LIMIT}) исчерпан"
    if ch["fetch_state"] != "ok":
        return f"@{ch['username']}: сначала обновите канал"
    db.ai_count()
    try:
        result = await ai.analyze(ch, db.posts(cid), AI_BASE_URL, AI_API_KEY, AI_MODEL)
    except ai.AIError as e:
        db.save_analysis_error(cid, str(e))
        return f"@{ch['username']}: ошибка ИИ: {e}"
    db.save_analysis(cid, result)
    return f"@{ch['username']}: оценка {result.get('score')}"


async def run_job(kind: str, ids: list[int]):
    job.update(running=True, kind=kind, done=0, total=len(ids), current="", log=[])
    try:
        for i, cid in enumerate(ids):
            ch = db.channel(cid)
            if not ch:
                continue
            job["current"] = ch["username"]
            msg = await (refresh_one(cid) if kind == "refresh" else analyze_one(cid))
            job["log"].insert(0, msg)
            job["done"] = i + 1
            if kind == "analyze" and ("лимит" in msg or "нет ключа" in msg):
                break
            if i + 1 < len(ids):
                await asyncio.sleep(FETCH_PAUSE if kind == "refresh" else 1)
    finally:
        job.update(running=False, current="")
        job["log"] = job["log"][:50]
        job["finished_at"] = now()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, status: str = "", sort: str = "score", msg: str = ""):
    return templates.TemplateResponse(request, "index.html", {
        "channels": db.channels(status or None, sort),
        "counts": db.status_counts(),
        "status": status,
        "sort": sort,
        "msg": msg,
        "job": job,
        "ai_ready": ai_ready(),
        "ai_used": db.ai_used_today(),
        "ai_limit": AI_DAILY_LIMIT,
    })


@app.post("/add")
async def add(text: str = Form("")):
    added, dup, bad = 0, 0, []
    for raw in text.replace(",", "\n").split():
        name = scraper.normalize(raw)
        if not name:
            bad.append(raw)
        elif db.add_channel(name):
            added += 1
        else:
            dup += 1
    msg = f"Добавлено: {added}, уже были: {dup}"
    if bad:
        msg += f", не распознано: {', '.join(bad[:10])}"
    return go("/", msg)


@app.post("/refresh-all")
async def refresh_all(bg: BackgroundTasks, only_new: str = Form("")):
    if not job["running"]:
        ids = [c["id"] for c in db.channels(sort="added") if not only_new or not c["fetched_at"]]
        bg.add_task(run_job, "refresh", ids[::-1])
    return RedirectResponse("/", status_code=303)


@app.post("/analyze-all")
async def analyze_all(bg: BackgroundTasks):
    if not job["running"] and ai_ready():
        ids = [c["id"] for c in db.channels(sort="added")
               if c["fetch_state"] == "ok" and not c["analyzed_at"]]
        bg.add_task(run_job, "analyze", ids[::-1])
    return RedirectResponse("/", status_code=303)


@app.get("/c/{cid}", response_class=HTMLResponse)
async def card(request: Request, cid: int, msg: str = ""):
    ch = db.channel(cid)
    if not ch:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "channel.html", {
        "ch": ch,
        "posts": db.posts(cid),
        "log": db.status_log(cid),
        "admins": db.channel_admins(cid),
        "contacts": json.loads(ch["contacts"]) if ch["contacts"] else [],
        "analysis": json.loads(ch["ai_result"]) if ch["ai_result"] else None,
        "ai_ready": ai_ready(),
        "msg": msg,
    })


@app.post("/c/{cid}/refresh")
async def card_refresh(cid: int):
    msg = await refresh_one(cid)
    return go(f"/c/{cid}", msg)


@app.post("/c/{cid}/analyze")
async def card_analyze(cid: int):
    msg = await analyze_one(cid)
    return go(f"/c/{cid}", msg)


@app.post("/c/{cid}/status")
async def card_status(cid: int, status: str = Form(...), note: str = Form("")):
    if status in STATUSES and db.channel(cid):
        db.set_status(cid, status, note.strip())
    return go(f"/c/{cid}", "Сохранено")


@app.post("/c/{cid}/delete")
async def card_delete(cid: int):
    db.delete_channel(cid)
    return go("/", "Канал удалён")


@app.post("/c/{cid}/admins/add")
async def card_admin_add(cid: int, username: str = Form(""), role: str = Form("")):
    name = scraper.normalize(username)
    if not name or not db.channel(cid):
        return go(f"/c/{cid}", "Не похоже на @username")
    db.link_admin(cid, name, role.strip())
    return go(f"/c/{cid}", f"@{name} добавлен в админы канала")


@app.post("/c/{cid}/admins/{aid}/unlink")
async def card_admin_unlink(cid: int, aid: int):
    db.unlink_admin(cid, aid)
    return go(f"/c/{cid}", "Админ отвязан от канала")


@app.get("/admins", response_class=HTMLResponse)
async def admins_page(request: Request, status: str = "", q: str = "", msg: str = ""):
    return templates.TemplateResponse(request, "admins.html", {
        "admins": db.admins(status or None, q.strip()),
        "counts": db.admin_status_counts(),
        "status": status,
        "q": q,
        "msg": msg,
    })


@app.get("/a/{aid}", response_class=HTMLResponse)
async def admin_card(request: Request, aid: int, msg: str = ""):
    a = db.admin(aid)
    if not a:
        return RedirectResponse("/admins", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {
        "a": a,
        "channels": db.admin_channels(aid),
        "log": db.admin_log(aid),
        "msg": msg,
    })


@app.post("/a/{aid}")
async def admin_save(aid: int, name: str = Form(""), status: str = Form(...), note: str = Form("")):
    if status in ADMIN_STATUSES and db.admin(aid):
        db.save_admin(aid, name.strip(), status, note.strip())
    return go(f"/a/{aid}", "Сохранено")


@app.post("/a/{aid}/delete")
async def admin_delete(aid: int):
    db.delete_admin(aid)
    return go("/admins", "Админ удалён из справочника")


@app.get("/admins.csv")
async def export_admins():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["админ", "ссылка", "имя", "статус", "каналы", "роли", "лучшая оценка канала",
                "написали", "заметка", "добавлен"])
    for a in db.admins():
        w.writerow([
            "@" + a["username"], f"https://t.me/{a['username']}", a["name"],
            ADMIN_STATUSES.get(a["status"], a["status"]),
            " ".join("@" + n for n in (a["channel_names"] or "").split()), a["roles"] or "",
            a["best_score"] if a["best_score"] is not None else "", a["contacted_at"] or "",
            a["note"], a["added_at"],
        ])
    return Response(
        "\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=nyamnyam-admins.csv"},
    )


@app.get("/export.csv")
async def export():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["канал", "ссылка", "название", "подписчики", "последний пост", "статус",
                "оценка ИИ", "тематика", "как зайти", "контакты", "заметка", "добавлен"])
    for c in db.channels(sort="score"):
        a = json.loads(c["ai_result"]) if c["ai_result"] else {}
        w.writerow([
            "@" + c["username"], f"https://t.me/{c['username']}", c["title"] or "",
            c["subscribers"] or "", c["last_post_at"] or "", STATUSES.get(c["status"], c["status"]),
            c["ai_score"] if c["ai_score"] is not None else "", a.get("topic", ""),
            a.get("approach", ""), ", ".join(json.loads(c["contacts"]) if c["contacts"] else []),
            c["note"], c["added_at"],
        ])
    # BOM so Excel opens Cyrillic correctly
    return Response(
        "\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=nyamnyam-channels.csv"},
    )


@app.get("/static/style.css")
async def style():
    return Response((BASE / "static" / "style.css").read_text(encoding="utf-8"), media_type="text/css")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "3003")),
                proxy_headers=False, access_log=False)
