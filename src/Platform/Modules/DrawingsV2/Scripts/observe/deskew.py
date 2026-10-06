# -*- coding: utf-8 -*-
"""照片平面内旋转纠偏（复用 V1 diff_visualizer._deskew_image / _cheap_skew_angle 逻辑）。

【缘由】V2 的 `geometry.normalize` 只做**透视**矫正（手机朝相机倾斜）。
但手持拍摄还常有**平面内旋转**（手机本身歪了），不校正会让照片侧 OCR 框
相对图纸侧 normBbox 整体偏一个角度，L2 落点不稳。

【复用方式】移植 V1 的「无模型 PCA 主方向角度预估」+ 旋转，作为
`pipeline.observe_image` 在透视矫正之后、OCR 之前的一步。
与 V1 一致：仅当置信度足够且 |angle|≥0.5° 且 <10° 才旋转（expand=True 防裁切），
平正照片 angle≈0 不旋转 → 零行为变化、零回归风险。

【不重复 OCR】角度用 PCA 在缩略图上估算（~0 成本），旋转后只对旋转图跑一次 OCR。
"""
from __future__ import annotations

import math

import cv2
import numpy as np


def _cheap_skew_angle(bgr, small: int = 256):
    """无模型倾斜预估：强边缘像素的 PCA 主方向≈文本行方向。

    移植自 V1 diff_visualizer._cheap_skew_angle。
    返回 (angle_deg, confidence)；confidence=None 表示边缘过少不可靠；
    angle 已归一到 [-45,45]（相对水平的偏差）。
    """
    try:
        h, w = bgr.shape[:2]
        s = small / float(max(w, h)) if max(w, h) > 0 else 1.0
        sm = cv2.resize(bgr, (max(1, int(w * s)), max(1, int(h * s))),
                        interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY) if sm.ndim == 3 else sm
        gx = np.abs(np.gradient(g, axis=1))
        gy = np.abs(np.gradient(g, axis=0))
        mag = np.sqrt(gx ** 2 + gy ** 2)
        th = mag.mean() + mag.std()
        ys, xs = np.where(mag > th)
        n = int(xs.size)
        if n < 30:
            return 0.0, None
        wts = mag[ys, xs]
        sw = float(wts.sum())
        if sw <= 0:
            return 0.0, None
        cx = float((xs * wts).sum() / sw)
        cy = float((ys * wts).sum() / sw)
        dx = xs - cx
        dy = ys - cy
        cxx = float((dx * dx * wts).sum() / sw)
        cyy = float((dy * dy * wts).sum() / sw)
        cxy = float((dx * dy * wts).sum() / sw)
        ang = 0.5 * float(np.degrees(np.arctan2(2.0 * cxy, cxx - cyy)))
        while ang > 45.0:
            ang -= 90.0
        while ang < -45.0:
            ang += 90.0
        conf = min(1.0, float(wts.sum() / mag.sum()) * 4.0)
        return float(ang), conf
    except Exception:
        return 0.0, None


def _rotate_cv(bgr, angle_deg: float):
    """绕中心旋转（cv2，expand=True 等价），白底填充。与 PIL rotate 同号（正=CCW）。"""
    h, w = bgr.shape[:2]
    center = (w / 2.0, h / 2.0)
    rad = math.radians(angle_deg)
    cos = abs(math.cos(rad))
    sin = abs(math.sin(rad))
    new_w = int(round(h * sin + w * cos))
    new_h = int(round(h * cos + w * sin))
    m = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    m[0, 2] += (new_w - w) / 2.0
    m[1, 2] += (new_h - h) / 2.0
    return cv2.warpAffine(bgr, m, (new_w, new_h),
                          flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT,
                          borderValue=(255, 255, 255))


def deskew(bgr, min_deg: float = 0.5, max_abs_deg: float = 10.0, min_conf: float = 0.5):
    """对照片做平面内旋转纠偏。

    返回 (纠正后的图, 角度_度)。不满足阈值时原样返回、角度 0.0（零行为变化）。
    """
    ang, conf = _cheap_skew_angle(bgr)
    if conf is None or conf < min_conf or abs(ang) < min_deg or abs(ang) > max_abs_deg:
        return bgr, 0.0
    return _rotate_cv(bgr, ang), round(ang, 2)


# ================= EXIF 方向转置（2026-09-30 问题3） =================
# 【背景】cv2 解码不读 EXIF：手机竖拍照片（Orientation=6/8）像素是「躺倒」的，
#   浏览器 <img> 会自动应用 EXIF 显示为正 →「H5 预览对、OCR 坐标系错位」。
#   pipeline._imread_oriented 按 EXIF 转置后回写原文件（JPEG q=95），使 H5 展示、
#   render_marked 历史缩略图、verify 定点裁剪等所有下游与 OCR 坐标系天然一致。
#
# 【⚠ 90° 像素侧歪（无 EXIF）自动判向已实验证伪（2026-09-30，勿再尝试 OCR/PCA 判向）】
#   ① OCR 证据分：RapidOCR 对 0/90/180/270 四方向都能正确读出文本（det 四边形透视
#      校正能力强），分数差 <10%，无法区分方向；
#   ② 文本框角度：RapidOCR 返回的 quad 已按「阅读系」重排，各方向角度均 ≈0°，无信号；
#   ③ PCA 主方向（_cheap_skew_angle）：被面板边框/背景结构污染，正向照与侧歪照同为
#      -33.3°，无法区分。
#   结论：无可靠判向信号时硬做 90° 自动扶正必然误转（比不转更糟），已按
#   「宁可不转、不可转错」原则放弃；如现场确有侧歪照片需求，走 H5 手动旋转。

_EXIF_ROT_CV = {
    3: cv2.ROTATE_180,
    6: cv2.ROTATE_90_CLOCKWISE,
    8: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def exif_orientation(data: bytes) -> int:
    """读 JPEG EXIF Orientation（tag 274）。无 EXIF/解析失败/非纯旋转方向返回 1。

    仅处理最常见的纯旋转 3/6/8；2/4/5/7（含镜像翻转）极罕见，保持原样不处理。
    """
    try:
        import io
        from PIL import Image
        ori = int(Image.open(io.BytesIO(data)).getexif().get(274) or 1)
    except Exception:
        return 1
    return ori if ori in _EXIF_ROT_CV else 1
