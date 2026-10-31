"""已上架商品管理（发布后的商品列表）。

与「上架草稿」区分：草稿是编辑中的素材，商品是已发布、可对外展示的条目。
发布时要求登录，草稿归属于发布者本人。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = {
    "on_sale": "在售",
    "sold": "已售出",
    "off_shelf": "已下架",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id                    TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL,
  title                 TEXT NOT NULL,
  category_id           TEXT NOT NULL,
  category_label        TEXT NOT NULL,
  condition_grade       TEXT NOT NULL,
  condition_grade_label TEXT NOT NULL,
  price                 REAL NOT NULL,
  image_path            TEXT NOT NULL,
  original_path         TEXT,
    notes                 TEXT NOT NULL DEFAULT '',
  description            TEXT NOT NULL DEFAULT '',
  selling_points         TEXT NOT NULL DEFAULT '[]',
  status                TEXT NOT NULL DEFAULT 'on_sale',
  draft_id              TEXT,
  sold_to               TEXT,
  sold_at               TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_user ON products(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status, created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ProductRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
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

    def create(self, data: dict[str, Any]) -> dict:
        product_id = uuid.uuid4().hex[:12]
        now = _now()
        record = {
            "id": product_id,
            "user_id": data["user_id"],
            "title": data["title"].strip(),
            "category_id": data["category_id"],
            "category_label": data["category_label"],
            "condition_grade": data["condition_grade"],
            "condition_grade_label": data["condition_grade_label"],
            "price": float(data["price"]),
            "image_path": data["image_path"],
            "original_path": data.get("original_path"),
            "notes": data.get("notes", "") or "",
            "description": data.get("description", "") or "",
            "selling_points": json.dumps(data.get("selling_points", []) or [], ensure_ascii=False),
            "status": data.get("status", "on_sale"),
            "draft_id": data.get("draft_id"),
            "created_at": now,
            "updated_at": now,
        }
        cols = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        with self._session() as conn:
            conn.execute(
                f"INSERT INTO products ({cols}) VALUES ({placeholders})",
                tuple(record.values()),
            )
        return self.get(product_id)  # type: ignore[return-value]

    def list_by_user(self, user_id: str) -> list[dict]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM products WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return [self._to_dict(r) for r in rows]

    def get(self, product_id: str) -> dict | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return self._to_dict(row) if row else None

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["selling_points"] = json.loads(d.get("selling_points") or "[]")
        return d

    _UPDATABLE = {"title", "price", "status", "notes", "description",
                  "selling_points", "category_id", "category_label"}

    def update(self, product_id: str, user_id: str, fields: dict[str, Any]) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in self._UPDATABLE and v is not None}
        if "selling_points" in allowed and isinstance(allowed["selling_points"], (list, dict)):
            allowed["selling_points"] = json.dumps(allowed["selling_points"], ensure_ascii=False)
        if not allowed:
            return self.get(product_id)
        allowed["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in allowed)
        with self._session() as conn:
            cur = conn.execute(
                f"UPDATE products SET {assignments} WHERE id = ? AND user_id = ?",
                (*allowed.values(), product_id, user_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(product_id)

    def delete(self, product_id: str, user_id: str) -> dict | None:
        product = self.get(product_id)
        if product is None or product["user_id"] != user_id:
            return None
        with self._session() as conn:
            conn.execute(
                "DELETE FROM products WHERE id = ? AND user_id = ?", (product_id, user_id)
            )
        return product

    def count_by_user(self, user_id: str) -> int:
        with self._session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM products WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["c"])
