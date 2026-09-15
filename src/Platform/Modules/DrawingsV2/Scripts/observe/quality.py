# -*- coding: utf-8 -*-
"""照片质量评估 —— 只描述「这张照片能不能用」，不描述「产品合不合格」。

【为什么必须做】
旧方案的失败模式之一：把模糊/过曝的照片直接送进匹配，产出大量假红框，
而用户看到的是「判定结果」而不是「照片不行」，无法区分「漏打」与「没拍清」。
v2 把质量做成一等公民：质量不达标时，判定结果标记为 not_comparable，
明确告诉用户「这张照片无法比对，请重拍」，而不是谎报缺标。

【阈值来源】
均为通用图像处理的经验区间，不针对任何具体图纸调参 —— v2 的原则是
「规则来自语料与标准，不来自对某个样本的过拟合」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

# 拉普拉斯方差：低于此值认为失焦。100 是文档拍摄的常用下限。
SHARPNESS_MIN = 100.0
# 短边像素：低于此值文字无法可靠识别
MIN_SIDE = 640

# ---- 过曝 / 过暗 / 低对比：必须「比例 + 内容」双条件 ----
# 【实测教训 1】首版只用平均亮度判定，结果白底工程图（实测平均亮度 249）被判「过曝」，
# 而它其实是完全可用的图纸渲染图。白底图纸本来就有 94.6% 的像素 > 250。
# 真正的过曝是「整片死白、什么内容都没有」—— 因此必须与内容联合判定。
# 【实测教训 2】二版用全图灰度标准差（std<20 判低对比）作硬门槛，结果细线结构图
# （实测 std 15~17.5，细线反走样天然压低 std，语料全量分布 15.0~38.0）被误判不可用。
# 最终改用「分位数动态范围 P99-P1」：只要图里存在可分辨的墨迹/内容，
# 1% 与 99% 分位必然拉开；全白/全黑/死灰图该值趋近 0。语料实测范围 60~252。
OVEREXPOSED_RATIO = 0.99   # >250 的像素占比
UNDEREXPOSED_RATIO = 0.95  # <40 的像素占比
DYNAMIC_RANGE_MIN = 30.0   # P99 - P1 低于此值认为几乎没有可分辨内容


@dataclass
class Quality:
    sharpness: float = 0.0
    brightness: float = 0.0
    contrast: float = 0.0            # 全图灰度 std（仅上报，不作门槛 —— 见头部实测教训 2）
    dynamic_range: float = 0.0       # P99 - P1 分位数动态范围（低对比判定依据）
    highlight_ratio: float = 0.0   # >250 的像素占比
    shadow_ratio: float = 0.0      # <40 的像素占比
    width: int = 0
    height: int = 0
    usable: bool = True
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sharpness": round(self.sharpness, 2),
            "brightness": round(self.brightness, 2),
            "contrast": round(self.contrast, 2),
            "dynamic_range": round(self.dynamic_range, 1),
            "highlight_ratio": round(self.highlight_ratio, 4),
            "shadow_ratio": round(self.shadow_ratio, 4),
            "width": self.width,
            "height": self.height,
            "usable": self.usable,
            "reasons": list(self.reasons),
        }


def assess(bgr: np.ndarray) -> Quality:
    """评估一张 BGR 图像的质量。

    判据设计（全部经实测校准，见本模块头部注释）：
      - 分辨率：短边下限
      - 清晰度：拉普拉斯方差
      - 过曝/过暗：高光/阴影占比 极高（内容消失型）
      - 低对比：P99-P1 动态范围不足（几乎无可分辨内容）；std 只上报不判
    """
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(gray.mean())
    contrast = float(gray.std())
    p1, p99 = np.percentile(gray, [1, 99])
    dr = float(p99 - p1)
    hi = float((gray > 250).mean())
    lo = float((gray < 40).mean())

    q = Quality(
        sharpness=sharp,
        brightness=bright,
        contrast=contrast,
        dynamic_range=dr,
        highlight_ratio=hi,
        shadow_ratio=lo,
        width=int(w),
        height=int(h),
    )

    if min(w, h) < MIN_SIDE:
        q.reasons.append(f"分辨率过低（短边 {min(w, h)} < {MIN_SIDE}）")
    if sharp < SHARPNESS_MIN:
        q.reasons.append(f"失焦（清晰度 {sharp:.0f} < {SHARPNESS_MIN:.0f}）")

    if hi > OVEREXPOSED_RATIO:
        q.reasons.append(f"过曝（高光占比 {hi * 100:.1f}%，内容几乎消失）")
    if lo > UNDEREXPOSED_RATIO:
        q.reasons.append(f"过暗（阴影占比 {lo * 100:.1f}%，内容几乎消失）")
    if dr < DYNAMIC_RANGE_MIN:
        q.reasons.append(f"灰阶动态范围不足（P99-P1={dr:.0f} < {DYNAMIC_RANGE_MIN:.0f}，几乎无可分辨内容）")

    q.usable = len(q.reasons) == 0
    return q
