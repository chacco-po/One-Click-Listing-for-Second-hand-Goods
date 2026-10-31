"""生成工作台截图（用于文档/演示素材）。

用真实 Chrome(CDP) 跑完整工作台流程：注册 → 上传 → 成色问卷 → 生成文案 → 截图。

运行：python scripts/screenshot.py [输出目录]
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
PORT = 9340
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tests" / "assets"


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=60)
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
        return res.get("result", {}).get("value")

    def wait(self, expr: str, timeout: float = 60.0, interval: float = 0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expr):
                return True
            time.sleep(interval)
        return False


def launch_chrome(profile_dir: str):
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
         "--remote-allow-origins=*", "--disable-gpu", "--window-size=1280,2600", "about:blank"],
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


def inject_file(cdp: CDP, path: Path, selector: str = "#file-input") -> None:
    """把本地图片注入 <input type=file> 并触发 change。

    注：DOM.setFileInputFiles 在本机对部分路径不生效（files.length 仍为 0），
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


def shoot(cdp: CDP, path: Path) -> None:
    shot = cdp.send("Page.captureScreenshot", format="png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(shot["data"]))
    print("saved:", path.resolve())


def api_post(path: str, body: dict, token: str = "") -> str:
    """直接调后端 API（用于造买家账号 / 下单），返回 token 或空串。"""
    import urllib.request

    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("token", "")
    except Exception:
        return ""


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "shots"
    profile = tempfile.mkdtemp(prefix="idlelister-shot-")
    proc = launch_chrome(profile)
    cdp = None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{BASE}", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            cdp = CDP(json.load(r)["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Page.navigate", url=BASE)
        cdp.wait("document.readyState === 'complete'")
        cdp.wait("!!document.querySelector('#spec-select option')", timeout=25)

        # 注册
        cdp.eval("document.querySelector('#btn-auth').click()")
        time.sleep(0.4)
        cdp.eval("document.querySelector('#auth-toggle').click()")
        user = "shot" + str(int(time.time()))[-6:]
        cdp.eval(f"document.querySelector('#auth-username').value = {json.dumps(user)}")
        cdp.eval("document.querySelector('#auth-password').value = 'shot123456'")
        cdp.eval("document.querySelector('#auth-submit').click()")
        cdp.wait("!!localStorage.getItem('idlelister.token')", timeout=20)

        # 上传 → 走完整管线
        inject_file(cdp, ASSETS / "sneaker_source.jpg")
        ok = cdp.wait("!document.querySelector('#card-copywriting').classList.contains('hidden')", timeout=90)
        if not ok:
            raise RuntimeError("处理管线未完成：" + str(cdp.eval("document.querySelector('#toast').textContent")))

        # 类目 + 成色问卷
        cdp.eval("(() => { const s = document.querySelector('#category-select');"
                 " s.value = 'shoes'; s.dispatchEvent(new Event('change')); })()")
        time.sleep(1.0)
        for name, val in [("usage", "y1"), ("appearance", "light"),
                          ("function", "ok"), ("accessories", "full")]:
            cdp.eval(f"(() => {{ const r = document.querySelector('input[name=\"{name}\"][value=\"{val}\"]');"
                     " r.checked = true; r.dispatchEvent(new Event('change')); })()")
            time.sleep(0.7)

        # 少量信息 + 生成文案
        for fid, val in [("brand", "Nike Air Force 1"), ("color", "白色 42码"),
                         ("channel", "京东自营"), ("reason", "换新闲置")]:
            cdp.eval(f"document.querySelector('#copy-{fid}').value = {json.dumps(val)}")
        cdp.eval("document.querySelector('#btn-generate-copy').click()")
        cdp.wait("document.querySelectorAll('#copy-body .title-cand').length > 0", timeout=30)
        time.sleep(1.0)

        cdp.eval("window.scrollTo(0, document.querySelector('#card-copywriting').offsetTop - 20)")
        time.sleep(0.6)
        shoot(cdp, out_dir / "copywriting.png")

        # ---------- 商城截图（含已售出巨标） ----------
        token = cdp.eval("localStorage.getItem('idlelister.token')")
        # 用当前账号发布两件商品
        product_ids = []
        for i in range(2):
            inject_file(cdp, ASSETS / "sneaker_source.jpg")
            cdp.wait("!document.querySelector('#card-copywriting').classList.contains('hidden')", timeout=90)
            img_id = cdp.eval("state.imageId")
            cdp.eval("(() => { const s = document.querySelector('#category-select');"
                     " s.value = 'shoes'; s.dispatchEvent(new Event('change')); })()")
            time.sleep(0.6)
            for name, val in [("usage", "y1"), ("appearance", "light"),
                              ("function", "ok"), ("accessories", "full")]:
                cdp.eval(f"(() => {{ const r = document.querySelector('input[name=\"{name}\"][value=\"{val}\"]');"
                         " r.checked = true; r.dispatchEvent(new Event('change')); })()")
                time.sleep(0.4)
            cdp.eval("document.querySelector('#btn-publish').click()")
            cdp.wait("!!localStorage.getItem('idlelister.token')", timeout=10)
            time.sleep(2.0)
            cdp.eval("document.querySelector('#product-title').value = "
                     + json.dumps(f"闲置运动鞋 {i+1} 号"))
            cdp.eval("document.querySelector('#btn-publish').click()")
            time.sleep(2.5)
            cdp.eval("document.querySelector('#nav-products').click()")
            time.sleep(1.5)
            pid = cdp.eval("document.querySelector('#products-grid .product-card') ? "
                           "document.querySelector('#products-grid .product-card').dataset.id : ''")
            if pid:
                product_ids.append(pid)

        # 另开买家账号买走其中一件（用 API 直接注册，避免受当前登录态影响）
        buyer = "shopbuy" + str(int(time.time()))[-6:]
        buyer_token = api_post("/api/auth/register", {"username": buyer, "password": "buyer123456"})
        if product_ids and buyer_token:
            api_post(f"/api/shop/{product_ids[0]}/buy", {}, token=buyer_token)

        # 切到买家视角看商城
        cdp.eval(f"localStorage.setItem('idlelister.token', {json.dumps(buyer_token)})")
        cdp.send("Page.navigate", url=BASE)
        cdp.wait("document.readyState === 'complete'")
        cdp.wait("!!document.querySelector('#spec-select option')", timeout=25)
        cdp.eval("document.querySelector('#nav-shop').click()")
        time.sleep(2.5)
        cdp.eval("window.scrollTo(0, 0)")
        time.sleep(0.5)
        shoot(cdp, out_dir / "shop.png")

        print("\n全部截图完成 ✔")
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
