"""商城：已上架商品的公开列表 + 购买订单。

与 `products.py` 的分工：
- `products.py` 管「卖家的商品」（我的商品：可改名、改价、改状态、删除）
- `market.py` 管「买家的视角」（商城：浏览所有在售商品、下单购买、查看订单）

设计要点：
- **原子购买**：用 `UPDATE ... WHERE status='on_sale'` + 检查 rowcount 实现，
  避免并发下同一件商品被卖出两次（先查后改会有竞态窗口）。
- **订单快照**：订单里冗余记录成交价与卖家 ID，即便商品后续改名/下架/删除，
  订单历史依然可读。
- **已售出商品仍留在商城里**，由前端打上「已售出」巨标 —— 这是二手市场的常见做法，
  既提示该商品已被买走，也给其他商品带来曝光。
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 商城里可见的商品状态（已下架的不展示）
MARKET_STATUSES = ("on_sale", "sold")

#: 商城列表可用的筛选维度
SORT_OPTIONS = {
    "latest": "created_at DESC",
    "price_asc": "price ASC",
    "price_desc": "price DESC",
}


class MarketError(Exception):
    """商城业务异常（购买失败等）。"""

    def __init__(self, message: str, code: str = "MARKET_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id          TEXT PRIMARY KEY,
  product_id  TEXT NOT NULL,
  buyer_id    TEXT NOT NULL,
  seller_id   TEXT NOT NULL,
  price       REAL NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_seller ON orders(seller_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product_id);
"""


class MarketRepository:
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
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ 商城列表

    def list_products(
        self,
        *,
        category_id: str | None = None,
        keyword: str | None = None,
        status: str | None = None,
        sort: str = "latest",
        viewer_id: str | None = None,
        only_bought: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """返回 (商品列表, 命中总数)。

        `viewer_id` 用于标注「这是否是我买的 / 这是不是我自己的」，
        让前端能直接渲染「立即购买」「已售出」「我的商品」等不同操作。
        """
        where = ["p.status IN (?, ?)"]
        args: list[Any] = list(MARKET_STATUSES)

        if category_id:
            where.append("p.category_id = ?")
            args.append(category_id)
        if keyword:
            where.append("(p.title LIKE ? OR p.category_label LIKE ? OR p.description LIKE ?)")
            like = f"%{keyword.strip()}%"
            args.extend([like, like, like])
        if status in ("on_sale", "sold"):
            where.append("p.status = ?")
            args.append(status)

        if only_bought and viewer_id:
            where.append("EXISTS (SELECT 1 FROM orders o WHERE o.product_id = p.id AND o.buyer_id = ?)")
            args.append(viewer_id)

        clause = " AND ".join(where)
        order_by = SORT_OPTIONS.get(sort, SORT_OPTIONS["latest"])

        with self._session() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM products p WHERE {clause}", tuple(args)
            ).fetchone()["c"]
            rows = conn.execute(
                f"""SELECT p.*, u.username AS seller_name,
                           (SELECT o.buyer_id FROM orders o
                             WHERE o.product_id = p.id ORDER BY o.created_at DESC LIMIT 1) AS last_buyer_id
                      FROM products p
                      LEFT JOIN users u ON u.id = p.user_id
                     WHERE {clause}
                     ORDER BY p.{order_by}
                     LIMIT ? OFFSET ?""",
                (*args, limit, offset),
            ).fetchall()

        items = []
        for row in rows:
            d = dict(row)
            d["selling_points"] = _load_points(d.get("selling_points"))
            d["is_own"] = bool(viewer_id and d.get("user_id") == viewer_id)
            d["bought_by_me"] = bool(viewer_id and d.get("last_buyer_id") == viewer_id)
            items.append(d)
        return items, int(total)

    def get_public(self, product_id: str, viewer_id: str | None = None) -> dict | None:
        """商城里的商品详情（不校验归属，公开可见）。"""
        with self._session() as conn:
            row = conn.execute(
                """SELECT p.*, u.username AS seller_name,
                          (SELECT o.buyer_id FROM orders o
                            WHERE o.product_id = p.id ORDER BY o.created_at DESC LIMIT 1) AS last_buyer_id
                     FROM products p LEFT JOIN users u ON u.id = p.user_id
                    WHERE p.id = ?""",
                (product_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["selling_points"] = _load_points(d.get("selling_points"))
        d["is_own"] = bool(viewer_id and d.get("user_id") == viewer_id)
        d["bought_by_me"] = bool(viewer_id and d.get("last_buyer_id") == viewer_id)
        return d

    # ------------------------------------------------------------ 购买

    def buy(self, product_id: str, buyer_id: str) -> dict:
        """购买商品。

        原子性：单条 `UPDATE ... WHERE status='on_sale'` 完成状态迁移，
        靠 rowcount 判断是否抢到。若 rowcount 为 0，说明已被别人买走或已下架。
        """
        with self._session() as conn:
            row = conn.execute(
                "SELECT id, user_id, title, price, status FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()
            if row is None:
                raise MarketError("商品不存在或已下架", "PRODUCT_NOT_FOUND")
            if row["user_id"] == buyer_id:
                raise MarketError("不能购买自己发布的商品", "OWN_ITEM")
            if row["status"] != "on_sale":
                raise MarketError("该商品已被买走或已下架", "ALREADY_SOLD")

            order_id = uuid.uuid4().hex[:12]
            now = _now()
            cur = conn.execute(
                "UPDATE products SET status = 'sold', sold_to = ?, sold_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'on_sale'",
                (buyer_id, now, now, product_id),
            )
            if cur.rowcount == 0:
                # 并发下被他人抢先
                raise MarketError("该商品刚被其他买家买走", "ALREADY_SOLD")

            conn.execute(
                "INSERT INTO orders (id, product_id, buyer_id, seller_id, price, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (order_id, product_id, buyer_id, row["user_id"], float(row["price"]), now),
            )

        return {
            "order_id": order_id,
            "product_id": product_id,
            "title": row["title"],
            "price": float(row["price"]),
            "created_at": now,
        }

    # ------------------------------------------------------------ 订单查询

    def list_orders(self, user_id: str, role: str = "buyer") -> list[dict]:
        """按买家/卖家视角列出订单（含商品快照信息）。"""
        join_col = "o.buyer_id" if role == "buyer" else "o.seller_id"
        with self._session() as conn:
            rows = conn.execute(
                f"""SELECT o.*, p.title, p.image_path, p.category_label,
                           p.condition_grade_label, p.status AS product_status,
                           buyer.username AS buyer_name, seller.username AS seller_name
                      FROM orders o
                      LEFT JOIN products p ON p.id = o.product_id
                      LEFT JOIN users buyer ON buyer.id = o.buyer_id
                      LEFT JOIN users seller ON seller.id = o.seller_id
                     WHERE {join_col} = ?
                     ORDER BY o.created_at DESC""",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_sold_by_user(self, user_id: str) -> int:
        with self._session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM orders WHERE seller_id = ?", (user_id,)
            ).fetchone()
        return int(row["c"])


def _load_points(raw: Any) -> list:
    import json

    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
