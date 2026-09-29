# -*- coding: utf-8 -*-
"""几何处理：基准点检测 + 透视矫正 + 坐标归一化。

【为什么需要】
手机拍的首件照片几乎必然带透视倾斜。若不矫正，图纸侧（正交、归一化坐标）
与照片侧（斜的、像素坐标）根本无法在同一坐标系里比较 —— 这是「匹配」的前置条件。

【P0-2（2026-09-22）基准点策略重构 · 方案 §5】
方案原文：「平面 PVC 盖子优先使用边框、孔位、面板轮廓或其他稳定结构作为基准点。
无法找到足够基准点时降低匹配置信度，并要求人工复核。」

**重构前的单一策略（只找最大凸四边形）实测两个致命问题**：
  1. 150 张真实生产照片 **100% 落 `no-quad`** —— 透视矫正从未真正生效，
     与 MarkVerifier 注释里「no-quad → 仿射配准 0/60 生效」的实证一致；
  2. 少数检出四边形的（如 JQ 面板）检到的是横向条带：960x1280 → warp → 811x433，
     宽高比 4.4 倍畸变，内容被压扁裁光，OCR 从 5 条掉到 0 条。

因此改为 **多候选 + 稳定性打分 + 护栏**：
  候选 = 面板外轮廓 / 边框直线 / 孔位圆 / 最大凸四边形；先打分排序，再过护栏，
  「宽高比畸变」「覆盖不足」一律否决，最后才应用变换。

【护栏与多候选为什么要分开控制】
护栏只杀「明显错的四边形」，且只会把「错误矫正」退回「不矫正」（即老行为），
无新增风险，故**默认启用**。
多候选探测（Hough 直线/圆等）实测有 +0.1~0.4s/张开销（640 工作尺度），
且其候选在实测中尚不可信
（见 choose() 的收紧说明），只作诊断，故**默认不开**，由 `use_multi` 控制 ——
开关需以实测数据为依据翻转，不允许拍脑袋默认改变生产行为。

【失败即降级，不硬猜】
找不到可信基准点时返回 applied=False 与原始尺寸，由上层决定
（可以照常做 OCR，但空间匹配的可信度下降）。绝不强行拟合出一个错的四边形。
"""
from __future__ import annotations

from dataclasses import dataclass, field

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

# ---------------- P0-2：基准点护栏与打分参数 ----------------

# warp 后/前的宽高比超出 [1/MAX, MAX] 即判定为「畸变裁错」，拒绝矫正。
# 【实测依据】JQ 面板 960x1280 被 warp 成 811x433 → 比值 (811/433)/(960/1280) = 2.50，
# 内容整体被压扁，OCR 由 5 条降为 0 条。取 1.4（±40%）留足安全边际。
MAX_ASPECT_DISTORTION = 1.4
# 四边形覆盖原图面积低于此值即拒绝 —— 绝大多数内容落在框外，等于把产品裁掉了。
MIN_QUAD_COVER = 0.30
# 候选进入「可用」所需的最低稳定性分。
CANDIDATE_SCORE_MIN = 0.45
# 多候选探测的统一工作尺度：Hough 变换对大图代价高，统一缩到该尺度再算，
# 得到的坐标乘回 scale 映射回原图。
CAND_SCALE_SIDE = 640


@dataclass
class Candidate:
    """一类基准点候选及其稳定性评分。"""

    kind: str                      # contour-quad / panel-rect / frame-lines / hole-circles
    score: float = 0.0             # 0~1，越高越可信
    usable: bool = False
    corners: list | None = None    # 四边形类候选才有（原图坐标）
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "score": round(self.score, 4),
            "usable": self.usable,
            "corners": self.corners,
            "meta": self.meta,
        }


@dataclass
class Geometry:
    applied: bool = False
    method: str = "none"
    corners: list | None = None      # 原图上的四角 [[x,y] x4]（tl,tr,br,bl）
    width: int = 0
    height: int = 0
    # ---- P0-2 新增 ----
    trusted: bool = False            # 是否拿到可信基准点（方案 §5「找不到足够基准点」）
    chosen: str | None = None        # 最终采用的候选类型
    candidates: list = field(default_factory=list)   # 全部候选（Candidate）的诊断快照
    rejected_corners: list | None = None  # 被护栏否决的四角（纯诊断，绝不复用为可信锚点）
    refusal: str | None = None       # 被护栏否决的原因

    def to_dict(self) -> dict:
        d = {
            "applied": self.applied,
            "method": self.method,
            "corners": self.corners,
            "width": self.width,
            "height": self.height,
        }
        # 新增字段一律追加，不改动既有键名。
        # 【与 C# MarkVerifier 的契约】C# 以 `applied || corners 非空` 判 GeometryTrusted，
        # 因此 corners 只允许在「可信」时非空（applied / flat-quad-skip）；
        # guard-reject 的四角必须放进 rejected_corners，否则会被误判为可信。
        d["trusted"] = self.trusted
        if self.chosen is not None:
            d["chosen"] = self.chosen
        if self.candidates:
            d["candidates"] = [c.to_dict() if isinstance(c, Candidate) else c
                               for c in self.candidates]
        if self.rejected_corners is not None:
            d["rejected_corners"] = self.rejected_corners
        if self.refusal:
            d["refusal"] = self.refusal
        return d


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


# ---------------------------------------------------------------- P0-2 护栏与打分


def _aspect_distortion(quad: np.ndarray, w: int, h: int) -> float:
    """warp 后/前的宽高比。1.0 = 不变形；2.5 = JQ 面板那种压扁。"""
    tl, tr, br, bl = quad
    w_top = float(np.linalg.norm(tr - tl))
    w_bot = float(np.linalg.norm(br - bl))
    h_left = float(np.linalg.norm(bl - tl))
    h_right = float(np.linalg.norm(br - tr))
    W = max(w_top, w_bot)
    H = max(h_left, h_right)
    if W <= 0 or H <= 0 or h <= 0:
        return 1.0
    src_ar = w / h
    dst_ar = W / H
    if src_ar <= 0 or dst_ar <= 0:
        return 1.0
    return dst_ar / src_ar


def quad_cover(quad: np.ndarray, w: int, h: int) -> float:
    """四边形覆盖原图的面积比例。"""
    if w <= 0 or h <= 0:
        return 0.0
    a = cv2.contourArea(np.asarray(quad, dtype=np.float32).reshape(4, 1, 2))
    return float(a) / float(w * h)


def pass_guard(quad: np.ndarray, w: int, h: int) -> tuple[bool, str | None]:
    """基准点护栏 —— 否决「看起来像、实际是错的」四边形。

    返回 (是否通过, 否决原因)。两条判据均有实测出处，见常量注释。

    【为什么护栏必须始终启用】
    它只拦「畸变/覆盖不足」的坏四边形 —— 那本来就是 bug（JQ 面板 OCR 5→0 条）。
    正常照片要么没有四边形候选（no-quad），要么四边合理，护栏对它们零影响。
    """
    distort = _aspect_distortion(quad, w, h)
    if distort > MAX_ASPECT_DISTORTION or distort < 1.0 / MAX_ASPECT_DISTORTION:
        return False, f"aspect-distortion:{distort:.2f}"
    cover = quad_cover(quad, w, h)
    if cover < MIN_QUAD_COVER:
        return False, f"cover-too-small:{cover:.2f}"
    return True, None


def _score_region(quad: np.ndarray, w: int, h: int) -> float:
    """四边形类候选的稳定性分：覆盖 0.4 + 矩形度 0.3 + 形变温和 0.3。

    三项都取 [0,1] 的分段/归一化打分，避免某一项单点决定。
    """
    # 1) 覆盖度：太小=只框到局部，接近 1=没框住内容
    cover = quad_cover(quad, w, h)
    if cover < 0.30:
        cov_s = max(0.0, cover / 0.30) * 0.6
    elif cover > 0.95:
        cov_s = max(0.0, (1.0 - cover) / 0.05) * 0.6
    else:
        cov_s = 1.0

    # 2) 矩形度：四内角偏离 90° 的平均程度 + 对边长度比
    pts = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    ang_dev = []
    for i in range(4):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % 4]
        v1, v2 = a - b, c - b
        cosv = float(np.dot(v1, v2)) / (float(np.linalg.norm(v1) * np.linalg.norm(v2)) + 1e-6)
        ang = float(np.degrees(np.arccos(np.clip(cosv, -1.0, 1.0))))
        ang_dev.append(abs(ang - 90.0))
    rect_s = max(0.0, 1.0 - (sum(ang_dev) / 4.0) / 30.0)     # 平均偏 30° 即归零

    # 3) 形变温和：宽高比越接近 1 越好
    distort = _aspect_distortion(quad, w, h)
    d = abs(np.log(max(distort, 1e-6)))
    shape_s = max(0.0, 1.0 - d / np.log(MAX_ASPECT_DISTORTION))

    return float(np.clip(0.4 * cov_s + 0.3 * rect_s + 0.3 * shape_s, 0.0, 1.0))


def _prep_small(bgr: np.ndarray) -> tuple:
    """缩到统一工作尺度，返回 (小图, 回原图的缩放系数)。"""
    h, w = bgr.shape[:2]
    s = CAND_SCALE_SIDE / float(max(w, h))
    if s >= 1.0:
        return bgr, 1.0
    small = cv2.resize(bgr, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
    return small, 1.0 / s     # 小图坐标 × scale = 原图坐标


def _cand_frame_lines(small: np.ndarray, scale: float) -> Candidate:
    """边框直线候选（方案 §5「边框」）：统计主方向与其支持度。

    只做**诊断**，不自动旋转 —— 旋转会整体改变坐标基准，风险高于收益，
    是否启用要以 Verify 端点的实测对比为准（本意为先把可观测量拿到手）。
    """
    h, w = small.shape[:2]
    m = min(w, h)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=int(m * 0.15),
                            minLineLength=m * 0.15, maxLineGap=m * 0.04)
    if lines is None:
        return Candidate("frame-lines", 0.0, False, meta={"count": 0})
    arr = np.asarray(lines).reshape(-1, 4)
    angs = np.degrees(np.arctan2(arr[:, 3] - arr[:, 1], arr[:, 2] - arr[:, 0])) % 180.0
    angs = np.minimum(angs, 180.0 - angs)          # 折到 0~90
    hist, edges_bin = np.histogram(angs, bins=45, range=(0.0, 90.0))
    k = int(np.argmax(hist))
    peak = hist[k]
    angle = float((edges_bin[k] + edges_bin[k + 1]) / 2.0)
    support = float(peak) / float(len(angs))
    # 支持度越高、角度越偏离已有轴对齐（确有倾斜可校）→ 分越高；
    # 但峰值接近 0°（本就水平）说明没什么可校正的，只算诊断信息
    score = float(np.clip(support * 0.8 + min(angle, 45.0) / 45.0 * 0.2, 0.0, 1.0))
    return Candidate("frame-lines", score, score >= CANDIDATE_SCORE_MIN, meta={
        "count": int(len(angs)),
        "dominant_angle_deg": round(angle, 2),
        "support": round(support, 3),
    })


def _cand_hole_circles(small: np.ndarray, scale: float) -> Candidate:
    """孔位圆候选（方案 §5「孔位」）：按半径一致性评分。"""
    h, w = small.shape[:2]
    m = min(w, h)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    cs = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.2,
                          minDist=max(16, int(m * 0.05)), param1=100, param2=30,
                          minRadius=max(6, int(m * 0.01)), maxRadius=int(m * 0.15))
    if cs is None:
        return Candidate("hole-circles", 0.0, False, meta={"count": 0})
    arr = np.asarray(cs).reshape(-1, 3)
    n = len(arr)
    r = arr[:, 2]
    if n >= 2 and float(np.mean(r)) > 0:
        cv_ = float(np.std(r) / np.mean(r))          # 半径变异系数
        consist = max(0.0, 1.0 - cv_ / 0.6)
    else:
        consist = 0.3
    # 数量给到 4 个即满分（再多也不增加新信息），半径一致占主导
    score = float(np.clip(consist * 0.7 + min(n, 4) / 4.0 * 0.3, 0.0, 1.0))
    return Candidate("hole-circles", score, score >= CANDIDATE_SCORE_MIN, meta={
        "count": int(n),
        "radius_cv": round(float(np.std(r) / np.mean(r)) if n and float(np.mean(r)) else -1.0, 3),
    })


def _cand_panel_rect(small: np.ndarray, scale: float) -> Candidate:
    """面板外轮廓候选（方案 §5「面板轮廓」）：最大轮廓的最小外接矩形。"""
    h, w = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return Candidate("panel-rect", 0.0, False, meta={"found": False})
    c = max(contours, key=cv2.contourArea)
    if cv2.contourArea(c) < h * w * 0.03:
        return Candidate("panel-rect", 0.0, False, meta={"found": False, "too_small": True})
    box = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32) * float(scale)
    quad = _order_quad(box)
    score = _score_region(quad, int(round(w * scale)), int(round(h * scale)))
    return Candidate("panel-rect", score, score >= CANDIDATE_SCORE_MIN,
                     corners=[[float(x), float(y)] for x, y in quad], meta={"found": True})


def detect_candidates(bgr: np.ndarray) -> list[Candidate]:
    """收集全部基准点候选（含评分）。只在 use_multi 时调用 —— Hough 有毫秒级成本。"""
    h, w = bgr.shape[:2]
    small, scale = _prep_small(bgr)

    cands: list[Candidate] = []

    q = detect_quad(bgr)
    if q is not None:
        ok, reason = pass_guard(q, w, h)
        cands.append(Candidate("contour-quad", _score_region(q, w, h) if ok else 0.0,
                               ok and _score_region(q, w, h) >= CANDIDATE_SCORE_MIN,
                               corners=[[float(x), float(y)] for x, y in q],
                               meta={"guard": reason or "pass"}))

    cands.append(_cand_panel_rect(small, scale))
    cands.append(_cand_frame_lines(small, scale))
    cands.append(_cand_hole_circles(small, scale))
    return cands


def choose(cands: list[Candidate]) -> Candidate | None:
    """按稳定性分选优；同分时优先「能直接给出矫正坐标」的四边形类候选。

    【2026-09-22 实测收紧：只有 contour-quad 允许驱动透视矫正】
    在 15 张真实生产照片上可视化判读发现：panel-rect（最大轮廓最小外接矩形）
    框住的是手掌/键盘/桌面等杂乱背景而非面板 —— 其中一例形变比恰好 0.87、
    骗过护栏并执行了 warp，矫正图整体歪斜、面板文字被裁掉（_p16_out/p16_vis_3_*.png）。
    因此 panel-rect / frame-lines / hole-circles 一律**只做诊断**，
    在它们具备可靠的「真面板」校验（如边缘周长支持度、白色区域分割）之前，
    不得参与矫正决策 —— 宁可不矫正（回到 no-quad 老行为），不可矫正错。
    """
    warp_capable = {"contour-quad"}
    usable = [c for c in cands if c.usable and c.corners and c.kind in warp_capable]
    if not usable:
        return None
    # 偏好顺序：可信的是几何证据本身，而不是候选类型的字母顺序
    pref = {"contour-quad": 0}
    usable.sort(key=lambda c: (-c.score, pref.get(c.kind, 9)))
    return usable[0]


# ---------------------------------------------------------------- 顶层入口


def normalize(bgr: np.ndarray, do_warp: bool = True, use_multi: bool = False) -> tuple:
    """返回 (矫正后的图, Geometry)。

    <param name="use_multi">
    是否启用多候选探测。默认 False —— 与重构前行为一致（只认最大凸四边形）。
    True 时额外收集 panel-rect / frame-lines / hole-circles 候选做**诊断**，
    但矫正决策仍只认 contour-quad（见 choose() 的实测收紧说明），
    因此多候选改变的是可观测性，不是矫正行为本身。
    </param>
    """
    h, w = bgr.shape[:2]
    if not do_warp:
        return bgr, Geometry(False, "disabled", None, int(w), int(h), trusted=False, chosen="disabled")

    if use_multi:
        cands = detect_candidates(bgr)
        chosen = choose(cands)
        if chosen is not None:
            q = np.asarray(chosen.corners, dtype=np.float32).reshape(4, 2)
            q = _order_quad(q)
            ok, reason = pass_guard(q, w, h)
            if not ok:
                return bgr, Geometry(False, "guard-reject", None,
                                     int(w), int(h), trusted=False, chosen=chosen.kind,
                                     candidates=cands,
                                     rejected_corners=[[float(x), float(y)] for x, y in q],
                                     refusal=reason)
            if _is_flat(q, w, h):
                return bgr, Geometry(False, "flat-quad-skip", [[float(x), float(y)] for x, y in q],
                                     int(w), int(h), trusted=True, chosen=chosen.kind,
                                     candidates=cands)
            out, W, H = warp(bgr, q)
            return out, Geometry(True, chosen.kind, [[float(x), float(y)] for x, y in q],
                                 int(W), int(H), trusted=True, chosen=chosen.kind,
                                 candidates=cands)

        # 候选皆不可用 → 不可信，交给上层降置信 + 人工复核（方案 §5）
        return bgr, Geometry(False, "no-trusted-anchor", None, int(w), int(h),
                             trusted=False, candidates=cands,
                             refusal="all-candidates-below-min-score")

    # ---- 单一路径（行为与重构前一致，仅多一道护栏）----
    quad = detect_quad(bgr)
    if quad is None:
        return bgr, Geometry(False, "no-quad", None, int(w), int(h), trusted=False)

    ok, reason = pass_guard(quad, w, h)
    corners = [[float(x), float(y)] for x, y in quad]
    if not ok:
        # 【契约】被拒四角不得放回 corners：C# 以 corners 非空判「几何可信」，
        # 而被护栏否决 = 不可信，否则等于把已知坏锚点当真。
        return bgr, Geometry(False, "guard-reject", None, int(w), int(h),
                             trusted=False, rejected_corners=corners, refusal=reason)

    # 平坦图（四边形≈全图）不矫正 —— 见 FLAT_CORNER_TOL 注释
    if _is_flat(quad, w, h):
        return bgr, Geometry(False, "flat-quad-skip", corners, int(w), int(h), trusted=True)

    out, W, H = warp(bgr, quad)
    return out, Geometry(True, "contour-quad", corners, int(W), int(H), trusted=True)


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
