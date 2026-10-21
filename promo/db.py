"""SQLite storage for the promo panel."""
import json
import sqlite3
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

STATUSES = {
    "new": "Новый",
    "fit": "Подходит",
    "unfit": "Не подходит",
    "contacted": "Написали",
    "replied": "Ответили",
    "agreed": "Договорились",
    "declined": "Отказ",
}

ADMIN_STATUSES = {
    "none": "Не писали",
    "contacted": "Написали",
    "replied": "Ответил",
    "agreed": "Договорились",
    "declined": "Отказ",
    "skip": "Не писать",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    title TEXT,
    description TEXT,
    subscribers INTEGER,
    contacts TEXT,            -- JSON list
    last_post_at TEXT,
    fetch_state TEXT,         -- ok / not_found / private / error
    fetch_error TEXT,
    fetched_at TEXT,
    ai_score INTEGER,
    ai_result TEXT,           -- JSON
    ai_error TEXT,
    analyzed_at TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    post_id INTEGER NOT NULL,
    posted_at TEXT,
    views TEXT,
    text TEXT,
    PRIMARY KEY (channel_id, post_id)
);
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    old TEXT,
    new TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'none',
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    contacted_at TEXT
);
CREATE TABLE IF NOT EXISTS channel_admins (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'контакт',
    source TEXT NOT NULL DEFAULT 'auto',  -- auto (from description) / manual
    PRIMARY KEY (channel_id, admin_id)
);
CREATE TABLE IF NOT EXISTS admin_log (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    old TEXT,
    new TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0
);
"""


def now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # channels
    def add_channel(self, username: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO channels (username, added_at) VALUES (?, ?)",
            (username, now()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def channel(self, cid: int):
        return self.conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()

    def channels(self, status: str | None = None, sort: str = "score"):
        order = {
            "score": "ai_score IS NULL, ai_score DESC, id",
            "subs": "subscribers IS NULL, subscribers DESC, id",
            "added": "id DESC",
            "name": "username",
        }.get(sort, "id")
        if status:
            q = f"SELECT * FROM channels WHERE status = ? ORDER BY {order}"
            return self.conn.execute(q, (status,)).fetchall()
        return self.conn.execute(f"SELECT * FROM channels ORDER BY {order}").fetchall()

    def status_counts(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM channels GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def save_fetch(self, cid: int, info: dict):
        self.conn.execute(
            """UPDATE channels SET title=?, description=?, subscribers=?, contacts=?,
               last_post_at=?, fetch_state='ok', fetch_error=NULL, fetched_at=? WHERE id=?""",
            (
                info["title"], info["description"], info["subscribers"],
                json.dumps(info["contacts"], ensure_ascii=False),
                info["posts"][-1]["posted_at"] if info["posts"] else None,
                now(), cid,
            ),
        )
        self.conn.execute("DELETE FROM posts WHERE channel_id = ?", (cid,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO posts VALUES (?, ?, ?, ?, ?)",
            [(cid, p["id"], p["posted_at"], p["views"], p["text"]) for p in info["posts"]],
        )
        self.conn.commit()

    def save_fetch_error(self, cid: int, state: str, error: str):
        self.conn.execute(
            "UPDATE channels SET fetch_state=?, fetch_error=?, fetched_at=? WHERE id=?",
            (state, error, now(), cid),
        )
        self.conn.commit()

    def posts(self, cid: int):
        return self.conn.execute(
            "SELECT * FROM posts WHERE channel_id = ? ORDER BY post_id DESC", (cid,)
        ).fetchall()

    def save_analysis(self, cid: int, result: dict):
        score = result.get("score")
        self.conn.execute(
            "UPDATE channels SET ai_score=?, ai_result=?, ai_error=NULL, analyzed_at=? WHERE id=?",
            (int(score) if isinstance(score, (int, float)) else None,
             json.dumps(result, ensure_ascii=False), now(), cid),
        )
        self.conn.commit()

    def save_analysis_error(self, cid: int, error: str):
        self.conn.execute("UPDATE channels SET ai_error=? WHERE id=?", (error, cid))
        self.conn.commit()

    def set_status(self, cid: int, status: str, note: str):
        ch = self.channel(cid)
        if ch["status"] != status:
            self.conn.execute(
                "INSERT INTO status_log (channel_id, at, old, new, note) VALUES (?, ?, ?, ?, ?)",
                (cid, now(), ch["status"], status, note),
            )
        self.conn.execute("UPDATE channels SET status=?, note=? WHERE id=?", (status, note, cid))
        self.conn.commit()

    def status_log(self, cid: int):
        return self.conn.execute(
            "SELECT * FROM status_log WHERE channel_id = ? ORDER BY id DESC", (cid,)
        ).fetchall()

    def delete_channel(self, cid: int):
        self.conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
        self.conn.commit()

    # admins
    def admin_id(self, username: str) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO admins (username, added_at) VALUES (?, ?)", (username, now())
        )
        return self.conn.execute("SELECT id FROM admins WHERE username = ?", (username,)).fetchone()["id"]

    def sync_auto_admins(self, cid: int, found: list[tuple[str, str]]):
        """Replace auto-found admin links of a channel; manual links stay."""
        self.conn.execute("DELETE FROM channel_admins WHERE channel_id = ? AND source = 'auto'", (cid,))
        for username, role in found:
            self.conn.execute(
                "INSERT OR IGNORE INTO channel_admins (channel_id, admin_id, role, source) VALUES (?, ?, ?, 'auto')",
                (cid, self.admin_id(username), role),
            )
        self.conn.commit()

    def link_admin(self, cid: int, username: str, role: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO channel_admins (channel_id, admin_id, role, source) VALUES (?, ?, ?, 'manual')",
            (cid, self.admin_id(username), role or "контакт"),
        )
        self.conn.commit()

    def unlink_admin(self, cid: int, aid: int):
        self.conn.execute("DELETE FROM channel_admins WHERE channel_id = ? AND admin_id = ?", (cid, aid))
        self.conn.commit()

    def admin(self, aid: int):
        return self.conn.execute("SELECT * FROM admins WHERE id = ?", (aid,)).fetchone()

    def admins(self, status: str | None = None, q: str = ""):
        sql = """SELECT a.*, COUNT(ca.channel_id) AS n_channels,
                   GROUP_CONCAT(c.username, ' ') AS channel_names,
                   MAX(c.ai_score) AS best_score,
                   GROUP_CONCAT(DISTINCT ca.role) AS roles
                 FROM admins a
                 LEFT JOIN channel_admins ca ON ca.admin_id = a.id
                 LEFT JOIN channels c ON c.id = ca.channel_id
                 WHERE (? IS NULL OR a.status = ?)
                   AND (? = '' OR a.username LIKE ? OR a.name LIKE ? OR a.note LIKE ?)
                 GROUP BY a.id
                 ORDER BY best_score IS NULL, best_score DESC, a.username"""
        like = f"%{q}%"
        return self.conn.execute(sql, (status, status, q, like, like, like)).fetchall()

    def admin_status_counts(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM admins GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def admin_channels(self, aid: int):
        return self.conn.execute(
            """SELECT c.*, ca.role, ca.source FROM channel_admins ca JOIN channels c ON c.id = ca.channel_id
               WHERE ca.admin_id = ? ORDER BY c.ai_score IS NULL, c.ai_score DESC""",
            (aid,),
        ).fetchall()

    def channel_admins(self, cid: int):
        return self.conn.execute(
            """SELECT a.*, ca.role, ca.source FROM channel_admins ca JOIN admins a ON a.id = ca.admin_id
               WHERE ca.channel_id = ? ORDER BY a.username""",
            (cid,),
        ).fetchall()

    def save_admin(self, aid: int, name: str, status: str, note: str):
        a = self.admin(aid)
        contacted_at = a["contacted_at"]
        if a["status"] != status:
            self.conn.execute(
                "INSERT INTO admin_log (admin_id, at, old, new, note) VALUES (?, ?, ?, ?, ?)",
                (aid, now(), a["status"], status, note),
            )
            if status == "contacted":
                contacted_at = now()
        self.conn.execute(
            "UPDATE admins SET name=?, status=?, note=?, contacted_at=? WHERE id=?",
            (name, status, note, contacted_at, aid),
        )
        self.conn.commit()

    def admin_log(self, aid: int):
        return self.conn.execute(
            "SELECT * FROM admin_log WHERE admin_id = ? ORDER BY id DESC", (aid,)
        ).fetchall()

    def delete_admin(self, aid: int):
        self.conn.execute("DELETE FROM admins WHERE id = ?", (aid,))
        self.conn.commit()

    # AI daily limit
    def ai_used_today(self) -> int:
        day = datetime.now(MSK).strftime("%Y-%m-%d")
        row = self.conn.execute("SELECT requests FROM ai_usage WHERE day = ?", (day,)).fetchone()
        return row["requests"] if row else 0

    def ai_count(self):
        day = datetime.now(MSK).strftime("%Y-%m-%d")
        self.conn.execute(
            "INSERT INTO ai_usage (day, requests) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET requests = requests + 1",
            (day,),
        )
        self.conn.commit()
