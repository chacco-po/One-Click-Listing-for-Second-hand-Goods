"""文案生成引擎单元测试（任务4）

覆盖：
- 标题候选生成与去重
- 卖点分组与来源标注
- 事实可追溯性（不臆造未提供的信息）
- 字段缺失兜底
- 非功能类目的措辞适配
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.copywriting import (  # noqa: E402
    CopyInput,
    clean_extras,
    generate_by_rules,
    generate_copy,
)

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"FAIL {name}  {detail}")


FULL = dict(
    category_id="shoes",
    grade_id="like_new_95",
    answers={"usage": "y1", "appearance": "light", "function": "ok", "accessories": "full"},
    recognized_label="运动鞋",
    recognized_confidence=0.82,
    extras={"brand": "Nike Air Force 1", "color": "白色 42码", "channel": "京东自营", "reason": "换新闲置"},
    price=288.0,
)


def test_full_input() -> None:
    r = generate_by_rules(CopyInput(**FULL))
    check("生成标题候选", len(r["titles"]) >= 2, str(len(r["titles"])))
    check("首选标题非空", bool(r["title"]), r["title"])
    check("标题不超过 60 字", all(len(t["text"]) <= 60 for t in r["titles"]))
    check("标题去重", len({t["text"] for t in r["titles"]}) == len(r["titles"]))
    check("卖点分组非空", len(r["selling_points"]) >= 3, str(len(r["selling_points"])))

    groups = {g["group"] for g in r["selling_points"]}
    check("包含成色相关分组", "使用情况" in groups and "配件清单" in groups, str(groups))

    # 每条卖点都要有来源标注
    items = [i for g in r["selling_points"] for i in g["items"]]
    check("卖点均标注来源", all(i["source"] for i in items))
    check("卖点均带生成依据", all(i["detail"] for i in items))

    check("描述包含关键信息", "Nike Air Force 1" in r["description"] and "京东自营" in r["description"])
    check("描述含售价", "288" in r["description"])
    check("引擎标识为规则", r["engine"] == "rule")


def test_no_fabrication() -> None:
    """用户未提供的信息不得出现在文案中（禁止臆造）。"""
    ci = CopyInput(
        category_id="shoes",
        grade_id="like_new_95",
        answers={"usage": "y1", "appearance": "light", "function": "ok", "accessories": "full"},
        recognized_label="运动鞋",
        extras={"brand": "Nike Air Force 1"},
    )
    r = generate_by_rules(ci)
    text = r["description"] + " " + " ".join(t["text"] for t in r["titles"])
    for forbidden in ("无拆修", "原厂包装", "专柜正品", "99新", "全新"):
        check(f"未臆造「{forbidden}」", forbidden not in text, text[:120])

    # 未提供渠道/原因时不应出现
    check("未虚构购入渠道", "购入渠道" not in r["description"])
    check("未虚构转手原因", "转手原因" not in r["description"])


def test_minimal_input_fallback() -> None:
    """字段大量缺失时仍应产出可用文案（任务要求的兜底）。"""
    ci = CopyInput(category_id="phone", grade_id="good_90", answers={}, extras={})
    r = generate_by_rules(ci)
    check("最简输入仍生成标题", bool(r["title"]), r["title"])
    check("最简输入仍生成卖点", len(r["selling_points"]) >= 1)
    check("最简输入有兜底提示", len(r["warnings"]) >= 3, str(len(r["warnings"])))
    check("提示建议补充品牌", any("品牌" in w for w in r["warnings"]))
    check("提示建议完成问卷", any("问卷" in w for w in r["warnings"]))


def test_non_function_category_wording() -> None:
    """鞋类等无"开机"概念的类目，不应出现"开机运行无异常"。"""
    ci = CopyInput(
        category_id="shoes",
        grade_id="good_90",
        answers={"function": "ok"},
        extras={},
    )
    r = generate_by_rules(ci)
    check("鞋类不使用开机类措辞", "开机" not in r["description"], r["description"])

    ci2 = CopyInput(
        category_id="phone",
        grade_id="good_90",
        answers={"function": "ok"},
        extras={},
    )
    r2 = generate_by_rules(ci2)
    check("手机类保留功能表述", "功能" in r2["description"], r2["description"])


def test_negative_grade_handling() -> None:
    """低成色不应在标题里写误导性的"X成新"。"""
    ci = CopyInput(category_id="bag", grade_id="poor_70", answers={"appearance": "broken"}, extras={})
    r = generate_by_rules(ci)
    check("低成色标题不含成新字样", "7成新" not in r["title"] and "成新" not in r["title"], r["title"])
    check("低成色仍在描述中如实体现", "破损" in r["description"], r["description"])


def test_extras_cleaning() -> None:
    cleaned = clean_extras({
        "brand": "  Nike  ",
        "color": "",
        "unknown": "不应保留",
        "reason": "x" * 100,
    })
    check("去除首尾空白", cleaned.get("brand") == "Nike", str(cleaned))
    check("丢弃空值", "color" not in cleaned)
    check("丢弃未知字段", "unknown" not in cleaned)
    check("超长字段被截断", len(cleaned.get("reason", "")) <= 30, str(len(cleaned.get("reason", ""))))


def test_engine_entry() -> None:
    r = generate_copy(CopyInput(**FULL))
    check("generate_copy 正常返回", bool(r["title"]))
    check("未配置 LLM 时使用规则引擎", r["engine"] == "rule", r["engine"])


def _fake_llm_server(payload: dict, capture: dict | None = None):
    """起一个本地假 OpenAI 兼容端点（不依赖外网）。"""
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            if capture is not None:
                capture["prompt"] = body["messages"][1]["content"]
            out = json.dumps({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def test_llm_enhancement() -> None:
    """配置 LLM 后应润色文案，且 prompt 必须携带已确认事实。"""
    import backend.copywriting as cw

    payload = {
        "titles": ["大模型标题一", "大模型标题二"],
        "selling_points": ["大模型卖点一", "大模型卖点二"],
        "description": "大模型描述正文。",
    }
    capture: dict = {}
    srv, url = _fake_llm_server(payload, capture)
    old = (cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL)
    try:
        cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL = url, "test-key", "test-model"
        r = cw.generate_copy(CopyInput(**FULL))
        check("LLM 启用后引擎标识为 llm", r["engine"] == "llm", r["engine"])
        check("LLM 标题被采用", r["title"] == "大模型标题一", r["title"])
        check("LLM 描述被采用", r["description"] == "大模型描述正文。")
        check("规则卖点分组仍保留", len(r["selling_points"]) > 1)
        check("LLM 卖点追加为独立分组",
              any(g["group"] == "综合亮点" for g in r["selling_points"]))
        # prompt 必须带事实，且带约束说明
        prompt = capture.get("prompt", "")
        check("prompt 含用户填写的品牌", "Nike" in prompt)
        check("prompt 含禁止臆造的约束", "禁止臆造" in prompt)
    finally:
        cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL = old
        srv.shutdown()


def test_llm_failure_falls_back() -> None:
    """LLM 不可用时必须降级回规则引擎，不能让主流程失败。"""
    import backend.copywriting as cw

    old = (cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL)
    try:
        # 指向一个不存在的端口
        cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL = "http://127.0.0.1:9/v1", "k", "m"
        r = cw.generate_copy(CopyInput(**FULL))
        check("LLM 失败后降级为规则引擎", r["engine"] == "rule", r["engine"])
        check("降级后仍有标题", bool(r["title"]))
        check("降级后仍有卖点", len(r["selling_points"]) >= 3)
        check("降级有明确提示", any("降级" in w or "大模型润色失败" in w for w in r["warnings"]),
              str(r["warnings"]))
    finally:
        cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL = old


def test_llm_malformed_response() -> None:
    """LLM 返回非预期结构时也必须降级，而不是抛异常。"""
    import backend.copywriting as cw

    srv, url = _fake_llm_server({"unexpected": "shape"})
    old = (cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL)
    try:
        cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL = url, "test-key", "test-model"
        r = cw.generate_copy(CopyInput(**FULL))
        check("LLM 返回异常结构时降级", r["engine"] == "rule", r["engine"])
        check("降级后文案完整", bool(r["title"]) and len(r["selling_points"]) >= 3)
    finally:
        cw.LLM_BASE_URL, cw.LLM_API_KEY, cw.LLM_MODEL = old
        srv.shutdown()


def main() -> int:
    for fn in (
        test_full_input,
        test_no_fabrication,
        test_minimal_input_fallback,
        test_non_function_category_wording,
        test_negative_grade_handling,
        test_extras_cleaning,
        test_engine_entry,
        test_llm_enhancement,
        test_llm_failure_falls_back,
        test_llm_malformed_response,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {PASSED}，失败 {len(FAILED)}")
    for f in FAILED:
        print("  FAIL:", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
