"""SQLite: заявки, диалоги по машине, расход токенов, журнал событий.

Заглушка ТОиР не отдаёт список заявок и после перезапуска нумерует с TKT-0001,
поэтому заявки (состав и номер из ТОиР) храним у себя.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from app import settings

_lock = threading.Lock()
Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL, session_id TEXT NOT NULL,
    status TEXT NOT NULL,           -- draft | sent | error
    toir_ticket_id TEXT,            -- номер, который вернул ТОиР
    payload TEXT NOT NULL,          -- JSON заявки по контракту ТОиР
    extra TEXT,                     -- JSON: обоснование приоритета, правки механика
    created_at REAL NOT NULL, sent_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL, vehicle_id TEXT NOT NULL, item TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_items_cv ON chat_items(client_id, vehicle_id);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT, step TEXT, model TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, seconds REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT, vehicle_id TEXT, actor TEXT, kind TEXT, data TEXT, created_at REAL
);
""")
_db.commit()


def _exec(sql: str, args=()) -> sqlite3.Cursor:
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur


def _rows(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _db.execute(sql, args).fetchall()]


def log_event(kind: str, data: dict | None = None, *, actor: str = "agent",
              request_id: str | None = None, vehicle_id: str | None = None) -> None:
    """Журнал действий агента и пользователя (для анализа работы системы)."""
    _exec("INSERT INTO events(request_id, vehicle_id, actor, kind, data, created_at) VALUES (?,?,?,?,?,?)",
          (request_id, vehicle_id, actor, kind, json.dumps(data or {}, ensure_ascii=False), time.time()))


def log_usage(request_id: str, step: str, model: str, usage: dict, seconds: float) -> None:
    _exec("INSERT INTO llm_usage(request_id, step, model, prompt_tokens, completion_tokens, seconds, created_at)"
          " VALUES (?,?,?,?,?,?,?)",
          (request_id, step, model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
           seconds, time.time()))


def add_message(vehicle_id: str, role: str, content: str) -> None:
    _exec("INSERT INTO messages(vehicle_id, role, content, created_at) VALUES (?,?,?,?)",
          (vehicle_id, role, content, time.time()))


def recent_messages(vehicle_id: str, limit: int = 8) -> list[dict]:
    rows = _rows("SELECT role, content FROM messages WHERE vehicle_id=? ORDER BY id DESC LIMIT ?",
                 (vehicle_id, limit))
    return rows[::-1]


def save_draft(vehicle_id: str, session_id: str, payload: dict, extra: dict) -> int:
    cur = _exec("INSERT INTO tickets(vehicle_id, session_id, status, payload, extra, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (vehicle_id, session_id, "draft", json.dumps(payload, ensure_ascii=False),
                 json.dumps(extra, ensure_ascii=False), time.time()))
    return cur.lastrowid


def update_ticket(ticket_db_id: int, **fields) -> None:
    for k in ("payload", "extra"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE tickets SET {cols} WHERE id=?", (*fields.values(), ticket_db_id))


def get_ticket(ticket_db_id: int) -> dict | None:
    rows = _rows("SELECT * FROM tickets WHERE id=?", (ticket_db_id,))
    return _decode(rows[0]) if rows else None


def tickets_for(vehicle_id: str) -> list[dict]:
    return [_decode(r) for r in _rows("SELECT * FROM tickets WHERE vehicle_id=? AND status!='draft' "
                                      "ORDER BY id DESC", (vehicle_id,))]


def _decode(r: dict) -> dict:
    r["payload"] = json.loads(r["payload"])
    r["extra"] = json.loads(r["extra"] or "{}")
    return r


def clear_tickets() -> int:
    """Демо: удалить все заявки и черновики, чтобы сценарий можно было пройти заново."""
    return _exec("DELETE FROM tickets").rowcount


def has_draft(vehicle_id: str, session_id: str) -> bool:
    """Есть ли черновик заявки по этой проверке (для «покажи заявку»)."""
    return bool(_rows("SELECT 1 FROM tickets WHERE vehicle_id=? AND session_id=? LIMIT 1", (vehicle_id, session_id)))
