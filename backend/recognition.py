"""物品识别与类目推断（任务3）

YOLO11（Ultralytics）检测 → 取面积最大且非人物的主体 → COCO 类目映射到二手平台类目。
模型/网络不可用时自动降级：返回 available=False，前端引导用户手动选择类目（兜底）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

# COCO 80 类中文名（识别结果展示用）
COCO_ZH: dict[str, str] = {
    "person": "人", "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "airplane": "飞机", "bus": "公交车", "train": "火车", "truck": "卡车",
    "boat": "船", "traffic light": "交通灯", "fire hydrant": "消防栓",
    "stop sign": "停车标志", "parking meter": "停车计时器", "bench": "长椅",
    "bird": "鸟", "cat": "猫", "dog": "狗", "horse": "马", "sheep": "羊",
    "cow": "牛", "elephant": "大象", "bear": "熊", "zebra": "斑马",
    "giraffe": "长颈鹿", "backpack": "背包", "umbrella": "雨伞",
    "handbag": "手提包", "tie": "领带", "suitcase": "行李箱",
    "frisbee": "飞盘", "skis": "滑雪板", "snowboard": "单板滑雪",
    "sports ball": "球类", "kite": "风筝", "baseball bat": "棒球棒",
    "baseball glove": "棒球手套", "skateboard": "滑板", "surfboard": "冲浪板",
    "tennis racket": "网球拍", "bottle": "瓶子", "wine glass": "酒杯",
    "cup": "杯子", "fork": "叉子", "knife": "刀", "spoon": "勺子",
    "bowl": "碗", "banana": "香蕉", "apple": "苹果", "sandwich": "三明治",
    "orange": "橙子", "broccoli": "西兰花", "carrot": "胡萝卜",
    "hot dog": "热狗", "pizza": "披萨", "donut": "甜甜圈", "cake": "蛋糕",
    "chair": "椅子", "couch": "沙发", "potted plant": "盆栽",
    "bed": "床", "dining table": "餐桌", "toilet": "马桶", "tv": "电视",
    "laptop": "笔记本电脑", "mouse": "鼠标", "remote": "遥控器",
    "keyboard": "键盘", "cell phone": "手机", "microwave": "微波炉",
    "oven": "烤箱", "toaster": "烤面包机", "sink": "水槽",
    "refrigerator": "冰箱", "book": "书", "clock": "时钟", "vase": "花瓶",
    "scissors": "剪刀", "teddy bear": "毛绒玩具", "hair drier": "吹风机",
    "toothbrush": "牙刷",
}

# COCO 类目 -> 二手平台类目 id（见 pricing.CATEGORIES）
COCO_TO_CATEGORY: dict[str, str] = {
    "cell phone": "phone", "laptop": "laptop",
    "tv": "monitor", "remote": "monitor", "keyboard": "monitor", "mouse": "monitor",
    "microwave": "appliance_small", "oven": "appliance_small",
    "toaster": "appliance_small", "sink": "appliance_small",
    "refrigerator": "appliance_large",
    "book": "books", "clock": "daily", "vase": "plant",
    "scissors": "daily", "teddy bear": "toy",
    "hair drier": "personal_care", "toothbrush": "daily",
    "chair": "furniture_chair", "bench": "furniture_chair",
    "couch": "furniture_sofa", "bed": "furniture_sofa",
    "dining table": "furniture_table", "potted plant": "plant",
    "handbag": "bag", "backpack": "bag", "suitcase": "bag", "tie": "watch_acc",
    "umbrella": "daily",
    "bottle": "kitchen", "cup": "kitchen", "wine glass": "kitchen",
    "fork": "kitchen", "knife": "kitchen", "spoon": "kitchen", "bowl": "kitchen",
    "banana": "kitchen", "apple": "kitchen", "sandwich": "kitchen",
    "orange": "kitchen", "broccoli": "kitchen", "carrot": "kitchen",
    "hot dog": "kitchen", "pizza": "kitchen", "donut": "kitchen", "cake": "kitchen",
    "bicycle": "bicycle", "motorcycle": "bicycle",
    "car": "vehicle", "truck": "vehicle", "bus": "vehicle",
    "train": "vehicle", "boat": "vehicle", "airplane": "vehicle",
    "sports ball": "ball_racket", "baseball bat": "ball_racket",
    "baseball glove": "ball_racket", "tennis racket": "ball_racket",
    "skateboard": "sports_gear", "surfboard": "sports_gear",
    "skis": "sports_gear", "snowboard": "sports_gear",
    "frisbee": "sports_gear", "kite": "sports_gear",
    "cat": "pet", "dog": "pet", "bird": "pet",
}


@dataclass
class Detection:
    label: str          # COCO 英文类名
    label_zh: str       # 中文名
    confidence: float
    bbox: list[float]   # [x1, y1, x2, y2]


@dataclass
class RecognitionResult:
    available: bool                        # 识别服务是否可用
    primary: Detection | None = None       # 主物品（面积最大且非人物）
    detections: list[Detection] = field(default_factory=list)
    category_id: str | None = None         # 推断的平台类目
    reason: str | None = None              # 不可用/未识别原因（兜底说明）


class ItemRecognizer:
    """YOLO11 物品识别器（优先使用本地预置权重，缺失时自动下载 yolo11n.pt）。"""

    def __init__(self, weights: str | None = None, conf: float = 0.25):
        from ultralytics import YOLO

        from backend.config import MODELS_DIR

        local = MODELS_DIR / "yolo11n.pt"
        self.weights = str(weights or (local if local.exists() else "yolo11n.pt"))
        self._conf = conf
        self._model = YOLO(self.weights)

    def recognize(self, image: Image.Image) -> RecognitionResult:
        results = self._model.predict(image.convert("RGB"), verbose=False, conf=self._conf)
        if not results:
            return RecognitionResult(available=True, reason="未检测到任何物体")

        detections: list[Detection] = []
        for box in results[0].boxes:
            label = self._model.names[int(box.cls)]
            detections.append(Detection(
                label=label,
                label_zh=COCO_ZH.get(label, label),
                confidence=round(float(box.conf), 3),
                bbox=[round(float(v), 1) for v in box.xyxy[0].tolist()],
            ))

        # 多主体干扰兜底：优先选面积最大的非人物目标作为待售主体
        candidates = [d for d in detections if d.label != "person"] or detections
        if not candidates:
            return RecognitionResult(available=True, reason="未检测到任何物体")
        primary = max(candidates, key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]))

        category_id = COCO_TO_CATEGORY.get(primary.label)
        reason = None
        if primary.label == "person":
            reason = "画面中主要检测到人物，请框选物品区域或手动选择类目"
        elif category_id is None:
            reason = f"「{primary.label_zh}」暂无对应平台类目，请手动选择"
        return RecognitionResult(
            available=True,
            primary=primary,
            detections=detections,
            category_id=category_id,
            reason=reason,
        )


_recognizer: ItemRecognizer | None = None
_recognizer_error: str | None = None


def get_recognizer() -> tuple[ItemRecognizer | None, str | None]:
    """获取全局识别器（惰性初始化）。返回 (recognizer, error)。"""
    global _recognizer, _recognizer_error
    if _recognizer is not None or _recognizer_error is not None:
        return _recognizer, _recognizer_error
    try:
        _recognizer = ItemRecognizer()
    except Exception as exc:  # noqa: BLE001 - 兜底：模型不可用时降级为手动选择
        _recognizer = None
        _recognizer_error = f"识别模型不可用: {exc}"
    return _recognizer, _recognizer_error
