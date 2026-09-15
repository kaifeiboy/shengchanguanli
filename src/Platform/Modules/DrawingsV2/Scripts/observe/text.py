# -*- coding: utf-8 -*-
"""文本感知：OCR 出「照片上有哪些字、在哪、多可信」。

【用的是第三方独立库，不是旧方案代码】
rapidocr_onnxruntime 是独立发布的 OCR 库（ONNX 推理，无外网依赖）。
这里只把它当作「文本检测器」使用，本模块自己负责：
  - 行内合并（OCR 常把 "MAC:A42985377FA0" 切成若干片）
  - 角度计算
  - 归一化坐标
  - 统一成 observe/1 契约
旧方案的 OCR 封装（pdf_ocr.py / OcrService）**一概不复用**（已封存）。

【不做的事】
不判「这个字对不对」，不判「该有的字有没有」。只报「看到什么」。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from . import geometry as G

MIN_CONF = 0.50          # 低于此置信度的 OCR 结果不采信
MERGE_GAP_RATIO = 0.012  # 同行合并的最大间隙（占图宽比例）
MERGE_DY_RATIO = 0.010   # 判定「同一行」的最大纵向偏移（占图高比例）

_engine = None


def _get_engine():
    """惰性构造 OCR 引擎（首次约 1~2s，之后复用）。"""
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    return _engine


@dataclass
class TextRegion:
    id: str
    text: str
    conf: float
    bbox: list          # [x0, y0, x1, y1] 像素
    norm_bbox: list     # [x, y, w, h]
    angle: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "conf": round(self.conf, 4),
            "bbox": [round(v, 3) for v in self.bbox],
            "norm_bbox": self.norm_bbox,
            "angle": round(self.angle, 2),
        }


def _angle_of(box: np.ndarray) -> float:
    """从上边缘向量算角度（度，0~360），与 vpdf 的 angle_from_dir 同约定。"""
    pts = np.asarray(box, dtype=float).reshape(4, 2)
    tl, tr = pts[0], pts[1]
    dx, dy = float(tr[0] - tl[0]), float(tr[1] - tl[1])
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return 0.0
    a = float(np.degrees(np.arctan2(dy, dx)))
    return round(a % 360.0, 2)


def _raw_regions(bgr: np.ndarray) -> list:
    eng = _get_engine()
    result, _ = eng(bgr)
    if not result:
        return []
    out = []
    for item in result:
        try:
            box, txt, score = item[0], item[1], float(item[2])
        except Exception:
            continue
        if not txt or score < MIN_CONF:
            continue
        pts = np.asarray(box, dtype=float).reshape(4, 2)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        out.append(
            {
                "text": str(txt).strip(),
                "conf": score,
                "bbox": [float(x0), float(y0), float(x1), float(y1)],
                "angle": _angle_of(pts),
            }
        )
    return out


def _merge_same_line(regions: list, W: int, H: int) -> list:
    """把同一行、水平间隙很小的片段合并成一个文本区。

    OCR 对打标内容（长串编码、中英混排）极易切碎。如果不合并，
    图纸侧的「服务热线 4008601111」永远匹配不到照片侧的 5 个碎片。
    """
    if not regions:
        return []

    gap_max = W * MERGE_GAP_RATIO
    dy_max = H * MERGE_DY_RATIO

    regions = sorted(regions, key=lambda r: (round(r["bbox"][1] / max(dy_max, 1)), r["bbox"][0]))
    merged: list = []
    for r in regions:
        placed = False
        for m in merged:
            same_line = abs(r["bbox"][1] - m["bbox"][1]) <= dy_max and abs(
                r["bbox"][3] - m["bbox"][3]
            ) <= dy_max
            adjacent = r["bbox"][0] - m["bbox"][2] <= gap_max
            if same_line and adjacent:
                m["text"] = m["text"] + r["text"] if _needs_no_space(m["text"], r["text"]) else m["text"] + " " + r["text"]
                m["bbox"] = [
                    min(m["bbox"][0], r["bbox"][0]),
                    min(m["bbox"][1], r["bbox"][1]),
                    max(m["bbox"][2], r["bbox"][2]),
                    max(m["bbox"][3], r["bbox"][3]),
                ]
                m["conf"] = min(m["conf"], r["conf"])
                placed = True
                break
        if not placed:
            merged.append(dict(r))
    return merged


def _needs_no_space(left: str, right: str) -> bool:
    """中英混排时是否不应加空格：任一侧是 CJK，或任一侧以/开头是标点数字。"""
    def is_cjk(s: str) -> bool:
        return any("一" <= ch <= "鿿" for ch in s)

    if not left or not right:
        return True
    if is_cjk(left[-1]) or is_cjk(right[0]):
        return True
    if left[-1] in ":：/-" or right[0] in ":：/-":
        return True
    return False


def detect(bgr: np.ndarray) -> list:
    """OCR 一张图，返回 TextRegion 列表（已做行内合并）。"""
    H, W = bgr.shape[:2]
    raw = _raw_regions(bgr)
    for r in raw:
        # 轻度外扩：OCR 框常比实际字略紧，匹配时容易差之毫厘
        pad = max(1.0, (r["bbox"][3] - r["bbox"][1]) * 0.06)
        r["bbox"] = [
            r["bbox"][0] - pad,
            r["bbox"][1] - pad,
            r["bbox"][2] + pad,
            r["bbox"][3] + pad,
        ]
    merged = _merge_same_line(raw, W, H)

    out = []
    for i, r in enumerate(merged):
        x0, y0, x1, y1 = r["bbox"]
        out.append(
            TextRegion(
                id=f"o{i:04d}",
                text=r["text"],
                conf=r["conf"],
                bbox=[x0, y0, x1, y1],
                norm_bbox=G.norm_bbox(x0, y0, x1, y1, W, H),
                angle=r["angle"],
            )
        )
    return out
