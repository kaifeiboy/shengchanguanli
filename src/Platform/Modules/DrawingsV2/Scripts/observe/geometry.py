# -*- coding: utf-8 -*-
"""几何处理：文档四角检测 + 透视矫正 + 坐标归一化。

【为什么需要】
手机拍的首件照片几乎必然带透视倾斜。若不矫正，图纸侧（正交、归一化坐标）
与照片侧（斜的、像素坐标）根本无法在同一坐标系里比较 —— 这是「匹配」的前置条件。

【失败即降级，不硬猜】
找不到可信四边形时返回 applied=False 与原始尺寸，由上层决定
（可以照常做 OCR，但空间匹配的可信度下降）。绝不强行拟合出一个错的四边形。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# 四边形面积占整图比例的可信区间（太小=噪声轮廓，太大=没框住内容）
AREA_MIN_RATIO = 0.15
AREA_MAX_RATIO = 0.99

# 「平坦图」判定：四角都贴着原图四角（±4%）说明根本没有透视可矫正。
# 【实测教训（2026-09-12，M4 端点验证）】对平面渲染图强行 warp 会把内容推移 ~4.4%，
# 图纸侧 normBbox 与照片侧 norm_bbox 失配，明明压在预期框上的文本被判「位置偏移」。
# 真正带透视的照片，四角必然显著偏离图像角点，不受此判定影响。
FLAT_CORNER_TOL = 0.04
# 轴对齐判定：各边与水平/垂直的夹角上限（度）。手持倾斜一般 ≥5°。
ANGLE_TOL_DEG = 4.0


@dataclass
class Geometry:
    applied: bool = False
    method: str = "none"
    corners: list | None = None      # 原图上的四角 [[x,y] x4]（tl,tr,br,bl）
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "method": self.method,
            "corners": self.corners,
            "width": self.width,
            "height": self.height,
        }


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """把 4 个点排成 tl, tr, br, bl。"""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    d = np.diff(pts, axis=1).ravel()
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def detect_quad(bgr: np.ndarray) -> np.ndarray | None:
    """在图中找最像文档的凸四边形，找不到返回 None。"""
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)

    # 先补几下形态学，把断续的边缘连起来
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, kernel, iterations=2)
    edges = cv2.erode(edges, kernel, iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    img_area = float(w * h)
    best, best_area = None, 0.0
    for c in contours:
        area = cv2.contourArea(c)
        if area < img_area * AREA_MIN_RATIO or area > img_area * AREA_MAX_RATIO:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4:
            continue
        if not cv2.isContourConvex(approx):
            continue
        if area > best_area:
            best, best_area = approx, area

    return _order_quad(best) if best is not None else None


def warp(bgr: np.ndarray, quad: np.ndarray) -> tuple:
    """按四角做透视矫正，返回 (矫正图, 目标宽, 目标高)。"""
    tl, tr, br, bl = quad
    w_top = float(np.linalg.norm(tr - tl))
    w_bot = float(np.linalg.norm(br - bl))
    h_left = float(np.linalg.norm(bl - tl))
    h_right = float(np.linalg.norm(br - tr))
    W = int(round(max(w_top, w_bot)))
    H = int(round(max(h_left, h_right)))
    if W < 32 or H < 32:
        return bgr, bgr.shape[1], bgr.shape[0]

    dst = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(quad, dst)
    out = cv2.warpPerspective(bgr, m, (W, H), flags=cv2.INTER_CUBIC)
    return out, W, H


def _is_flat(quad: np.ndarray, w: int, h: int) -> bool:
    """无透视 → 跳过矫正。两种情形（见 FLAT_CORNER_TOL 注释）：
      1. 四角都贴着原图角点（±FLAT_CORNER_TOL）；
      2. 四边形轴对齐（各边与水平/垂直夹角 ≤ ANGLE_TOL_DEG）——
         【实测（2026-09-12）】平面图纸会检出「图纸边框」矩形（页边距 2~7%），
         对它 warp 只是裁掉页边距，却让页面相对坐标整体错位 ~4%，文本命中被判偏位。
         真正手持倾斜的照片边框必然带角度（≥5°），不受影响。
    """
    tx, ty = w * FLAT_CORNER_TOL, h * FLAT_CORNER_TOL
    tl, tr, br, bl = quad
    near_corners = (
        abs(tl[0]) <= tx and abs(tl[1]) <= ty
        and abs(w - tr[0]) <= tx and abs(tr[1]) <= ty
        and abs(w - br[0]) <= tx and abs(h - br[1]) <= ty
        and abs(bl[0]) <= tx and abs(h - bl[1]) <= ty
    )
    if near_corners:
        return True

    def deg_from_h(v):
        v = v.astype(float)
        a = abs(np.degrees(np.arctan2(v[1], v[0]))) % 180.0
        return min(a, 180.0 - a)

    def deg_from_v(v):
        return abs(90.0 - deg_from_h(v))

    devs = [
        deg_from_h(tr - tl), deg_from_h(br - bl),   # 上/下边贴水平
        deg_from_v(bl - tl), deg_from_v(br - tr),   # 左/右边贴垂直
    ]
    return max(devs) <= ANGLE_TOL_DEG


def normalize(bgr: np.ndarray, do_warp: bool = True) -> tuple:
    """返回 (矫正后的图, Geometry)。"""
    h, w = bgr.shape[:2]
    if not do_warp:
        return bgr, Geometry(False, "disabled", None, int(w), int(h))

    quad = detect_quad(bgr)
    if quad is None:
        return bgr, Geometry(False, "no-quad", None, int(w), int(h))

    # 平坦图（四边形≈全图）不矫正 —— 见 FLAT_CORNER_TOL 注释
    if _is_flat(quad, w, h):
        corners = [[float(x), float(y)] for x, y in quad]
        return bgr, Geometry(False, "flat-quad-skip", corners, int(w), int(h))

    out, W, H = warp(bgr, quad)
    corners = [[float(x), float(y)] for x, y in quad]
    return out, Geometry(True, "contour-quad", corners, int(W), int(H))


def norm_bbox(x0: float, y0: float, x1: float, y1: float, W: float, H: float) -> list:
    """归一化 [x, y, w, h] ∈ [0,1]，与 vpdf/normalize.py 同一约定。"""
    if W <= 0 or H <= 0:
        return [0.0, 0.0, 0.0, 0.0]

    def clamp(v: float) -> float:
        return 0.0 if v < 0 else (1.0 if v > 1 else v)

    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    return [
        round(clamp(x0 / W), 6),
        round(clamp(y0 / H), 6),
        round(clamp((x1 - x0) / W), 6),
        round(clamp((y1 - y0) / H), 6),
    ]
