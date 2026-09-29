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

# ---- OCR 证据豁免（文档 §5 / §6） ----
# 【为什么要有】清晰度（拉普拉斯方差）是**统计代理指标**，不是"能否比对"本身。
# 实测 42 例中有 8 例被判"失焦"（sharp 16~98 < 100）而整张照片拒绝比对，
# 但这些照片短边 1080~3072、动态范围 219~239，OCR 全部稳定识别出内容，
# p63 三例甚至识别出了该部位全部 4 个打标对象（LOW VOLTAGE/禁止强电/地暖阀/DC15/24V）。
# 文档 §5：质量不满足才返回"请重拍"，**不直接判缺标**；
# 文档 §6：照片侧**必须**识别文字并保存置信度 —— OCR 是照片侧的正式环节。
# 因此：OCR 取到有效证据 = 这张照片可以比对，清晰度类门槛只在 OCR 取不到证据时才拒绝。
OCR_CONF_MIN = 0.5
# 可被 OCR 证据豁免的理由前缀（清晰度/对比类统计门槛）
OVERRIDABLE_REASONS = ("失焦", "灰阶动态范围不足")


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
    ocr_override: bool = False   # OCR 证据是否已豁免清晰度类拒绝（文档 §5/§6）
    ocr_texts: int = 0           # OCR 取到的有效文本条数
    ocr_codes: int = 0           # 检测到的码数量

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
            "ocr_override": self.ocr_override,
            "ocr_texts": self.ocr_texts,
            "ocr_codes": self.ocr_codes,
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


def apply_ocr_evidence(q: Quality, texts, codes) -> Quality:
    """以 OCR 识别结果作为「这张照片能否比对」的前置判断依据（文档 §5 / §6）。

    规则：
      - OCR 取到有效证据（≥1 条文本且最高置信度 ≥ OCR_CONF_MIN，或 ≥1 个二维码）
        → 清晰度/动态范围这类**统计代理指标**由"拒绝"降级为"提示"，usable 置真；
      - OCR 取不到证据 → 维持原判定（该请重拍就请重拍）；
      - 分辨率过低、过曝、过暗属于"主体过小/内容消失"，不因 OCR 侥幸命中而豁免。

    这样做不放松标准，只是把判定依据从"图像统计量"换成"实际能不能读出内容"，
    符合 §5「质量不满足才请重拍，不直接判缺标」与 §6「照片侧必须识别文字」。
    """
    n = 0
    best = 0.0
    for t in (texts or []):
        try:
            s = (t.get("text") or "").strip()
        except Exception:
            s = ""
        if not s:
            continue
        n += 1
        try:
            best = max(best, float(t.get("conf") or 0.0))
        except Exception:
            pass

    code_n = 0
    try:
        code_n = len(codes or [])
    except Exception:
        code_n = 0

    q.ocr_texts = n
    q.ocr_codes = code_n

    if not ((n > 0 and best >= OCR_CONF_MIN) or code_n > 0):
        return q

    kept, relaxed = [], []
    for r in q.reasons:
        if any(str(r).startswith(p) for p in OVERRIDABLE_REASONS):
            relaxed.append(r)
        else:
            kept.append(r)

    if relaxed:
        q.reasons = kept + [
            "清晰度偏低但 OCR 已识别内容，按可比对处理（原判定：" + "；".join(relaxed) + "）"
        ]
        q.usable = len(kept) == 0
        q.ocr_override = True
    return q
