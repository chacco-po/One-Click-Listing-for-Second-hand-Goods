"""探测 HF 镜像中可用的抠图模型权重（用于离线/受限网络环境下的模型预置）。"""
import json
import urllib.request

REPOS = [
    "davidfant/rembg-u2net",
    "Carve/u2net-universal",
    "jellybox/u2net",
    "lyiiiii/u2net",
    "reidn3r/u2net-image-rembg",
    "AlenZeng/u2netonnxmodel",
    "briaai/RMBG-1.4",
    "onnx-community/BiRefNet_lite",
    "Ultralytics/YOLO11",
]
BASE = "https://hf-mirror.com/api/models/{}?full=true"

for repo in REPOS:
    try:
        with urllib.request.urlopen(BASE.format(repo), timeout=25) as r:
            data = json.load(r)
        files = [s["rfilename"] for s in data.get("siblings", [])]
        picks = [f for f in files if f.endswith((".onnx", ".pt", ".pth"))]
        print(f"{repo}: {picks[:10] if picks else files[:8]}")
    except Exception as exc:
        print(f"{repo}: ERR {exc}")
