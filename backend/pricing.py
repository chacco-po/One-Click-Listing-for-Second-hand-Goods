"""成色自评引导与参考定价（任务5、任务6）

- 成色自评：问卷式引导（使用时长/外观/功能/配件）→ 评分 → 成色等级
- 参考定价：类目基准价 × 成色系数 → 参考价区间，前端可手动覆盖
- 定价依据为内置同类目参考价表（演示数据），可替换为真实行情数据源
"""
from __future__ import annotations

# ---------------- 类目表（id -> 展示信息；base_price 为同类目新品参考基准价，元） ----------------
CATEGORIES: dict[str, dict] = {
    # 数码 3C
    "phone":          {"label": "手机",          "group": "数码3C",   "base_price": 2500},
    "laptop":         {"label": "笔记本电脑",     "group": "数码3C",   "base_price": 5500},
    "tablet":         {"label": "平板电脑",       "group": "数码3C",   "base_price": 2800},
    "headphone":      {"label": "耳机/音箱",      "group": "数码3C",   "base_price": 900},
    "camera":         {"label": "相机/摄像",      "group": "数码3C",   "base_price": 3500},
    "wearable":       {"label": "智能手表/手环",  "group": "数码3C",   "base_price": 1800},
    "console":        {"label": "游戏机/游戏周边", "group": "数码3C",   "base_price": 2200},
    "monitor":        {"label": "显示器/电脑配件", "group": "数码3C",   "base_price": 1200},
    # 家用电器
    "appliance_small": {"label": "小家电",        "group": "家用电器", "base_price": 500},
    "appliance_large": {"label": "大家电",        "group": "家用电器", "base_price": 2500},
    # 家具家居
    "furniture_chair": {"label": "椅凳",          "group": "家具家居", "base_price": 900},
    "furniture_table": {"label": "桌几",          "group": "家具家居", "base_price": 1500},
    "furniture_sofa":  {"label": "沙发/床",       "group": "家具家居", "base_price": 3000},
    "storage":         {"label": "收纳/家居用品",  "group": "家具家居", "base_price": 250},
    "plant":           {"label": "绿植/花瓶",     "group": "家具家居", "base_price": 150},
    # 服饰鞋包
    "clothing_top":    {"label": "上装",          "group": "服饰鞋包", "base_price": 300},
    "clothing_bottom": {"label": "下装",          "group": "服饰鞋包", "base_price": 300},
    "shoes":           {"label": "鞋靴",          "group": "服饰鞋包", "base_price": 600},
    "bag":             {"label": "箱包",          "group": "服饰鞋包", "base_price": 800},
    "watch_acc":       {"label": "配饰/手表",      "group": "服饰鞋包", "base_price": 700},
    # 运动户外
    "sports_gear":     {"label": "健身器材",      "group": "运动户外", "base_price": 1500},
    "ball_racket":     {"label": "球类/球拍",     "group": "运动户外", "base_price": 400},
    "outdoor":         {"label": "户外装备",      "group": "运动户外", "base_price": 800},
    "bicycle":         {"label": "自行车/电动车",  "group": "运动户外", "base_price": 1500},
    # 图书文娱
    "books":           {"label": "图书",          "group": "图书文娱", "base_price": 45},
    "instrument":      {"label": "乐器",          "group": "图书文娱", "base_price": 2000},
    "toy":             {"label": "玩具/模型",     "group": "图书文娱", "base_price": 150},
    # 母婴玩趣
    "baby":            {"label": "母婴用品",      "group": "母婴玩趣", "base_price": 600},
    "pet":             {"label": "宠物用品",      "group": "母婴玩趣", "base_price": 250},
    # 美容个护
    "beauty":          {"label": "护肤/彩妆",     "group": "美容个护", "base_price": 300},
    "personal_care":   {"label": "个护电器",      "group": "美容个护", "base_price": 400},
    # 餐厨日用
    "kitchen":         {"label": "餐厨用具",      "group": "餐厨日用", "base_price": 200},
    "daily":           {"label": "日用百货",      "group": "餐厨日用", "base_price": 100},
    # 其他
    "vehicle":         {"label": "交通工具",      "group": "其他",     "base_price": 8000},
    "other":           {"label": "其他",          "group": "其他",     "base_price": 300},
}

# ---------------- 成色等级（id, 展示名, 定价系数） ----------------
GRADES: list[dict] = [
    {"id": "brand_new", "label": "全新/未使用", "multiplier": 0.92},
    {"id": "like_new_99", "label": "99新",      "multiplier": 0.82},
    {"id": "like_new_95", "label": "95新",      "multiplier": 0.70},
    {"id": "good_90",     "label": "9成新",     "multiplier": 0.58},
    {"id": "fair_80",     "label": "8成新",     "multiplier": 0.42},
    {"id": "poor_70",     "label": "7成新及以下", "multiplier": 0.25},
]
GRADE_MAP = {g["id"]: g for g in GRADES}

# ---------------- 成色自评问卷（任务5：以问卷形式引导） ----------------
CONDITION_QUESTIONS: list[dict] = [
    {
        "id": "usage",
        "title": "使用时长",
        "hint": "从购买到现在大约多久？",
        "options": [
            {"value": "sealed", "label": "未拆封/未使用", "score": 0},
            {"value": "m3",     "label": "3 个月以内",   "score": 1},
            {"value": "y1",     "label": "3 个月 - 1 年", "score": 2},
            {"value": "y2",     "label": "1 - 2 年",     "score": 3},
            {"value": "y2p",    "label": "2 年以上",     "score": 4},
        ],
    },
    {
        "id": "appearance",
        "title": "外观磨损",
        "hint": "仔细观察表面与边角",
        "options": [
            {"value": "none",   "label": "无瑕疵",       "score": 0},
            {"value": "light",  "label": "轻微划痕/磨损", "score": 1},
            {"value": "visible", "label": "明显磨损/污渍", "score": 2},
            {"value": "broken", "label": "破损/变形",     "score": 3},
        ],
    },
    {
        "id": "function",
        "title": "功能状态",
        "hint": "开机/核心功能是否正常",
        "options": [
            {"value": "ok",     "label": "功能完好",     "score": 0},
            {"value": "minor",  "label": "偶发小问题",   "score": 2},
            {"value": "broken", "label": "功能故障",     "score": 4},
        ],
    },
    {
        "id": "accessories",
        "title": "配件与包装",
        "hint": "原装配件、说明书、包装盒是否齐全",
        "options": [
            {"value": "full",   "label": "全套原装",     "score": 0},
            {"value": "partial", "label": "部分配件",    "score": 1},
            {"value": "none",   "label": "无配件",       "score": 2},
        ],
    },
]

# 总分 -> 成色等级 id
GRADE_THRESHOLDS: list[tuple[int, str]] = [
    (0, "brand_new"),
    (2, "like_new_99"),
    (3, "like_new_95"),
    (6, "good_90"),
    (9, "fair_80"),
    (11, "poor_70"),
]


def compute_grade(answers: dict[str, str]) -> dict:
    """根据问卷答案计算成色等级。缺失题目按中间档计并提示（兜底）。"""
    total = 0
    missing: list[str] = []
    for q in CONDITION_QUESTIONS:
        value = answers.get(q["id"])
        if not value:
            missing.append(q["title"])
            total += 2  # 未作答按中间档，保证定价可出
            continue
        opt = next((o for o in q["options"] if o["value"] == value), None)
        if opt is None:
            missing.append(q["title"])
            total += 2
            continue
        total += opt["score"]

    grade_id = GRADE_MAP["good_90"]["id"]
    for threshold, gid in GRADE_THRESHOLDS:
        if total >= threshold:
            grade_id = gid
    grade = GRADE_MAP[grade_id]
    return {
        "grade_id": grade_id,
        "grade_label": grade["label"],
        "multiplier": grade["multiplier"],
        "score": total,
        "missing": missing,
    }


def _nice_round(value: float) -> int:
    """把价格取整到好看的量级。"""
    if value < 100:
        return max(5, round(value / 5) * 5)
    if value < 1000:
        return round(value / 10) * 10
    if value < 5000:
        return round(value / 50) * 50
    return round(value / 100) * 100


def suggest_price(category_id: str, grade_id: str) -> dict:
    """参考定价：类目基准价 × 成色系数 → 区间。"""
    if category_id not in CATEGORIES:
        raise ValueError(f"未知类目 {category_id}")
    if grade_id not in GRADE_MAP:
        raise ValueError(f"未知成色 {grade_id}")

    category = CATEGORIES[category_id]
    grade = GRADE_MAP[grade_id]
    base = category["base_price"]
    mid = _nice_round(base * grade["multiplier"])
    low = _nice_round(mid * 0.88)
    high = _nice_round(mid * 1.15)
    return {
        "category_id": category_id,
        "category_label": category["label"],
        "grade_id": grade_id,
        "grade_label": grade["label"],
        "base_price": base,
        "multiplier": grade["multiplier"],
        "suggested_low": low,
        "suggested_mid": mid,
        "suggested_high": high,
        "basis": f"同类目新品参考价 ¥{base} × 成色系数 {grade['multiplier']} = ¥{mid}（区间 ±12%）",
    }
