"""SQLite 状态存储：记录每个 aweme 的处理进度，保证去重与可重试。"""
from __future__ import annotations

import sqlite3
import threading
import time
import json as _json
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    aweme_id     TEXT PRIMARY KEY,
    account      TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    publish_time INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'discovered',
    video_path   TEXT NOT NULL DEFAULT '',
    audio_path   TEXT NOT NULL DEFAULT '',
    xima_track   TEXT NOT NULL DEFAULT '',
    meta         TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    sec_uid     TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'search',
    fetched_all INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
"""

# 状态机：discovered -> downloading -> downloaded -> converting -> converted
#          -> uploading -> uploaded；任意一步失败 -> failed（attempts < max 时下轮重试）
STATUSES = (
    "discovered",
    "downloading",
    "downloaded",
    "converting",
    "converted",
    "uploading",
    "uploaded",
    "failed",
    "skipped",
)


class Store:
    def __init__(self, db_path: Path | str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 基础操作 -------------------------------------------------
    def upsert_discovered(self, rec: dict[str, Any]) -> bool:
        """新作品入库；已存在则返回 False（去重）。"""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM records WHERE aweme_id = ?", (rec["aweme_id"],)
            )
            if cur.fetchone():
                return False
            with self._conn:
                self._conn.execute(
                    """INSERT INTO records (aweme_id, account, title, author, publish_time,
                                            status, meta, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        rec["aweme_id"],
                        rec.get("account", ""),
                        rec.get("title", ""),
                        rec.get("author", ""),
                        int(rec.get("publish_time", 0)),
                        "discovered",
                        _json.dumps(rec.get("meta", {}), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            return True

    def get(self, aweme_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE aweme_id = ?", (aweme_id,)
            ).fetchone()
        if row:
            rec = dict(row)
            rec["meta"] = _json.loads(rec.get("meta") or "{}")
            return rec
        return None

    def update(self, aweme_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE records SET {cols} WHERE aweme_id = ?",
                (*fields.values(), aweme_id),
            )

    def fail(self, aweme_id: str, error: str) -> None:
        self.update(aweme_id, status="failed", error=error[-2000:])

    def list_records(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records ORDER BY created_at DESC, aweme_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["meta"] = _json.loads(rec.get("meta") or "{}")
            out.append(rec)
        return out

    def pending(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        """取待处理记录：处于这些状态（含 attempts 未超限的 failed）。"""
        q_marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM records
                    WHERE status IN ({q_marks})
                       OR (status = 'failed' AND attempts < ?)
                    ORDER BY publish_time ASC, created_at ASC""",
                (*statuses, self._max_attempts),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["meta"] = _json.loads(rec.get("meta") or "{}")
            out.append(rec)
        return out

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM records GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    _max_attempts = 3  # 由 pipeline 启动时注入实际配置

    def set_max_attempts(self, n: int) -> None:
        self._max_attempts = max(1, int(n))

    # ---- 监控账号（配置文件 + 状态页搜索添加，统一入表管理） ----------
    def sync_config_accounts(self, accounts: list) -> None:
        """把配置文件里的账号同步入库（source=config，不覆盖已有记录）。"""
        now = int(time.time())
        with self._lock, self._conn:
            for a in accounts:
                self._conn.execute(
                    "INSERT OR IGNORE INTO accounts (sec_uid, name, source, created_at)"
                    " VALUES (?,?,?,?)",
                    (a.sec_uid, a.name, "config", now),
                )

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM accounts ORDER BY created_at, sec_uid"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_account(self, sec_uid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE sec_uid = ?", (sec_uid,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_account(self, sec_uid: str, name: str, source: str = "search") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO accounts (sec_uid, name, source, created_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(sec_uid) DO UPDATE SET name = excluded.name",
                (sec_uid, name, source, int(time.time())),
            )

    def set_fetched_all(self, sec_uid: str, done: int = 1) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE accounts SET fetched_all = ? WHERE sec_uid = ?", (done, sec_uid)
            )

    def delete_account(self, sec_uid: str) -> bool:
        """删除监控账号；仅允许删除状态页添加的（source=search）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM accounts WHERE sec_uid = ? AND source = 'search'",
                (sec_uid,),
            )
        return cur.rowcount > 0
