"""SQLite-backed persistence for SMS inbox and analysis records.

Behavior is kept identical to the original implementation; this refactor adds
type hints, docstrings, and small structural cleanups without changing logic.
"""

import os
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


class SQLiteStore:
    """Lightweight SQLite store with a stable interface.

    Methods provided (unchanged):
      - save_analysis(rec)
      - get_analysis_page(page, page_size) -> (items, total)
      - add_sms(id_num, row)
      - get_sms_recent(since_id, limit)
      - get_sms_max_id()

    Configure with env:
      STORAGE_BACKEND=sqlite
      SQLITE_PATH=/home/data/app.db  (Azure App Service Linux: use /home)
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._enabled = False
        path = db_path or _env("SQLITE_PATH") or os.path.join(os.getcwd(), "app.db")
        # Ensure parent dir exists
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._ensure_schema()
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        # analysis table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis (
              id TEXT PRIMARY KEY,
              sms TEXT,
              normalized TEXT,
              hits TEXT,
              context TEXT,
              answer TEXT,
              ts TEXT NOT NULL
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analysis_ts ON analysis(ts);")
        # sms table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sms (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              id_num INTEGER UNIQUE,
              message TEXT,
              sender TEXT,
              receiver TEXT,
              provider_message_id TEXT,
              received_at TEXT,
              created_at TEXT
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_idnum ON sms(id_num);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_recv ON sms(received_at);")
        self._conn.commit()
        cur.close()

    def save_analysis(self, rec: Dict[str, Any]) -> Optional[str]:
        if not self.enabled:
            return None
        from uuid import uuid4
        aid = rec.get("id") or str(uuid4())
        sms = rec.get("sms")
        normalized = json.dumps(rec.get("normalized"), ensure_ascii=False)
        hits = json.dumps(rec.get("hits"), ensure_ascii=False)
        context = json.dumps(rec.get("context"), ensure_ascii=False)
        answer = rec.get("answer")
        ts = rec.get("ts")
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO analysis (id, sms, normalized, hits, context, answer, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              sms=excluded.sms,
              normalized=excluded.normalized,
              hits=excluded.hits,
              context=excluded.context,
              answer=excluded.answer,
              ts=excluded.ts
            """,
            (aid, sms, normalized, hits, context, answer, ts),
        )
        self._conn.commit()
        cur.close()
        return str(aid)

    def get_analysis_page(self, page: int, page_size: int) -> Tuple[List[Dict[str, Any]], int]:
        if not self.enabled:
            return [], 0
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        offset = (page - 1) * page_size
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(1) FROM analysis")
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT id, sms, normalized, hits, context, answer, ts FROM analysis ORDER BY ts DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        rows = cur.fetchall()
        cur.close()
        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "id": r[0],
                    "sms": r[1],
                    "normalized": _json_loads_safe(r[2]),
                    "hits": _json_loads_safe(r[3]),
                    "context": _json_loads_safe(r[4]),
                    "answer": r[5],
                    "ts": r[6],
                }
            )
        return items, total

    def add_sms(self, id_num: int, row: Dict[str, Any]) -> Optional[str]:
        if not self.enabled:
            return None
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO sms (id_num, message, sender, receiver, provider_message_id, received_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_num) DO UPDATE SET
              message=excluded.message,
              sender=excluded.sender,
              receiver=excluded.receiver,
              provider_message_id=excluded.provider_message_id,
              received_at=excluded.received_at,
              created_at=excluded.created_at
            """,
            (
                int(id_num),
                row.get("message"),
                row.get("sender"),
                row.get("receiver"),
                row.get("provider_message_id"),
                row.get("received_at"),
                row.get("created_at"),
            ),
        )
        self._conn.commit()
        cur.close()
        return str(id_num)

    def get_sms_recent(self, since_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id_num, message, sender, receiver, provider_message_id, received_at FROM sms WHERE id_num > ? ORDER BY id_num ASC LIMIT ?",
            (int(since_id), int(limit)),
        )
        rows = cur.fetchall()
        cur.close()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": int(r[0]),
                    "message": r[1],
                    "sender": r[2],
                    "receiver": r[3],
                    "provider_message_id": r[4],
                    "received_at": r[5],
                }
            )
        return out

    def get_sms_max_id(self) -> int:
        if not self.enabled:
            return 0
        cur = self._conn.cursor()
        cur.execute("SELECT COALESCE(MAX(id_num), 0) FROM sms")
        v = cur.fetchone()[0]
        cur.close()
        try:
            return int(v or 0)
        except Exception:
            return 0


def _json_loads_safe(s: Any) -> Any:
    try:
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="ignore")
        if isinstance(s, str):
            return json.loads(s)
        return s
    except Exception:
        return s

