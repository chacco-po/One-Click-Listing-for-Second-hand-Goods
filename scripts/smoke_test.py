"""端到端冒烟验证：注册 → 处理图片 → 建草稿 → 发布商品 → 我的商品 → 改名 → 删草稿 → 删商品。

用法：
    python scripts/smoke_test.py [base_url]

依赖：requests + pillow（已在 backend/requirements.txt 中）。
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "assets" / "sneaker_source.jpg"

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  -> {detail}" if detail else ""))
    return ok


def wait_ready(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE}/health", timeout=3).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    print(f"== 目标服务: {BASE} ==")
    if not wait_ready():
        print("服务未就绪")
        return 1

    username = f"smoke_{uuid.uuid4().hex[:8]}"
    password = "smoke_pass_123"

    # ---------- 1. 注册 ----------
    r = requests.post(f"{BASE}/api/auth/register", json={"username": username, "password": password}, timeout=15)
    if not check("注册账号", r.status_code == 201, f"HTTP {r.status_code} {r.text[:120]}"):
        return 1
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    # ---------- 2. 登录 ----------
    r = requests.post(f"{BASE}/api/auth/login", json={"username": username, "password": password}, timeout=15)
    check("登录并返回 token", r.status_code == 200 and bool(r.json().get("token")), f"HTTP {r.status_code}")

    r = requests.get(f"{BASE}/api/auth/me", headers=auth, timeout=15)
    check("查询当前用户", r.status_code == 200 and (r.json().get("user") or {}).get("username") == username, f"HTTP {r.status_code}")

    # ---------- 3. 处理图片 ----------
    with SAMPLE.open("rb") as fh:
        r = requests.post(
            f"{BASE}/api/process",
            files={"file": (SAMPLE.name, fh, "image/jpeg")},
            timeout=300,
        )
    if not check("图片处理管线", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"):
        return 1
    proc = r.json()
    image_id = proc.get("image_id")
    check("返回 image_id", bool(image_id), str(image_id))

    # ---------- 4. 创建草稿 ----------
    r = requests.post(
        f"{BASE}/api/drafts",
        headers=auth,
        json={
            "image_id": image_id,
            "category_id": "shoes",
            "condition_answers": {
                "usage": "y2",
                "appearance": "light",
                "function": "ok",
                "accessories": "partial",
            },
            "title": "冒烟测试商品",
        },
        timeout=30,
    )
    if not check("创建草稿", r.status_code == 201, f"HTTP {r.status_code} {r.text[:200]}"):
        return 1
    draft = r.json()
    draft_id = draft.get("id")
    check("草稿返回 id", bool(draft_id), str(draft_id))

    # ---------- 4.5 文案生成（任务4） ----------
    r = requests.post(
        f"{BASE}/api/copywriting",
        json={
            "category_id": "shoes",
            "grade_id": "like_new_95",
            "condition_answers": {
                "usage": "y2",
                "appearance": "light",
                "function": "ok",
                "accessories": "partial",
            },
            "extras": {
                "brand": "Nike Air Force 1",
                "color": "白色 42码",
                "channel": "京东自营",
                "reason": "换新闲置",
            },
            "image_id": image_id,
            "price": 199.0,
        },
        timeout=30,
    )
    if not check("生成文案", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"):
        return 1
    copy = r.json()
    check("文案含标题候选", len(copy.get("titles") or []) >= 2, str(len(copy.get("titles") or [])))
    check("文案含结构化卖点", len(copy.get("selling_points") or []) >= 3, str(len(copy.get("selling_points") or [])))
    check("文案含描述全文", bool(copy.get("description")))
    check("卖点均标注来源", all(
        item.get("source") for g in (copy.get("selling_points") or []) for item in (g.get("items") or [])
    ))
    copy_title = (copy.get("titles") or [{}])[0].get("text", "")
    check("文案引用用户填写的品牌", "Nike" in copy_title, copy_title)
    copy_points = [
        item.get("text", "") for g in (copy.get("selling_points") or []) for item in (g.get("items") or [])
    ]
    check("文案引用用户填写的渠道", any("京东自营" in t for t in copy_points), str(copy_points[:3]))

    # 字段缺失兜底：只给类目也应能出文案
    r = requests.post(
        f"{BASE}/api/copywriting",
        json={"category_id": "phone", "grade_id": "good_90"},
        timeout=30,
    )
    bare = r.json() if r.status_code == 200 else {}
    check("字段缺失仍能生成文案", r.status_code == 200 and bool(bare.get("title")), f"HTTP {r.status_code}")
    check("字段缺失时给出补充提示", len(bare.get("warnings") or []) >= 2, str(len(bare.get("warnings") or [])))

    # 非法类目应被拒
    r = requests.post(f"{BASE}/api/copywriting", json={"category_id": "not_exist"}, timeout=15)
    check("非法类目被拒", r.status_code == 400, f"HTTP {r.status_code}")

    # ---------- 5. 我的草稿列表 ----------
    r = requests.get(f"{BASE}/api/drafts", headers=auth, timeout=15)
    ids = [d.get("id") for d in (r.json().get("items") or [])]
    check("草稿列表包含新草稿", r.status_code == 200 and draft_id in ids, f"HTTP {r.status_code} count={len(ids)}")

    # ---------- 6. 发布商品（带商品名称 + 文案） ----------
    r = requests.post(
        f"{BASE}/api/products",
        headers=auth,
        json={
            "draft_id": draft_id,
            "title": "冒烟测试运动鞋",
            "price": 199.0,
            "description": copy.get("description", ""),
            "selling_points": [
                item
                for g in (copy.get("selling_points") or [])
                for item in (g.get("items") or [])
            ],
        },
        timeout=30,
    )
    if not check("发布商品", r.status_code == 201, f"HTTP {r.status_code} {r.text[:200]}"):
        return 1
    product = r.json()
    product_id = product.get("id")
    check("商品名称已保存", product.get("title") == "冒烟测试运动鞋", str(product.get("title")))
    check("商品默认状态为在售", product.get("status") == "on_sale", str(product.get("status")))
    check("商品已保存描述文案", bool(product.get("description")), str(product.get("description"))[:80])
    check("商品已保存结构化卖点", len(product.get("selling_points") or []) >= 3,
          str(len(product.get("selling_points") or [])))

    # ---------- 7. 我的商品列表 ----------
    r = requests.get(f"{BASE}/api/products", headers=auth, timeout=15)
    items = r.json().get("items") or []
    check("我的商品列表包含新商品", r.status_code == 200 and any(p.get("id") == product_id for p in items), f"HTTP {r.status_code} count={len(items)}")

    # ---------- 8. 商品改名 / 改价 ----------
    r = requests.put(
        f"{BASE}/api/products/{product_id}",
        headers=auth,
        json={"title": "冒烟测试运动鞋（已改名）", "price": 168.0},
        timeout=15,
    )
    ok = r.status_code == 200 and r.json().get("title") == "冒烟测试运动鞋（已改名）"
    check("修改商品名称与价格", ok, f"HTTP {r.status_code} {r.text[:160]}")

    # ---------- 9. 未登录保护 ----------
    r = requests.get(f"{BASE}/api/products", timeout=15)
    check("未登录访问我的商品被拒", r.status_code == 401, f"HTTP {r.status_code}")

    # ---------- 10. 商城浏览（公开，无需登录） ----------
    r = requests.get(f"{BASE}/api/shop", timeout=20)
    check("未登录可浏览商城", r.status_code == 200, f"HTTP {r.status_code}")
    shop_items = (r.json() or {}).get("items") or []
    check("商城包含已上架商品", any(p.get("id") == product_id for p in shop_items),
          f"count={len(shop_items)}")
    mine = next((p for p in shop_items if p.get("id") == product_id), {})
    check("商城标记自己的商品 is_own", mine.get("is_own") is False, str(mine.get("is_own")))
    check("商城商品带卖家名", bool(mine.get("seller_name")), str(mine.get("seller_name")))
    check("商城商品图片为可访问 URL", str(mine.get("image_path", "")).startswith("/outputs/"),
          str(mine.get("image_path")))
    check("商城不泄露买家身份", "last_buyer_id" not in mine and "sold_to" not in mine,
          str(list(mine.keys())))

    r = requests.get(f"{BASE}/api/shop/{product_id}", timeout=15)
    check("商城商品详情可公开访问", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------- 11. 购买：需第二个账号（不能买自己的） ----------
    buyer_name = username + "_b"
    r = requests.post(f"{BASE}/api/auth/register", timeout=20,
                      json={"username": buyer_name, "password": "buyer123456"})
    check("买家账号注册成功", r.status_code == 201, f"HTTP {r.status_code} {r.text[:120]}")
    r = requests.post(f"{BASE}/api/auth/login", timeout=20,
                      json={"username": buyer_name, "password": "buyer123456"})
    buyer_auth = {"Authorization": "Bearer " + (r.json() or {}).get("token", "")}
    check("买家登录成功", r.status_code == 200 and bool(buyer_auth["Authorization"]),
          f"HTTP {r.status_code}")

    r = requests.post(f"{BASE}/api/shop/{product_id}/buy", headers=auth, timeout=20, json={})
    check("购买自己的商品被拒", r.status_code == 409, f"HTTP {r.status_code} {r.text[:120]}")

    r = requests.post(f"{BASE}/api/shop/{product_id}/buy", timeout=20, json={})
    check("未登录购买被拒", r.status_code == 401, f"HTTP {r.status_code}")

    r = requests.post(f"{BASE}/api/shop/{product_id}/buy", headers=buyer_auth, timeout=20, json={})
    check("买家下单成功", r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}")
    order = (r.json() or {}).get("order") or {}
    check("订单返回成交价", abs(float(order.get("price", 0)) - 168.0) < 0.01, str(order.get("price")))

    # ---------- 12. 购买后状态与商城展示 ----------
    r = requests.get(f"{BASE}/api/shop/{product_id}", headers=buyer_auth, timeout=15)
    detail = r.json() or {}
    check("商品状态变为已售出", detail.get("status") == "sold", str(detail.get("status")))
    check("商品记录成交时间", bool(detail.get("sold_at")), str(detail.get("sold_at")))
    check("买家视角标记 bought_by_me", detail.get("bought_by_me") is True, str(detail.get("bought_by_me")))

    r = requests.get(f"{BASE}/api/shop?status=sold", headers=buyer_auth, timeout=20)
    sold_items = (r.json() or {}).get("items") or []
    check("商城「已售出」筛选生效", any(p.get("id") == product_id for p in sold_items),
          f"count={len(sold_items)}")

    r = requests.get(f"{BASE}/api/shop?only_bought=true", headers=buyer_auth, timeout=20)
    bought_items = (r.json() or {}).get("items") or []
    check("商城「我买到的」筛选生效", any(p.get("id") == product_id for p in bought_items),
          f"count={len(bought_items)}")

    r = requests.post(f"{BASE}/api/shop/{product_id}/buy", headers=buyer_auth, timeout=20, json={})
    check("重复购买同一商品被拒", r.status_code == 409, f"HTTP {r.status_code} {r.text[:120]}")

    # ---------- 13. 订单查询 ----------
    r = requests.get(f"{BASE}/api/orders?role=buyer", headers=buyer_auth, timeout=20)
    orders = (r.json() or {}).get("items") or []
    check("买家订单列表含新订单", any(o.get("product_id") == product_id for o in orders),
          f"count={len(orders)}")
    if orders:
        o0 = orders[0]
        check("订单含商品名快照", bool(o0.get("title")), str(o0.get("title")))
        check("订单含卖家名", bool(o0.get("seller_name")), str(o0.get("seller_name")))
        check("订单含成交价", float(o0.get("price", 0)) > 0, str(o0.get("price")))

    r = requests.get(f"{BASE}/api/orders?role=seller", headers=auth, timeout=20)
    sold_orders = (r.json() or {}).get("items") or []
    check("卖家卖出记录含新订单", any(o.get("product_id") == product_id for o in sold_orders),
          f"count={len(sold_orders)}")

    r = requests.get(f"{BASE}/api/orders", timeout=15)
    check("未登录查订单被拒", r.status_code == 401, f"HTTP {r.status_code}")

    # ---------- 14. 删除草稿（核心回归点） ----------
    r = requests.delete(f"{BASE}/api/drafts/{draft_id}", headers=auth, timeout=30)
    check("删除草稿返回 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}")
    r = requests.get(f"{BASE}/api/drafts/{draft_id}", headers=auth, timeout=15)
    check("草稿删除后不可再取", r.status_code == 404, f"HTTP {r.status_code}")

    # ---------- 15. 商品图片不随草稿删除而失效 ----------
    r = requests.get(f"{BASE}/api/products/{product_id}", headers=auth, timeout=15)
    img = r.json().get("image_path") or ""
    check("商品图片路径已转为可访问 URL", img.startswith("/outputs/"), img)
    if img:
        r = requests.get(f"{BASE}{img}", timeout=15)
        check("商品图片可正常访问", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------- 16. 删除商品 ----------
    r = requests.delete(f"{BASE}/api/products/{product_id}", headers=auth, timeout=30)
    check("删除商品返回 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}")
    r = requests.get(f"{BASE}/api/products/{product_id}", headers=auth, timeout=15)
    check("商品删除后不可再取", r.status_code == 404, f"HTTP {r.status_code}")

    # ---------- 17. 登出 ----------
    r = requests.post(f"{BASE}/api/auth/logout", headers=auth, timeout=15)
    check("登出成功", r.status_code == 200, f"HTTP {r.status_code}")
    r = requests.get(f"{BASE}/api/auth/me", headers=auth, timeout=15)
    check("登出后 /me 不再返回用户", r.status_code == 200 and not r.json().get("user"), f"HTTP {r.status_code} {r.text[:120]}")
    r = requests.get(f"{BASE}/api/products", headers=auth, timeout=15)
    check("登出后 token 访问受保护接口被拒", r.status_code == 401, f"HTTP {r.status_code}")

    failed = [x for x in results if x[0] == FAIL]
    print("\n" + "=" * 56)
    print(f"总计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for _, name, detail in failed:
        print(f"  FAIL: {name}  {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
