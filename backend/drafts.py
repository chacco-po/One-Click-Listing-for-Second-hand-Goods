"""上架草稿管理（任务9）：SQLite 存储 + 草稿文件管理

Repository 模式：路由层不直接写 SQL，全部经此模块。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
  id                    TEXT PRIMARY KEY,
  title                 TEXT NOT NULL DEFAULT '',
  category_id           TEXT NOT NULL,
  category_label        TEXT NOT NULL,
  recognized_label      TEXT,
  recognized_confidence REAL,
  condition_grade       TEXT NOT NULL,
  condition_grade_label TEXT NOT NULL,
  condition_answers     TEXT NOT NULL DEFAULT '{}',
  price                 REAL,
  price_low             REAL,
  price_high            REAL,
  image_path            TEXT NOT NULL,
  original_path         TEXT,
  spec                  TEXT,
    notes                 TEXT NOT NULL DEFAULT '',
  description            TEXT NOT NULL DEFAULT '',
  selling_points         TEXT NOT NULL DEFAULT '[]',
  owner_id              TEXT,
  product_id            TEXT,
  status                TEXT NOT NULL DEFAULT 'draft',
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_created_at ON drafts(created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["condition_answers"] = json.loads(d["condition_answers"] or "{}")
    d["selling_points"] = json.loads(d["selling_points"] or "[]")
    return d


class DraftRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self):
        """确保连接及时关闭（避免长连接占用文件锁）。"""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------------- CRUD ----------------

    def create(self, data: dict[str, Any]) -> dict:
        draft_id = uuid.uuid4().hex[:12]
        now = _now()
        record = {
            "id": draft_id,
            "title": (data.get("title") or "").strip()[:60],
            "category_id": data["category_id"],
            "category_label": data["category_label"],
            "recognized_label": data.get("recognized_label"),
            "recognized_confidence": data.get("recognized_confidence"),
            "condition_grade": data["condition_grade"],
            "condition_grade_label": data["condition_grade_label"],
            "condition_answers": json.dumps(data.get("condition_answers", {}), ensure_ascii=False),
            "price": data.get("price"),
            "price_low": data.get("price_low"),
            "price_high": data.get("price_high"),
            "image_path": data["image_path"],
            "original_path": data.get("original_path"),
            "spec": data.get("spec"),
            "notes": data.get("notes", ""),
            "description": data.get("description", "") or "",
            "selling_points": json.dumps(data.get("selling_points", []) or [], ensure_ascii=False),
            "owner_id": data.get("owner_id"),
            "product_id": data.get("product_id"),
            "status": data.get("status", "draft"),
            "created_at": now,
            "updated_at": now,
        }
        cols = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        with self._session() as conn:
            conn.execute(
                f"INSERT INTO drafts ({cols}) VALUES ({placeholders})",
                tuple(record.values()),
            )
        return self.get(draft_id)  # type: ignore[return-value]

    def list(self) -> list[dict]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM drafts ORDER BY created_at DESC").fetchall()
        return [_row_to_dict(r) for r in rows]

    def get(self, draft_id: str) -> dict | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return _row_to_dict(row) if row else None

    _UPDATABLE = {"title", "category_id", "category_label", "condition_grade",
                  "condition_grade_label", "condition_answers", "price",
                  "price_low", "price_high", "notes", "description",
                  "selling_points", "owner_id", "product_id", "status"}

    def update(self, draft_id: str, fields: dict[str, Any]) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in self._UPDATABLE}
        if "condition_answers" in allowed and isinstance(allowed["condition_answers"], dict):
            allowed["condition_answers"] = json.dumps(allowed["condition_answers"], ensure_ascii=False)
        if "selling_points" in allowed and isinstance(allowed["selling_points"], (list, dict)):
            allowed["selling_points"] = json.dumps(allowed["selling_points"], ensure_ascii=False)
        if not allowed:
            return self.get(draft_id)
        allowed["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in allowed)
        with self._session() as conn:
            cur = conn.execute(
                f"UPDATE drafts SET {assignments} WHERE id = ?",
                (*allowed.values(), draft_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(draft_id)

    def delete(self, draft_id: str) -> dict | None:
        """删除草稿及其图片文件，返回被删除的记录（供调用方清理文件）。"""
        draft = self.get(draft_id)
        if draft is None:
            return None
        with self._session() as conn:
            conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        for key in ("image_path", "original_path"):
            rel = draft.get(key)
            if rel:
                path = Path(rel)
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    # 部分环境把 unlink 改写为回收站操作，可能失败；不影响草稿删除结果
                    pass
        return draft
