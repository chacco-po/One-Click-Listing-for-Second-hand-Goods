"""商城数据层单元测试：原子购买、并发防重、订单查询、筛选。

用临时 SQLite 文件，不依赖运行中的服务。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.market import MarketError, MarketRepository  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} | {detail}")
        print(f"  [FAIL] {name} | {detail}")


def make_product(repo: MarketRepository, seller_id: str, **over) -> dict:
    """直接往 products 表插一条商品（绕过发布流程，聚焦数据层）。"""
    import sqlite3

    pid = over.get("id", "p" + seller_id[-4:] + str(len(seller_id)))
    now = "2026-10-23T10:00:00+08:00"
    conn = sqlite3.connect(repo.db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS products (
             id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
             category_id TEXT NOT NULL, category_label TEXT NOT NULL,
             condition_grade TEXT NOT NULL, condition_grade_label TEXT NOT NULL,
             price REAL NOT NULL, image_path TEXT NOT NULL, original_path TEXT,
             notes TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
             selling_points TEXT NOT NULL DEFAULT '[]',
             status TEXT NOT NULL DEFAULT 'on_sale', draft_id TEXT,
             sold_to TEXT, sold_at TEXT,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        """INSERT INTO products
           (id, user_id, title, category_id, category_label, condition_grade,
            condition_grade_label, price, image_path, status, description,
            selling_points, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            pid, seller_id, over.get("title", "测试商品"), over.get("category_id", "shoes"),
            over.get("category_label", "鞋包"), "like_new_95", "95新",
            over.get("price", 100.0), over.get("image_path", "/tmp/a.png"),
            over.get("status", "on_sale"), over.get("description", "描述"),
            "[]", now, now,
        ),
    )
    conn.commit()
    conn.close()
    return {"id": pid}


def make_user(repo: MarketRepository, uid: str, username: str) -> None:
    import sqlite3

    conn = sqlite3.connect(repo.db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
            "password_hash TEXT NOT NULL, salt TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO users (id, username, password_hash, salt, created_at) VALUES (?,?,?,?,?)",
            (uid, username, "x", "y", "2026-10-23T10:00:00+08:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_schema(tmp: Path) -> None:
    print("\n== 表结构 ==")
    repo = MarketRepository(tmp / "m.db")
    import sqlite3

    conn = sqlite3.connect(repo.db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    check("orders 表已创建", "orders" in tables, str(tables))
    # 索引检查
    conn = sqlite3.connect(repo.db_path)
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    check("orders 买家索引已建", "idx_orders_buyer" in idx, str(idx))
    check("orders 商品索引已建", "idx_orders_product" in idx, str(idx))


def test_buy_flow(tmp: Path) -> None:
    print("\n== 购买主流程 ==")
    repo = MarketRepository(tmp / "b.db")
    make_user(repo, "seller1", "卖家一")
    make_user(repo, "buyer1", "买家一")
    p = make_product(repo, "seller1", id="prod_a", title="Air Force 1", price=288.0)

    order = repo.buy("prod_a", "buyer1")
    check("购买返回订单 id", bool(order.get("order_id")), str(order))
    check("订单成交价与商品一致", order["price"] == 288.0, str(order["price"]))
    check("订单记录了商品名快照", order["title"] == "Air Force 1", str(order["title"]))

    item = repo.get_public("prod_a", "buyer1")
    check("商品状态变为 sold", item["status"] == "sold", item["status"])
    check("商品记录了 sold_at", bool(item.get("sold_at")), str(item.get("sold_at")))
    check("商品记录了买家 sold_to", item.get("sold_to") == "buyer1", str(item.get("sold_to")))
    check("买家视角标记 bought_by_me", item["bought_by_me"] is True, str(item))
    check("卖家视角不标记 bought_by_me", repo.get_public("prod_a", "seller1")["bought_by_me"] is False)
    check("卖家视角标记 is_own", repo.get_public("prod_a", "seller1")["is_own"] is True)
    check("商品列表返回卖家名", repo.get_public("prod_a")["seller_name"] == "卖家一",
          str(repo.get_public("prod_a").get("seller_name")))


def test_buy_guard(tmp: Path) -> None:
    print("\n== 购买约束 ==")
    repo = MarketRepository(tmp / "g.db")
    make_user(repo, "s1", "卖家甲")
    make_user(repo, "b1", "买家甲")
    make_product(repo, "s1", id="prod_b", price=50.0)

    try:
        repo.buy("prod_b", "s1")
        check("购买自己的商品被拒绝", False, "未抛异常")
    except MarketError as e:
        check("购买自己的商品被拒绝", e.code == "OWN_ITEM", e.code)

    try:
        repo.buy("not_exist", "b1")
        check("购买不存在的商品被拒绝", False, "未抛异常")
    except MarketError as e:
        check("购买不存在的商品被拒绝", e.code == "PRODUCT_NOT_FOUND", e.code)

    repo.buy("prod_b", "b1")
    try:
        repo.buy("prod_b", "b1")
        check("重复购买同一商品被拒绝", False, "未抛异常")
    except MarketError as e:
        check("重复购买同一商品被拒绝", e.code == "ALREADY_SOLD", e.code)

    check("已售出商品状态正确", repo.get_public("prod_b")["status"] == "sold")

    # 下架商品不可购买
    make_product(repo, "s1", id="prod_c", status="off_shelf")
    try:
        repo.buy("prod_c", "b1")
        check("购买已下架商品被拒绝", False, "未抛异常")
    except MarketError as e:
        check("购买已下架商品被拒绝", e.code == "ALREADY_SOLD", e.code)


def test_concurrent_buy(tmp: Path) -> None:
    print("\n== 并发防重复售出 ==")
    import sqlite3
    import threading

    repo = MarketRepository(tmp / "c.db")
    make_user(repo, "s9", "卖家丙")
    make_product(repo, "s9", id="prod_race", price=99.0)

    results: list[str] = []
    lock = threading.Lock()

    def worker(buyer: str) -> None:
        try:
            repo.buy("prod_race", buyer)
            with lock:
                results.append("ok")
        except MarketError as e:
            with lock:
                results.append(e.code)

    threads = [threading.Thread(target=worker, args=(f"buyer{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok_count = results.count("ok")
    check("并发下只有 1 笔成交", ok_count == 1, f"ok={ok_count} all={results}")
    check("其余请求被判为已售出", all(r == "ALREADY_SOLD" for r in results if r != "ok"), str(results))

    conn = sqlite3.connect(repo.db_path)
    n = conn.execute("SELECT COUNT(*) FROM orders WHERE product_id='prod_race'").fetchone()[0]
    conn.close()
    check("订单表只有 1 条记录", n == 1, str(n))


def test_orders_query(tmp: Path) -> None:
    print("\n== 订单查询 ==")
    repo = MarketRepository(tmp / "o.db")
    make_user(repo, "s2", "卖家乙")
    make_user(repo, "b2", "买家乙")
    make_user(repo, "b3", "买家丙")
    make_product(repo, "s2", id="prod_d", title="闲置显示器", price=320.0)
    make_product(repo, "s2", id="prod_e", title="机械键盘", price=150.0)

    repo.buy("prod_d", "b2")
    repo.buy("prod_e", "b3")

    bought = repo.list_orders("b2", "buyer")
    check("买家看到 1 条购买记录", len(bought) == 1, str(len(bought)))
    check("购买记录含商品名", bought[0]["title"] == "闲置显示器", str(bought[0].get("title")))
    check("购买记录含卖家名", bought[0]["seller_name"] == "卖家乙", str(bought[0].get("seller_name")))
    check("购买记录含买家名", bought[0]["buyer_name"] == "买家乙", str(bought[0].get("buyer_name")))
    check("购买记录含成交价", bought[0]["price"] == 320.0, str(bought[0].get("price")))

    sold = repo.list_orders("s2", "seller")
    check("卖家看到 2 条卖出记录", len(sold) == 2, str(len(sold)))
    check("卖出记录数统计正确", repo.count_sold_by_user("s2") == 2)

    check("无关用户查不到订单", len(repo.list_orders("nobody", "buyer")) == 0)


def test_list_filters(tmp: Path) -> None:
    print("\n== 商城筛选与排序 ==")
    repo = MarketRepository(tmp / "f.db")
    make_user(repo, "s3", "卖家丁")
    make_user(repo, "b4", "买家丁")
    make_product(repo, "s3", id="p1", title="白色球鞋", price=300.0, category_id="shoes")
    make_product(repo, "s3", id="p2", title="黑色背包", price=120.0, category_id="bags")
    make_product(repo, "s3", id="p3", title="女士手袋", price=560.0, category_id="bags")
    make_product(repo, "s3", id="p4", title="下架商品", price=1.0, status="off_shelf")

    items, total = repo.list_products()
    check("默认不含下架商品", all(i["status"] != "off_shelf" for i in items), str([i["id"] for i in items]))
    check("默认总数为 3", total == 3, str(total))

    items, total = repo.list_products(category_id="bags")
    check("类目筛选生效", total == 2, str(total))

    items, _ = repo.list_products(keyword="球鞋")
    check("关键词搜索生效", len(items) == 1 and items[0]["id"] == "p1", str([i["id"] for i in items]))

    items, total = repo.list_products(sort="price_asc")
    check("价格升序排序生效", items[0]["price"] == 120.0, str([i["price"] for i in items]))
    items, _ = repo.list_products(sort="price_desc")
    check("价格降序排序生效", items[0]["price"] == 560.0, str([i["price"] for i in items]))

    repo.buy("p1", "b4")
    items, total = repo.list_products(status="sold", viewer_id="b4")
    check("已售出筛选生效", total == 1 and items[0]["id"] == "p1", str(total))

    items, total = repo.list_products(only_bought=True, viewer_id="b4")
    check("「我买到的」筛选生效", total == 1 and items[0]["id"] == "p1", str(total))

    items, total = repo.list_products(only_bought=True, viewer_id="s3")
    check("卖家视角「我买到的」为空", total == 0, str(total))

    items, _ = repo.list_products(limit=2, offset=0)
    check("分页 limit 生效", len(items) == 2, str(len(items)))
    items, _ = repo.list_products(limit=2, offset=2)
    check("分页 offset 生效", len(items) == 1, str(len(items)))


def test_selling_points_parse(tmp: Path) -> None:
    print("\n== 卖点字段解析 ==")
    import sqlite3

    repo = MarketRepository(tmp / "sp.db")
    make_user(repo, "s5", "卖家戊")
    make_product(repo, "s5", id="p_sp")
    conn = sqlite3.connect(repo.db_path)
    conn.execute(
        "UPDATE products SET selling_points = ? WHERE id = 'p_sp'",
        ('[{"group":"功能状态","text":"各项功能正常"}]',),
    )
    conn.commit()
    conn.close()
    item = repo.get_public("p_sp")
    check("卖点 JSON 正确解析为列表", isinstance(item["selling_points"], list), str(item["selling_points"]))
    check("卖点内容正确", item["selling_points"][0]["text"] == "各项功能正常", str(item["selling_points"]))


def main() -> int:
    print("=" * 60)
    print("商城数据层测试")
    print("=" * 60)
    # 沙箱环境会把 unlink 改写为回收站操作，临时目录清理可能因文件占用失败，
    # 因此这里手动建目录并在结束后忽略清理错误。
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="idlelister_market_"))
    try:
        test_schema(tmp)
        test_buy_flow(tmp)
        test_buy_guard(tmp)
        test_concurrent_buy(tmp)
        test_orders_query(tmp)
        test_list_filters(tmp)
        test_selling_points_parse(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过 ✔ {PASSED} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
