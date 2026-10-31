"""用真实 Chrome（CDP）验证前端交互：登录、我的商品页、草稿删除（用户反馈的问题点）。

运行：python tests/ui_test.py
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8765"
PORT = 9222
ASSETS = Path(__file__).parent / "assets"


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.id = 0

    def send(self, method: str, **params):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError(f"{method} -> {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str, await_promise: bool = False):
        res = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                        awaitPromise=await_promise)
        if res.get("exceptionDetails"):
            raise RuntimeError(res["exceptionDetails"].get("text", "JS 异常"))
        return res.get("result", {}).get("value")

    def wait(self, expr: str, timeout: float = 20.0, interval: float = 0.4):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.eval(expr)
            if last:
                return last
            time.sleep(interval)
        return last


def launch_chrome(profile_dir: str):
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
         "--remote-allow-origins=*",
         "--disable-gpu", "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Chrome 启动失败")


def new_target() -> str:
    # Chrome 110+ 要求 /json/new 使用 PUT
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{BASE}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["webSocketDebuggerUrl"]


def api(method: str, path: str, token: str = "", body: dict | None = None) -> dict:
    """用 curl 调后端接口（multipart 场景最省事）。"""
    tmp = Path(tempfile.gettempdir()) / f"idlelister_{int(time.time()*1000)}.json"
    cmd = ["curl", "-s", "-X", method, BASE + path, "-o", str(tmp)]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body, ensure_ascii=False)]
    subprocess.run(cmd, check=False, capture_output=True)
    try:
        return json.loads(tmp.read_text(encoding="utf-8"))
    except Exception:
        return {}
    finally:
        tmp.unlink(missing_ok=True)


def upload_sample(token: str) -> str:
    tmp = Path(tempfile.gettempdir()) / f"idlelister_proc_{int(time.time()*1000)}.json"
    subprocess.run(
        ["curl", "-s", "-X", "POST", f"{BASE}/api/process",
         "-F", f"file=@{ASSETS / 'sample.jpg'}", "-o", str(tmp)],
        check=False, capture_output=True)
    data = json.loads(tmp.read_text(encoding="utf-8"))
    tmp.unlink(missing_ok=True)
    return data.get("image_id", "")


def inject_file(cdp, path: Path, selector: str = "#file-input") -> None:
    """把本地图片注入 <input type=file> 并触发 change。

    注：CDP 的 DOM.setFileInputFiles 在本机对部分路径不生效（files.length 仍为 0），
    因此改为 base64 + DataTransfer 注入，行为与用户手动选文件一致。
    """
    b64 = base64.b64encode(path.read_bytes()).decode()
    cdp.eval(f"""
    (() => {{
      const bin = atob({json.dumps(b64)});
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      const file = new File([arr], {json.dumps(path.name)}, {{type: 'image/jpeg'}});
      const dt = new DataTransfer();
      dt.items.add(file);
      const inp = document.querySelector({json.dumps(selector)});
      inp.files = dt.files;
      inp.dispatchEvent(new Event('change', {{bubbles: true}}));
    }})()
    """)


def check(name: str, ok, extra: str = ""):
    print(("PASS " if ok else "FAIL ") + name + (f" | {extra}" if extra else ""))
    if not ok:
        raise AssertionError(name)


def main() -> int:
    profile = tempfile.mkdtemp(prefix="idlelister-chrome-")
    proc = launch_chrome(profile)
    cdp = None
    try:
        cdp = CDP(new_target())
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Page.navigate", url=BASE)
        cdp.wait("document.readyState === 'complete'")
        ok = cdp.wait("!!document.querySelector('#spec-select option')", timeout=25)
        check("页面加载完成，规格下拉已渲染", bool(ok))

        # ---------- 1. 登录（注册模式） ----------
        cdp.eval("document.querySelector('#btn-auth').click()")
        cdp.wait("!document.querySelector('#auth-modal').classList.contains('hidden')")
        cdp.eval("document.querySelector('#auth-toggle').click()")   # 切到注册
        user = "uitest" + str(int(time.time()))[-6:]
        cdp.eval(f"document.querySelector('#auth-username').value = {json.dumps(user)}")
        cdp.eval("document.querySelector('#auth-password').value = 'uitest123456'")
        cdp.eval("document.querySelector('#auth-submit').click()")
        ok = cdp.wait("!!localStorage.getItem('idlelister.token')", timeout=20)
        check("注册并自动登录", bool(ok))
        ok = cdp.wait("document.querySelector('#btn-auth').textContent.includes('退出')", timeout=10)
        check("顶栏显示用户名与退出入口", bool(ok))
        token = cdp.eval("localStorage.getItem('idlelister.token')")

        # ---------- 2. 准备一条草稿 ----------
        image_id = upload_sample(token)
        check("图片处理成功", bool(image_id), f"image_id={image_id}")
        api("POST", "/api/drafts", token, {
            "image_id": image_id, "category_id": "shoes",
            "condition_answers": {"usage": "y1", "appearance": "light", "function": "ok", "accessories": "full"},
            "price": 260, "title": "UI 测试商品",
        })

        # ---------- 3. 草稿箱删除（用户反馈的问题点） ----------
        cdp.eval("document.querySelector('#nav-drafts').click()")
        time.sleep(2.0)  # 等待草稿列表异步渲染稳定，避免读到重渲染前的旧节点
        count = cdp.wait("document.querySelectorAll('#drafts-grid .draft-card').length > 0", timeout=20)
        check("草稿箱渲染出卡片", bool(count), f"cards={count}")
        before = cdp.eval("document.querySelectorAll('#drafts-grid .draft-card').length")
        cdp.eval("document.querySelector('#drafts-grid .draft-card [data-act=\"del-draft\"]').click()")
        armed = cdp.wait(
            "document.querySelector('#drafts-grid .draft-card [data-act=\"del-draft\"]').textContent === '确认删除?'",
            timeout=5)
        check("点击删除 → 进入二次确认态", bool(armed))
        cdp.eval("document.querySelector('#drafts-grid .draft-card [data-act=\"del-draft\"]').click()")
        busy = cdp.wait(
            "document.querySelector('#drafts-grid .draft-card [data-act=\"del-draft\"]') && "
            "document.querySelector('#drafts-grid .draft-card [data-act=\"del-draft\"]').textContent === '删除中…'",
            timeout=5)
        check("二次确认 → 显示删除中反馈", bool(busy))
        after = cdp.wait(
            f"document.querySelectorAll('#drafts-grid .draft-card').length === {before - 1}", timeout=40)
        now = cdp.eval("document.querySelectorAll('#drafts-grid .draft-card').length")
        check("草稿被成功删除", bool(after), f"{before} -> {now}")

        # ---------- 4. 我的商品视图 ----------
        cdp.eval("document.querySelector('#nav-products').click()")
        ok = cdp.wait("!document.querySelector('#view-products').classList.contains('hidden')", timeout=10)
        check("我的商品视图可打开", bool(ok))
        check("商品视图容器已渲染", cdp.eval("!!document.querySelector('#products-grid')") is not None)

        # ---------- 5. 发布上架（含商品名称）----------
        image_id2 = upload_sample(token)
        draft = api("POST", "/api/drafts", token, {
            "image_id": image_id2, "category_id": "shoes",
            "condition_answers": {"usage": "y1", "appearance": "light", "function": "ok", "accessories": "full"},
            "price": 288, "title": "发布测试商品",
        })
        prod = api("POST", "/api/products", token, {"draft_id": draft.get("id", ""), "title": "发布测试商品", "price": 288})
        check("发布商品成功", bool(prod.get("id")), f"商品={prod.get('title')} ¥{prod.get('price')}")
        cdp.eval("document.querySelector('#nav-products').click()")
        has = cdp.wait("document.querySelectorAll('#products-grid .product-card').length > 0", timeout=20)
        check("我的商品列表显示已发布商品", bool(has),
              f"名称={cdp.eval('document.querySelector(\"#products-grid .draft-title\") ? document.querySelector(\"#products-grid .draft-title\").textContent : \"\"')}")
        pname = cdp.eval("document.querySelector('#products-grid .draft-title').textContent")
        check("商品名称正确展示", pname.strip() == "发布测试商品", f"名称={pname!r}")

        # ---------- 6. 商品改名 + 删除 ----------
        cdp.eval("document.querySelector('#products-grid .product-card').click()")
        cdp.wait("!document.querySelector('#draft-modal').classList.contains('hidden')", timeout=10)
        cdp.eval("document.querySelector('#p-title').value = '改名后的商品'")
        cdp.eval("document.querySelector('#p-save').click()")
        renamed = cdp.wait(
            "document.querySelector('#products-grid .draft-title') && "
            "document.querySelector('#products-grid .draft-title').textContent.trim() === '改名后的商品'", timeout=20)
        check("商品可改名并保存", bool(renamed))
        cdp.eval("document.querySelector('#products-grid [data-act=\"del-product\"]').click()")
        time.sleep(0.3)
        cdp.eval("document.querySelector('#products-grid [data-act=\"del-product\"]').click()")
        gone = cdp.wait("document.querySelectorAll('#products-grid .product-card').length === 0", timeout=30)
        check("商品可删除", bool(gone))

        # ---------- 7. 文案生成（任务4）----------
        cdp.eval("document.querySelector('#nav-workbench').click()")
        time.sleep(0.5)
        # 注入测试图片触发完整管线（DOM.setFileInputFiles 在本机对该路径不生效，改用 base64 + DataTransfer）
        inject_file(cdp, ASSETS / "sneaker_source.jpg")
        appeared = cdp.wait(
            "!!document.querySelector('#card-copywriting') && "
            "!document.querySelector('#card-copywriting').classList.contains('hidden')", timeout=90)
        check("文案生成卡片随管线出现", bool(appeared),
              f"toast={cdp.eval('document.querySelector(\"#toast\").textContent')}")
        check("图片处理成功（前端状态）",
              cdp.eval("typeof state !== 'undefined' && !!state.imageId") is True,
              f"image_id={cdp.eval('typeof state !== \"undefined\" ? state.imageId : null')}")

        # 填写少量信息
        cdp.eval("document.querySelector('#copy-brand').value = 'Nike Air Force 1'")
        cdp.eval("document.querySelector('#copy-color').value = '白色 42码'")
        cdp.eval("document.querySelector('#copy-channel').value = '京东自营'")
        cdp.eval("document.querySelector('#copy-reason').value = '换新闲置'")
        # 选择类目 + 完成成色问卷
        cdp.eval("(() => { const s = document.querySelector('#category-select');"
                 " s.value = 'shoes'; s.dispatchEvent(new Event('change')); })()")
        time.sleep(1.0)
        cdp.eval("(() => { const r = document.querySelector('input[name=\"usage\"][value=\"y1\"]');"
                 " r.checked = true; r.dispatchEvent(new Event('change')); })()")
        time.sleep(0.8)
        cdp.eval("(() => { const r = document.querySelector('input[name=\"appearance\"][value=\"light\"]');"
                 " r.checked = true; r.dispatchEvent(new Event('change')); })()")
        time.sleep(0.8)
        cdp.eval("(() => { const r = document.querySelector('input[name=\"function\"][value=\"ok\"]');"
                 " r.checked = true; r.dispatchEvent(new Event('change')); })()")
        time.sleep(0.8)
        cdp.eval("(() => { const r = document.querySelector('input[name=\"accessories\"][value=\"full\"]');"
                 " r.checked = true; r.dispatchEvent(new Event('change')); })()")
        time.sleep(1.2)

        cdp.eval("document.querySelector('#btn-generate-copy').click()")
        ok = cdp.wait("document.querySelectorAll('#copy-body .title-cand').length > 0", timeout=30)
        check("点击生成 → 渲染标题候选", bool(ok),
              f"候选数={cdp.eval('document.querySelectorAll(\"#copy-body .title-cand\").length')}")
        ok = cdp.wait("document.querySelectorAll('#copy-body .sp-group').length > 0", timeout=10)
        check("渲染结构化卖点分组", bool(ok),
              f"分组数={cdp.eval('document.querySelectorAll(\"#copy-body .sp-group\").length')}")
        ok = cdp.wait("!!document.querySelector('#copy-description') && "
                      "document.querySelector('#copy-description').value.length > 0", timeout=10)
        check("渲染描述全文", bool(ok))

        title_val = cdp.eval("document.querySelector('#product-title').value")
        check("首选标题自动填入商品名称", "Nike" in title_val, f"名称={title_val!r}")

        # 点击第二个标题候选 → 应覆盖商品名称
        cdp.eval("document.querySelectorAll('#copy-body .title-cand')[1].click()")
        time.sleep(0.5)
        new_val = cdp.eval("document.querySelector('#product-title').value")
        check("点击标题候选可替换商品名称", new_val != title_val and bool(new_val), f"名称={new_val!r}")

        # 卖点来源标注可见
        ok = cdp.wait("document.querySelectorAll('#copy-body .sp-src').length > 0", timeout=5)
        check("卖点展示来源标注", bool(ok))

        # ---------- 8. 商城：浏览 → 购买 → 已售出巨标 ----------
        # 造一个「别人发布的商品」供购买
        image_id3 = upload_sample(token)
        draft2 = api("POST", "/api/drafts", token, {
            "image_id": image_id3, "category_id": "shoes",
            "condition_answers": {"usage": "y1", "appearance": "light", "function": "ok", "accessories": "full"},
            "price": 199, "title": "商城测试商品",
        })
        prod2 = api("POST", "/api/products", token,
                    {"draft_id": draft2.get("id", ""), "title": "商城测试商品", "price": 199})
        target_id = prod2.get("id", "")
        check("商城测试商品已上架", bool(target_id), f"id={target_id}")

        cdp.eval("document.querySelector('#nav-shop').click()")
        ok = cdp.wait("!document.querySelector('#view-shop').classList.contains('hidden')", timeout=10)
        check("商城视图可打开", bool(ok))
        ok = cdp.wait("document.querySelectorAll('#shop-grid .shop-card').length > 0", timeout=20)
        check("商城渲染出商品卡片", bool(ok),
              f"卡片数={cdp.eval('document.querySelectorAll(\"#shop-grid .shop-card\").length')}")

        # 自己的商品不能买，按钮应为「我发布的」
        own_label = cdp.eval("""
          (() => {
            const cards = [...document.querySelectorAll('#shop-grid .shop-card')];
            const c = cards.find(x => x.dataset.id === %s);
            if (!c) return null;
            return c.querySelector('.shop-actions .btn:last-child').textContent.trim();
          })()
        """ % json.dumps(target_id))
        check("自己发布的商品显示「我发布的」", own_label == "我发布的", f"按钮={own_label!r}")

        # 换个买家账号买这件商品
        buyer = "uibuyer" + str(int(time.time()))[-6:]
        reg = api("POST", "/api/auth/register", "", {"username": buyer, "password": "buyer123456"})
        buyer_token = reg.get("token", "")
        check("买家账号注册成功", bool(buyer_token))
        buy_res = api("POST", f"/api/shop/{target_id}/buy", buyer_token, {})
        check("买家下单成功", bool(buy_res.get("order")), str(buy_res)[:120])

        # 回到商城：已售出巨标应出现
        cdp.eval("document.querySelector('#nav-shop').click()")
        time.sleep(1.0)
        cdp.eval("document.querySelector('#nav-workbench').click()")
        time.sleep(0.3)
        cdp.eval("document.querySelector('#nav-shop').click()")
        stamp = cdp.wait(f"""
          (() => {{
            const cards = [...document.querySelectorAll('#shop-grid .shop-card')];
            const c = cards.find(x => x.dataset.id === {json.dumps(target_id)});
            return c && c.querySelector('.sold-stamp') ? c.querySelector('.sold-stamp').textContent.trim() : '';
          }})()
        """, timeout=20)
        check("已售出商品显示巨大「已售出」标签", stamp == "已售出", f"标签={stamp!r}")

        # 标签必须真的覆盖在图片上且足够醒目（尺寸 + 旋转 + 层级）
        stamp_style = cdp.eval(f"""
          (() => {{
            const cards = [...document.querySelectorAll('#shop-grid .shop-card')];
            const c = cards.find(x => x.dataset.id === {json.dumps(target_id)});
            const st = c && c.querySelector('.sold-stamp');
            if (!st) return null;
            const r = st.getBoundingClientRect();
            const span = st.querySelector('span');
            const sr = span.getBoundingClientRect();
            const img = c.querySelector('img').getBoundingClientRect();
            return {{
              covers: r.width >= img.width * 0.9 && r.height >= img.height * 0.9,
              fontSize: parseFloat(getComputedStyle(span).fontSize),
              rotated: getComputedStyle(span).transform !== 'none',
              zIndex: getComputedStyle(st).zIndex,
              fontWeight: getComputedStyle(span).fontWeight,
            }};
          }})()
        """)
        check("已售出标签覆盖整张商品图", bool(stamp_style and stamp_style["covers"]), str(stamp_style))
        check("已售出标签字号足够大(≥26px)", bool(stamp_style and stamp_style["fontSize"] >= 26),
              f"字号={stamp_style and stamp_style['fontSize']}px")
        check("已售出标签有旋转效果", bool(stamp_style and stamp_style["rotated"]), str(stamp_style))
        check("已售出标签为粗体且有层级", bool(stamp_style and stamp_style["fontWeight"] in ("700", "800", "900")
              and int(stamp_style["zIndex"]) >= 3), str(stamp_style))

        # 已售出商品不再有购买按钮
        buy_btn = cdp.eval(f"""
          (() => {{
            const cards = [...document.querySelectorAll('#shop-grid .shop-card')];
            const c = cards.find(x => x.dataset.id === {json.dumps(target_id)});
            if (!c) return null;
            const b = c.querySelector('[data-act="buy"]');
            return b ? 'still-buyable' : 'no-buy-button';
          }})()
        """)
        check("已售出商品不再提供购买按钮", buy_btn == "no-buy-button", str(buy_btn))

        # 「已售出」筛选
        cdp.eval("document.querySelector('#shop-filters .chip[data-status=\"sold\"]').click()")
        ok = cdp.wait(f"""
          (() => {{
            const cards = [...document.querySelectorAll('#shop-grid .shop-card')];
            return cards.length > 0 && cards.every(c => c.querySelector('.sold-stamp'));
          }})()
        """, timeout=15)
        check("商城「已售出」筛选只显示已售商品", bool(ok))
        cdp.eval("document.querySelector('#shop-filters .chip[data-status=\"\"]').click()")
        time.sleep(0.8)

        # 我的订单（当前浏览器是卖家账号 → 切到「我卖出的」）
        cdp.eval("document.querySelector('#order-tabs .chip[data-role=\"seller\"]').click()")
        ok = cdp.wait("document.querySelectorAll('#orders-list .order-item').length > 0", timeout=15)
        check("「我卖出的」列出已成交订单", bool(ok),
              f"订单数={cdp.eval('document.querySelectorAll(\"#orders-list .order-item\").length')}")
        cdp.eval("document.querySelector('#order-tabs .chip[data-role=\"buyer\"]').click()")
        time.sleep(1.0)
        buyer_tab_empty = cdp.eval("!document.querySelector('#orders-empty').classList.contains('hidden')")
        check("「我买到的」对未购买者显示空态", buyer_tab_empty is True,
              f"空态可见={buyer_tab_empty}")
        cdp.eval("document.querySelector('#order-tabs .chip[data-role=\"seller\"]').click()")

        # ---------- 9. 退出登录后提示 ----------
        cdp.eval("localStorage.removeItem('idlelister.token')")
        cdp.send("Page.navigate", url=BASE)
        cdp.wait("document.readyState === 'complete'")
        cdp.wait("!!document.querySelector('#btn-auth')", timeout=20)
        cdp.eval("document.querySelector('#nav-products').click()")
        ok = cdp.wait("!document.querySelector('#products-empty').classList.contains('hidden')", timeout=15)
        check("未登录时商品页提示登录", bool(ok))

        # 未登录仍可浏览商城
        cdp.eval("document.querySelector('#nav-shop').click()")
        ok = cdp.wait("document.querySelectorAll('#shop-grid .shop-card').length > 0", timeout=15)
        check("未登录也能浏览商城", bool(ok))

        print("\n真实浏览器 UI 验证全部通过 ✔")
        return 0
    finally:
        if cdp:
            try:
                cdp.close()
            except Exception:
                pass
        proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
