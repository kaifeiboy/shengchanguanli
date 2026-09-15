# -*- coding: utf-8 -*-
"""observe 流水线：把一张照片变成 observe/1 中性观测。

顺序很重要：
  1) 质量评估（在**原图**上做 —— 矫正会改变清晰度统计量）
  2) 几何矫正
  3) 文本 OCR + 码检测（在矫正图上做，坐标才有可比性）
"""
from __future__ import annotations

import hashlib
import os
import time

import cv2

import numpy as np

from . import SCHEMA, VERSION, geometry as G, marks as M, quality as Q, text as T


def _imread_any(path: str):
    """读图（支持中文/非 ASCII 路径）。

    【实测坑】Windows 下 `cv2.imread` 对非 ASCII 路径会返回 None 并只打一行 WARN，
    随后以「cannot read image」失败 —— 而本项目图纸名几乎全是中文。
    改用 numpy 读字节 + cv2.imdecode 可彻底规避。
    """
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# OCR 输入的最长边上限。
# 【实测权衡】同一张渲染图（2339×1653）：
#   2339 -> 67 区 / 15.5s    1600 -> 61 区 / 15.4s
#   1280 -> 57 区 / 10.3s     960 -> 40 区 /  8.0s
# 1280 相比原尺寸省 1/3 时间、召回只少 4 个区（且都是尺寸标注这类噪声），
# 960 再省一点但掉到 40 区、开始丢内容。故取 1280。
MAX_SIDE = 1280


def _resize_max_side(bgr, max_side: int):
    h, w = bgr.shape[:2]
    if max_side <= 0 or max(w, h) <= max_side:
        return bgr, 1.0
    s = max_side / float(max(w, h))
    return cv2.resize(bgr, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA), s


def observe_image(bgr, source_name: str = "", sha256: str = "", do_warp: bool = True,
                  max_side: int = MAX_SIDE) -> dict:
    """对已加载的 BGR 图像做感知，返回 observe/1 文档。"""
    t0 = time.perf_counter()

    # 质量在**原图**上评：缩放会改变清晰度与噪声的统计量
    quality = Q.assess(bgr)
    warped, geo = G.normalize(bgr, do_warp=do_warp)

    # OCR 在缩放后的图上跑（耗时的主要来源）
    proc, _ = _resize_max_side(warped, max_side)
    texts = T.detect(proc)
    codes, detector, degraded = M.detect(proc)

    ms = round((time.perf_counter() - t0) * 1000, 1)
    h, w = proc.shape[:2]

    return {
        "schema": SCHEMA,
        "generator": {
            "name": "observe",
            "version": VERSION,
            "cv2": cv2.__version__,
        },
        "source": {
            "file_name": os.path.basename(source_name) if source_name else "",
            "sha256": sha256,
            "width": int(w),
            "height": int(h),
        },
        "quality": quality.to_dict(),        "geometry": geo.to_dict(),
        "texts": [t.to_dict() for t in texts],
        "codes": [c.to_dict() for c in codes],
        "diagnostics": {
            "observe_ms": ms,
            "code_detector": detector,
            "code_degraded": degraded,
            "text_regions": len(texts),
            "code_count": len(codes),
        },
    }


def observe_file(path: str, do_warp: bool = True) -> dict:
    bgr = _imread_any(path)
    if bgr is None:
        raise RuntimeError(f"cannot read image: {path}")
    return observe_image(bgr, source_name=path, sha256=_sha256(path), do_warp=do_warp)
