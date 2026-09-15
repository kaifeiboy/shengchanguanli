# -*- coding: utf-8 -*-
"""observe —— 照片侧感知层（v2 perception）。

【职责边界（与 vpdf 对称）】
  vpdf    : 图纸侧感知，输出「图纸上要求打什么」
  observe : 照片侧感知，输出「照片上实际看到什么」

【铁律：本包不回答「合格与否」】
  这里只产出**中性观测**：文本区（含 OCR 文本与置信度）、码（二维码/条码）、几何、质量。
  是否合格由 C# 决策层的 MarkVerifier 判定。Python 永远不写 matched/missing 这类结论。

【契约】schema = "observe/1"，与 vpdf/1 共用同一套归一化约定：
  norm_bbox = [x, y, w, h] ∈ [0,1]，原点左上，相对矫正后的图像。
"""
from __future__ import annotations

SCHEMA = "observe/1"
VERSION = "1.0.0"

from . import quality, geometry, text, marks  # noqa: E402,F401
from .pipeline import observe_image, observe_file  # noqa: E402
from .verify import verify_image, verify_file  # noqa: E402

__all__ = [
    "SCHEMA",
    "VERSION",
    "quality",
    "geometry",
    "text",
    "marks",
    "observe_image",
    "observe_file",
    "verify_image",
    "verify_file",
]
