"""前端静态校验：JS 语法 + DOM id 引用一致性（避免运行时白屏/事件不绑定）。"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "frontend" / "app.js"
HTML = ROOT / "frontend" / "index.html"
CSS = ROOT / "frontend" / "styles.css"

# 1) JS 语法检查
node = r"C:\Users\我爱帅哥\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
res = subprocess.run([node, "--check", str(JS)], capture_output=True, text=True)
if res.returncode != 0:
    print("JS 语法错误:\n", res.stderr)
    sys.exit(1)
print("PASS app.js 语法检查")

# 2) 动态创建的 HTML 片段中的 id（innerHTML 模板）也算存在
html = HTML.read_text(encoding="utf-8")
js = JS.read_text(encoding="utf-8")
css = CSS.read_text(encoding="utf-8")

static_ids = set(re.findall(r'id="([^"]+)"', html))
# 模板字符串里生成的 id
dynamic_ids = set(re.findall(r'id="([a-zA-Z0-9_\-]+)"', js))
referenced = set(re.findall(r"\$\('#([a-zA-Z0-9_\-]+)'\)", js))

missing = sorted(referenced - static_ids - dynamic_ids)
if missing:
    print("FAIL 以下 id 在 HTML/模板中不存在:", missing)
    sys.exit(1)
print(f"PASS DOM id 引用一致（引用 {len(referenced)} 个，全部可解析）")

# 3) 检查关键新功能是否已接入
checks = {
    "登录接口": "/api/auth/login" in js,
    "注册接口": "/api/auth/register" in js,
    "商品列表": "/api/products" in js,
    "发布接口": "'/api/products'" in js and "publishFromDraft" in js,
    "商品名称输入": "product-title" in html and "product-title" in js,
    "草稿删除(行内)": 'data-act="del-draft"' in js,
    "商品删除(行内)": 'data-act="del-product"' in js,
    "两步确认(无 confirm)": "confirm(" not in js,
    "携带 token": "Authorization" in js,
    "文案生成接口": "/api/copywriting" in js,
    "文案生成按钮": "btn-generate-copy" in html and "btn-generate-copy" in js,
    "文案卡片存在": "card-copywriting" in html,
    "标题候选渲染": "title-cand" in js,
    "卖点分组渲染": "sp-group" in js,
    "文案补充信息字段": "copy-fields" in html and "copy-fields" in js,
    "文案随草稿保存": "collectCopyPayload" in js and "selling_points" in js,
    "文案失效重生成": "invalidateCopy" in js,
    # --- 商城与购买 ---
    "商城列表接口": "/api/shop" in js,
    "购买接口": "/buy" in js and "buyItem" in js,
    "订单接口": "/api/orders" in js,
    "商城视图存在": "view-shop" in html,
    "商城导航入口": "nav-shop" in html and "nav-shop" in js,
    "商城筛选(状态/类目/关键词/排序)":
        "shop-filters" in html and "shop-category" in html
        and "shop-keyword" in html and "shop-sort" in html,
    "已售出巨标渲染": "sold-stamp" in js and "已售出" in js,
    "已售出巨标样式": "sold-stamp" in css,
    "订单双角色切换": "order-tabs" in html and "orderRole" in js,
    "购买需登录拦截": "请先登录后再购买" in js,
}
for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        sys.exit(1)
print("\n全部前端静态校验通过 ✔")
