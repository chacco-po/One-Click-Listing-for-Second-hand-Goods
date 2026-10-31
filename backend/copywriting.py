"""标题与描述文案生成（任务4）

依据「识别结果 + 成色问卷 + 用户填写的少量信息」生成结构化标题与卖点描述。

设计要点
--------
1. **纯规则引擎，零外部依赖**：默认路径不调用任何模型，纯 Python 模板拼装，
   保证离线/演示环境下稳定可复现（与 pricing.py 同风格）。
2. **所有主张可追溯**：文案里的每一句只允许来自三类输入——
   识别结果、成色问卷、用户填写字段。绝不臆造「无拆修」「原厂包装」之类未提供的事实。
   用户没提供的信息一律降级为中性表述或直接省略。
3. **结构化输出**：标题给出多个风格候选，卖点按「成色 / 功能 / 配件 / 外观 / 场景」分组，
   每条卖点带 ``from`` 字段标明来源，前端可据此展示"依据"。
4. **可插拔 LLM 增强**：配置 ``COPYWRITING_LLM_BASE_URL`` 等环境变量后，
   会把规则引擎产出的草稿交给 OpenAI 兼容接口润色；失败自动降级回规则结果，
   不影响主流程（与 matting.py 的多后端降级策略一致）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from backend.pricing import CATEGORIES, GRADE_MAP

# ------------------------------------------------------------------ 用户填写字段定义

#: 用户只需填写少量信息；每个字段给出 label/placeholder/max_length，供前端动态渲染
EXTRA_FIELDS: list[dict] = [
    {
        "id": "brand",
        "label": "品牌 / 型号",
        "placeholder": "例如：Apple iPhone 13 128G",
        "max_length": 40,
    },
    {
        "id": "color",
        "label": "颜色 / 规格",
        "placeholder": "例如：黑色 / 42 码",
        "max_length": 30,
    },
    {
        "id": "channel",
        "label": "购入渠道（可选）",
        "placeholder": "例如：京东自营 / 朋友赠送",
        "max_length": 30,
    },
    {
        "id": "reason",
        "label": "转手原因（可选）",
        "placeholder": "例如：换新设备 / 搬家闲置",
        "max_length": 30,
    },
]

EXTRA_FIELD_IDS = [f["id"] for f in EXTRA_FIELDS]


def clean_extras(extras: dict | None) -> dict[str, str]:
    """清洗用户填写的少量信息：去空白、限长、丢弃空值。"""
    extras = extras or {}
    out: dict[str, str] = {}
    for f in EXTRA_FIELDS:
        raw = extras.get(f["id"])
        if not isinstance(raw, str):
            continue
        value = raw.strip()[: f["max_length"]]
        if value:
            out[f["id"]] = value
    return out


# ------------------------------------------------------------------ 成色问卷 → 文案素材

#: 问卷答案 → 卖点短语。
#: 每项为 (描述短语, 标题用短词)；描述短语用于正文，短词用于标题，避免标题过长。
ANSWER_PHRASES: dict[str, dict[str, tuple[str, str]]] = {
    "usage": {
        "sealed": ("未拆封，配件齐全", "未拆封"),
        "m3": ("入手 3 个月以内，使用频率不高", "使用少"),
        "y1": ("使用约半年，日常爱惜使用", "使用半年"),
        "y2": ("使用 1-2 年，功能稳定", "用了1-2年"),
        "y2p": ("使用 2 年以上，各项功能依旧正常", "长期使用"),
    },
    "appearance": {
        "none": ("外观干净整洁，几乎无使用痕迹", "无明显痕迹"),
        "light": ("边角有轻微使用痕迹，不影响正常使用", "轻微痕迹"),
        "visible": ("有明显磨损与使用痕迹，已如实拍摄", "磨损已标注"),
        "broken": ("存在破损或变形，已在图中标出", "有破损已标注"),
    },
    "function": {
        "ok": ("各项功能正常，开机运行无异常", "功能完好"),
        "minor": ("存在偶发小问题，已在描述中说明", "偶发小问题"),
        "broken": ("存在功能故障，售前请先沟通", "有故障已说明"),
    },
    "accessories": {
        "full": ("原装配件、包装盒、说明书齐全", "配件齐全"),
        "partial": ("主要配件保留，缺失部分已注明", "部分配件"),
        "none": ("仅主品出售，配件需另行确认", "仅主品"),
    },
}

#: 这些类目没有"开机/运行"概念，功能状态改用中性表述，避免"开机运行无异常"这类不贴合的文案
NON_FUNCTION_CATEGORIES: set[str] = {
    "shoes", "bag", "furniture_chair", "furniture_table", "furniture_sofa",
    "storage", "plant", "clothing_top", "clothing_bottom", "watch_acc",
    "books", "baby", "pet", "beauty", "kitchen", "daily", "sports_gear",
}
NON_FUNCTION_PHRASES = {
    "ok": ("使用状态正常，可正常使用", "状态正常"),
    "minor": ("有轻微小问题，已在描述中说明", "有小问题"),
    "broken": ("存在破损或故障，售前请先沟通", "有瑕疵已说明"),
}

#: 问卷题目 id → 卖点分组名
ANSWER_GROUP: dict[str, str] = {
    "usage": "使用情况",
    "appearance": "外观状态",
    "function": "功能状态",
    "accessories": "配件清单",
}


# ------------------------------------------------------------------ 类目专属卖点模板

#: 按类目给出"该品类买家最关心什么"的短语；仅在用户对应问卷答案非负向时才使用
CATEGORY_HIGHLIGHTS: dict[str, list[str]] = {
    "phone": ["屏幕显示正常，触控灵敏", "电池续航正常，充电正常"],
    "laptop": ["键盘手感正常，散热正常", "屏幕无坏点，接口齐全"],
    "tablet": ["屏幕显示正常，触控灵敏", "电池续航正常，充电正常"],
    "headphone": ["音质正常，无破音", "降噪/通话功能正常"],
    "camera": ["快门/录制正常，取景正常", "镜头无霉无划痕"],
    "wearable": ["显示与计步正常", "佩戴舒适，电池正常"],
    "console": ["开机运行正常，手柄连接正常", "游戏支持正常"],
    "monitor": ["显示无坏点，无闪屏", "接口齐全，可正常连接"],
    "appliance_small": ["运行正常，功能齐全", "外观干净，便于清洁"],
    "appliance_large": ["运行正常，功能齐全", "已清洁内外机，运转噪音正常"],
    "furniture_chair": ["结构稳固，坐感舒适", "表面完好，易清洁"],
    "furniture_table": ["结构稳固，桌面平整", "边角完好，易搬运"],
    "furniture_sofa": ["面料完好，坐感舒适", "骨架稳固，无塌陷"],
    "storage": ["收纳容量实用", "结构完好，可叠放"],
    "plant": ["状态健康，生长良好", "已配好花盆，便于摆放"],
    "clothing_top": ["面料舒适，版型正常", "洗护良好，无明显褪色"],
    "clothing_bottom": ["面料舒适，版型正常", "洗护良好，无明显褪色"],
    "shoes": ["鞋底磨损程度与图中一致", "鞋面已清洁，尺码正常"],
    "bag": ["皮面/面料完好，五金正常", "内部干净，容量实用"],
    "watch_acc": ["走时精准，表镜完好", "表带完好，可正常佩戴"],
    "sports_gear": ["器材完好，可正常使用", "已清洁，运动表现正常"],
    "ball_racket": ["器材完好，握拍手感正常", "拍面/球体状态良好"],
    "outdoor": ["装备完好，户外表现正常", "已清洁晾干，携带方便"],
    "bicycle": ["骑行正常，刹车变速正常", "已清洁上油，轮胎状态良好"],
    "books": ["正版无缺页，无笔记涂写", "保存良好，无水渍"],
    "instrument": ["音准正常，演奏状态良好", "配件齐全，适合入门练习"],
    "toy": ["配件齐全，功能正常", "已清洁，适合作为礼物"],
    "baby": ["材质安全，使用状态良好", "已彻底清洁消毒"],
    "pet": ["用品完好，清洁到位", "无异味，可直接使用"],
    "beauty": ["未开封或使用次数很少", "保质期内，包装完好"],
    "personal_care": ["运行正常，清洁卫生", "配件齐全，收纳方便"],
    "kitchen": ["洁净无异味，可直接使用", "设计实用，易清洗"],
    "daily": ["实用耐用品，日常好打理", "无破损，可直接使用"],
    "vehicle": ["启动正常，行驶状态良好", "已保养，证件可过户"],
    "other": ["状态良好，可正常使用", "已清洁，包装完好"],
}


# ------------------------------------------------------------------ 成色 → 标题前缀

GRADE_TITLE_TAG: dict[str, str] = {
    "brand_new": "全新未拆",
    "like_new_99": "99新",
    "like_new_95": "95新",
    "good_90": "9成新",
    "fair_80": "8成新",
    "poor_70": "7成新",
}

#: 负向成色在标题里不写"95新"这类误导词，改用中性描述
GRADE_NEGATIVE: set[str] = {"poor_70"}


# ------------------------------------------------------------------ 数据结构


@dataclass
class CopyInput:
    """文案生成的输入（全部来源于识别结果 / 问卷 / 用户填写）。"""

    category_id: str
    grade_id: str
    answers: dict[str, str] = field(default_factory=dict)
    recognized_label: str | None = None       # 识别出的物品名（中文）
    recognized_confidence: float | None = None
    extras: dict[str, str] = field(default_factory=dict)
    price: float | None = None
    original_price: float | None = None        # 参考价区间上限，用于"原价"对照（有据才写）


@dataclass
class SellingPoint:
    group: str
    text: str
    source: str            # 成色问卷 / 用户填写 / 类目特征 / 识别结果
    detail: str = ""       # 生成依据的原文，便于前端展示"为什么这么写"
    short: str = ""        # 精简版（用于标题，避免标题过长）

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "text": self.text,
            "source": self.source,
            "detail": self.detail,
        }


# ------------------------------------------------------------------ 核心：规则引擎


def _item_name(copy_in: CopyInput) -> str:
    """物品名称：优先用用户填的品牌型号，其次识别结果，最后类目标签。"""
    brand = copy_in.extras.get("brand")
    if brand:
        return brand
    if copy_in.recognized_label:
        return copy_in.recognized_label
    return CATEGORIES.get(copy_in.category_id, {}).get("label", "闲置物品")


def _category_label(copy_in: CopyInput) -> str:
    return CATEGORIES.get(copy_in.category_id, {}).get("label", "闲置物品")


def _grade_label(copy_in: CopyInput) -> str:
    return GRADE_MAP.get(copy_in.grade_id, {}).get("label", "")


def _build_selling_points(copy_in: CopyInput) -> list[SellingPoint]:
    """按分组生成卖点，每条都标注来源。"""
    points: list[SellingPoint] = []
    non_function = copy_in.category_id in NON_FUNCTION_CATEGORIES

    # 1) 成色问卷 → 卖点
    for qid, group in ANSWER_GROUP.items():
        value = copy_in.answers.get(qid)
        if not value:
            continue
        table = ANSWER_PHRASES.get(qid, {})
        phrase = table.get(value)
        if not phrase:
            continue
        text, short = phrase
        if qid == "function" and non_function:
            alt = NON_FUNCTION_PHRASES.get(value)
            if alt:
                text, short = alt
        points.append(SellingPoint(
            group=group,
            text=text,
            source="成色问卷",
            detail=f"{group}问卷选择「{_answer_label(qid, value)}」",
            short=short,
        ))

    # 2) 类目特征 → 卖点（只在该类目有专属素材时给出）
    highlights = CATEGORY_HIGHLIGHTS.get(copy_in.category_id, [])
    if highlights:
        points.append(SellingPoint(
            group="品类要点",
            text=highlights[0],
            source="类目特征",
            detail=f"「{_category_label(copy_in)}」品类的通用检查要点",
            short=highlights[1] if len(highlights) > 1 else highlights[0],
        ))

    # 3) 用户填写 → 卖点（分组名已说明含义，正文不再重复前缀）
    channel = copy_in.extras.get("channel")
    if channel:
        points.append(SellingPoint(
            group="购入渠道",
            text=channel,
            source="用户填写",
            detail=channel,
            short="",
        ))
    reason = copy_in.extras.get("reason")
    if reason:
        points.append(SellingPoint(
            group="转手原因",
            text=reason,
            source="用户填写",
            detail=reason,
            short="",
        ))
    color = copy_in.extras.get("color")
    if color:
        points.append(SellingPoint(
            group="商品信息",
            text=color,
            source="用户填写",
            detail=color,
            short=color,
        ))

    # 4) 识别结果 → 卖点
    if copy_in.recognized_label:
        conf = copy_in.recognized_confidence
        detail = f"识别为「{copy_in.recognized_label}」"
        if conf:
            detail += f"，置信度 {round(conf * 100)}%"
        points.append(SellingPoint(
            group="识别信息",
            text=f"实拍为{copy_in.recognized_label}",
            source="识别结果",
            detail=detail,
            short="",
        ))

    return points


def _answer_label(qid: str, value: str) -> str:
    from backend.pricing import CONDITION_QUESTIONS

    for q in CONDITION_QUESTIONS:
        if q["id"] == qid:
            for o in q["options"]:
                if o["value"] == value:
                    return o["label"]
    return value


def _group_points(points: list[SellingPoint]) -> list[dict]:
    """按分组聚合卖点，保持出现顺序。"""
    order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for p in points:
        if p.group not in buckets:
            buckets[p.group] = []
            order.append(p.group)
        buckets[p.group].append(p.to_dict())
    return [{"group": g, "items": buckets[g]} for g in order]


def _title_candidates(copy_in: CopyInput, points: list[SellingPoint]) -> list[dict]:
    """生成多个风格的结构化标题候选。

    每个候选都带 ``template``（用了什么结构）与 ``fields``（各段文本），便于前端解释与二次编辑。
    """
    name = _item_name(copy_in)
    color = copy_in.extras.get("color", "")
    grade_label = _grade_label(copy_in)
    grade_tag = GRADE_TITLE_TAG.get(copy_in.grade_id, "")
    category = _category_label(copy_in)

    # 负向成色不写"X成新"，避免给买家错误预期
    if copy_in.grade_id in GRADE_NEGATIVE:
        grade_tag = ""

    # 核心短语：从成色 + 识别名拼出"物品 + 成色"
    core = name if not grade_tag else f"{name} {grade_tag}"

    # 若用户填了品牌型号且品牌里已含品类词，则不再重复追加品类词
    has_category = category and category in name

    candidates: list[dict] = []

    # 候选 1：标准型 —— 品牌 + 规格 + 成色
    parts = [core]
    if color:
        parts.append(color)
    candidates.append({
        "style": "标准",
        "text": " ".join(p for p in parts if p)[:60],
        "template": "物品名 + 成色 + 规格",
        "fields": {"名称": core, "规格": color, "成色": grade_tag},
    })

    # 候选 2：品类前置 —— 便于按品类浏览时识别
    if not has_category and category:
        parts2 = [category]
        if grade_tag:
            parts2.append(grade_tag)
        parts2.append(name)
        if color:
            parts2.append(color)
        candidates.append({
            "style": "品类优先",
            "text": " ".join(parts2)[:60],
            "template": "品类 + 成色 + 名称 + 规格",
            "fields": {"品类": category, "成色": grade_tag, "名称": name, "规格": color},
        })

    # 候选 3：卖点前置 —— 把最强的一条卖点提到标题
    highlight_short, highlight_text = _pick_highlight(points)
    if highlight_short:
        parts3 = [highlight_short]
        if grade_tag:
            parts3.append(grade_tag)
        parts3.append(name)
        candidates.append({
            "style": "卖点前置",
            "text": " ".join(parts3)[:60],
            "template": "核心卖点 + 成色 + 名称",
            "fields": {"卖点": highlight_text, "成色": grade_tag, "名称": name},
        })

    # 候选 4：简洁型 —— 去掉一切修饰
    simple = name if not color else f"{name} {color}"
    candidates.append({
        "style": "简洁",
        "text": simple[:60],
        "template": "名称 + 规格",
        "fields": {"名称": name, "规格": color},
    })

    # 去重（同名不同风格保留前一个）
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        if not c["text"] or c["text"] in seen:
            continue
        seen.add(c["text"])
        unique.append(c)
    return unique


def _pick_highlight(points: list[SellingPoint]) -> tuple[str, str]:
    """挑选最适合放进标题的一条卖点：返回 (短词, 完整短语)。

    优先正面且信息量高的分组；负向描述不适合当标题卖点
    （"存在破损变形"会吓跑买家，但必须如实保留在卖点正文里）。
    """
    priority = ["功能状态", "配件清单", "使用情况", "外观状态", "品类要点"]
    by_group = {p.group: p for p in points}
    negative = ("破损", "故障", "变形", "异味", "变色", "瑕疵已说明", "有问题")
    for g in priority:
        p = by_group.get(g)
        if not p or not p.short:
            continue
        if any(w in p.text for w in negative) or any(w in p.short for w in negative):
            continue
        return p.short, p.text
    return "", ""


def _description(copy_in: CopyInput, points: list[SellingPoint]) -> str:
    """生成一段结构化描述文本（分组要点串联）。"""
    name = _item_name(copy_in)
    grade_label = _grade_label(copy_in)

    head = f"{name}"
    if copy_in.grade_id not in GRADE_NEGATIVE and grade_label:
        head += f" · {grade_label}"
    if copy_in.extras.get("color"):
        head += f" · {copy_in.extras['color']}"

    lines = [head]
    for group in _group_points(points):
        texts = [i["text"] for i in group["items"]]
        if texts:
            lines.append(f"【{group['group']}】" + "；".join(texts))
    if copy_in.price:
        lines.append(f"【售价】{copy_in.price:.0f} 元，可小刀，欢迎私聊沟通。")
    return "\n".join(lines)


# ------------------------------------------------------------------ 规则引擎入口


def generate_by_rules(copy_in: CopyInput) -> dict:
    """规则引擎生成文案（默认路径，无外部依赖）。"""
    points = _build_selling_points(copy_in)
    candidates = _title_candidates(copy_in, points)
    grouped = _group_points(points)

    basis: list[str] = []
    if copy_in.recognized_label:
        basis.append(f"识别结果：{copy_in.recognized_label}")
    if copy_in.extras:
        basis.append("用户填写：" + "、".join(copy_in.extras.values()))
    if copy_in.answers:
        basis.append("成色问卷：" + "、".join(copy_in.answers.values()))

    return {
        "titles": candidates,
        "title": candidates[0]["text"] if candidates else "",
        "selling_points": grouped,
        "description": _description(copy_in, points),
        "engine": "rule",
        "basis": basis,
        "warnings": _warnings(copy_in, points),
    }


def _warnings(copy_in: CopyInput, points: list[SellingPoint]) -> list[str]:
    """字段缺失兜底提示：告诉用户补什么能让文案更好。"""
    warns: list[str] = []
    if not copy_in.extras.get("brand") and not copy_in.recognized_label:
        warns.append("未获得识别结果且未填写品牌，标题较为笼统，建议补充品牌型号")
    if not copy_in.extras.get("color"):
        warns.append("未填写颜色/规格，标题与描述缺少关键区分信息")
    answered = len([k for k in copy_in.answers if copy_in.answers.get(k)])
    if answered == 0:
        warns.append("未完成成色问卷，卖点描述仅基于类目特征生成")
    elif answered < 4:
        warns.append(f"成色问卷仅完成 {answered}/4 题，部分卖点缺失")
    if not copy_in.price:
        warns.append("尚未确定售价，描述中不含价格信息")
    if not any(p.source == "识别结果" for p in points):
        warns.append("文案未引用识别结果，若识别不准请手动选择类目")
    return warns


# ------------------------------------------------------------------ LLM 增强（可选）

LLM_BASE_URL = os.environ.get("COPYWRITING_LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("COPYWRITING_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("COPYWRITING_LLM_MODEL", "")


def llm_enabled() -> bool:
    return bool(LLM_BASE_URL and LLM_API_KEY and LLM_MODEL)


def _llm_prompt(copy_in: CopyInput, draft: dict) -> str:
    points_text = "\n".join(
        f"- [{i['group']}] {i['text']}（来源：{i['source']}）"
        for g in draft["selling_points"] for i in g["items"]
    )
    extras = "、".join(f"{k}={v}" for k, v in copy_in.extras.items()) or "（无）"
    return (
        "你是二手交易平台的文案编辑。请基于以下**已确认事实**撰写商品文案。\n"
        "严格约束：只能使用下列事实，禁止臆造品牌、配置、无拆修、原装包装、瑕疵位置等未提供的信息。\n\n"
        f"【物品】{_item_name(copy_in)}（类目：{_category_label(copy_in)}）\n"
        f"【成色】{_grade_label(copy_in)}\n"
        f"【用户填写】{extras}\n"
        f"【已确认卖点】\n{points_text}\n\n"
        "请输出 JSON（不要 markdown 代码块），字段：\n"
        '  "titles": 3-4 个标题候选字符串数组（20-30 字，不要书名号堆砌，关键词靠前）\n'
        '  "selling_points": 字符串数组（4-8 条，每条一句话，卖点式表达）\n'
        '  "description": 一段 100-150 字的描述文本\n'
    )


def _call_llm(copy_in: CopyInput, draft: dict) -> dict | None:
    """调用 OpenAI 兼容接口润色文案；任何异常都返回 None（调用方降级）。"""
    if not llm_enabled():
        return None
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不要解释，不要 markdown 代码块。"},
            {"role": "user", "content": _llm_prompt(copy_in, draft)},
        ],
        "temperature": 0.6,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError) as exc:
        import logging

        logging.getLogger("idle-item-lister").warning("copywriting_llm_failed error=%s", exc)
        return None

    titles = [str(t).strip() for t in data.get("titles", []) if str(t).strip()][:4]
    sp_texts = [str(s).strip() for s in data.get("selling_points", []) if str(s).strip()][:8]
    description = str(data.get("description", "")).strip()
    if not titles or not sp_texts:
        return None

    # 保留规则引擎的分组结构：把 LLM 的卖点平铺进一个「综合亮点」组
    groups = list(draft["selling_points"])
    groups.append({
        "group": "综合亮点",
        "items": [{"group": "综合亮点", "text": t, "source": "大模型润色", "detail": ""} for t in sp_texts],
    })
    return {
        "titles": [{"style": f"方案{i+1}", "text": t, "template": "大模型生成", "fields": {}}
                   for i, t in enumerate(titles)],
        "title": titles[0],
        "selling_points": groups,
        "description": description or draft["description"],
        "engine": "llm",
        "basis": draft["basis"],
        "warnings": draft["warnings"],
    }


# ------------------------------------------------------------------ 对外入口


def generate_copy(copy_in: CopyInput) -> dict:
    """生成文案：规则引擎打底，配置了 LLM 时再润色（失败自动降级）。"""
    draft = generate_by_rules(copy_in)
    if llm_enabled():
        enhanced = _call_llm(copy_in, draft)
        if enhanced:
            return enhanced
        draft["warnings"] = draft["warnings"] + ["大模型润色失败，已返回规则引擎文案"]
    return draft
