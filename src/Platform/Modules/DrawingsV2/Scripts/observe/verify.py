# -*- coding: utf-8 -*-
"""verify —— 定点验证：在照片的「预期位置」上做局部感知。

【为什么需要这个模式（2026-09-12 实测）】
全图盲检对 QR 小码召回不足（语料 21 张：图纸侧声明 26 个 QR，盲检 15 个，
缺口 11 个；0 假阳性）。而 v2 的比对是「应打标对象位置比对」——图纸侧
mark 自带精确 normBbox，所以匹配层根本不需要依赖盲检：
  按预期位置裁剪 → 局部多尺度解码/OCR → 命中即 matched，未命中即 missing 证据。
P1HEQ2 实测：全图所有方案都失败的小码，按 M2 normBbox 裁剪后
detectAndDecode 直接解出 QHRCG200512BFF0001。

【仍然不回答合格与否】
本模块只产出「这个位置上看到/没看到什么」的中性观测，
matched/missing/not_comparable 由 C# MarkVerifier 判定。

【契约】schema = "observe-verify/1"，坐标约定与 observe/1 相同
（norm_bbox = [x,y,w,h] ∈ [0,1]，原点左上，相对矫正后图像）。
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np

from . import VERSION, geometry as G, marks as M, text as T

SCHEMA = "observe-verify/1"

# 裁剪外扩：把预期区域向四周放大这么多倍（码/字可能比图纸标注框略大或偏移）
MARGIN_RATIO = 1.0
MARGIN_MIN_PX = 24.0
# QR 裁剪图上尝试的放大倍数（实测：裁剪图 1x 即可解出小码；2x/4x 兜底）
QR_SCALES = (1.0, 2.0, 4.0)
# QR 解码输入长边上限（#19 性能）：大裁剪超过此值不再 2x/4x 暴力放大，
# 改用「1x + 缩到该上限」策略（见 _qr_scales_for）。正常/小裁剪保持原 (1,2,4)，召回不变。
QR_DECODE_MAX_SIDE = float(os.environ.get("V2_QR_MAX_SIDE", "720"))


def _qr_scales_for(crop: np.ndarray) -> list:
    """QR 解码尺度策略（#19 性能优化，内容自适应）。
    - 长边 <= 上限：保持原 (1,2,4)（小/正常裁剪放大代价低，召回不变）
    - 长边 > 上限（大裁剪）：1x 试 + 缩到上限试，取代无谓 2x/4x 放大
      （原 4x 对大图 = 数千 px，cv2-qr 极慢，是定点验证耗时大头）
    返回尺度系数列表；调用处 cv2.resize(crop, fx=s) 自动处理放大/缩小，
    解码 pts / s 即映射回 crop 坐标，几何无损。
    """
    h, w = crop.shape[:2]
    long = max(h, w)
    if long <= QR_DECODE_MAX_SIDE:
        return [1.0, 2.0, 4.0]
    s = QR_DECODE_MAX_SIDE / long
    return [1.0, round(s, 4)]
# 文本裁剪图的最小高度（低于此值放大到该高度再 OCR）
TEXT_MIN_SIDE = 96.0
TEXT_UPSCALE = 3.0
# OCR 输入尺寸上限（长边，px）。0 = 不限制（原始行为）。
# 【为什么】crop 经「外扩 1 倍 + 加 25% 白边」后可达 4500px 宽，整张喂给 RapidOCR 检测极慢
# （#15 标定：text 平均 4.09s/region，占端到端 75%）。等比缩到该长边后再 OCR。
# 【安全】白边同比缩放，绝对像素仍充足（见下方实测注释）；坐标按 rscale 映射回原 crop。
# 【阈值选定（2026-09-15 离线标定，42 个真实 Text region × 4 档）】
#   canvas 长边 P50=981 / P90=4608（crop 经 margin 1.0 + 白边 25% 后可达 4608），
#   48% 的 region 会被缩放。耗时（已预热）：0 → 265.1s，1024 → 30.3s，1280 → 31.8s，
#   1600 → 43.3s（剂量效应单调 ⇒ 确由尺寸驱动）。1024 与 1280 仅差 5%，
#   取 1280 保留更多分辨率（小丝印更稳），端到端省约 80%。
# 【判定零变化验证】目标文本「从有到无」0/42；共有文本 bbox 偏移中位 0.47%、最大 2.15%
#   （crop 内相对，换算到照片归一化 ≪ 位置容差）。详见 data/_exp/textsize_calib*.json。
TEXT_MAX_SIDE = int(os.environ.get("V2_TEXT_MAX_SIDE", "1280"))

# 小裁剪白边 padding。
# 【实测教训（2026-09-12）】RapidOCR 对极端长宽比的小裁剪（540x64，文字清晰可见）
# 检测直接失灵，且与灰度/二值/放大均无关；四周加白边（≥64px 或短边 25%）后
# 同一引擎同图立即识别（conf 0.89/0.92）。DB 检测需要上下文，纯条带不行。
TEXT_PAD_MIN = 64
TEXT_PAD_RATIO = 0.25


# ---------------- 空白区快筛（2026-09-15 性能） ----------------
# 【为什么需要】定点框因「照片取景 ≠ 图纸视图」会系统性落在纯空白/纯模糊背景上
# （见 #13：仿射配准 0/60 生效）。这类区域仍跑完整「加白边 + 放大 + OCR」是纯浪费——
# 实测端到端 92% 耗时在定点验证，P95 达 67.8s（#15 基线）。
# 【安全边界】只在「几乎不可能有内容」时跳过：四个指标同时低于阈值才跳。
# 误杀的代价是假未检出（黄），漏判的代价只是没省时间——所以阈值取保守，宁可多跑。
# 【标定结论（2026-09-15，193 个真实 region 实测）—— 快筛前提被证伪，默认关闭，勿随意开启】
# 未检出的区域并非"低纹理空白"：多为黑色哑光外壳表面，人眼看着空白，但像素层面
# 噪声纹理丰富（lap 65~917，甚至高于检出组 P5=66.8）。四个指标（std/lap/edge/ink）
# 在「零误杀」约束下跳过率为 0%——低纹理快筛方案不成立，数据见 data/_exp/blank_calib.json。
# 唯一零误杀可跳的是「整块黑壳」（ink>=0.85 且 lap<=300，Otsu 全前景），但覆盖仅
# 11/135、可省 9s/轮（占 665s 的 1.4%），收益不值得冒"黑壳暗码被跳过"的理论风险。
# 真正的耗时大头是「有内容的 region」：text 平均 4.09s/个（占 75%）、qr 平均 5.52s/个（占 25%），
# 优化方向应是降低单 region OCR 成本（输入尺寸/尺度层级），而非跳过。
BLANK_SKIP_ENABLED = os.environ.get("V2_BLANK_SKIP", "0") == "1"
BLANK_LAP_MAX = float(os.environ.get("V2_BLANK_LAP", "8"))      # 拉普拉斯方差（纹理强度）
BLANK_EDGE_MAX = float(os.environ.get("V2_BLANK_EDGE", "0.004"))  # Canny 边缘密度
BLANK_STD_MAX = float(os.environ.get("V2_BLANK_STD", "6"))      # 灰度标准差
BLANK_INK_MAX = float(os.environ.get("V2_BLANK_INK", "0.02"))   # Otsu 前景占比


def _content_metrics(crop: np.ndarray) -> dict:
    """裁剪区内容度指标。尺度归一化（长边统一到 256），使大图小图可比。"""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    if g.size == 0:
        return {"std": 0.0, "lap": 0.0, "edge": 0.0, "ink": 0.0, "side": 0}
    h, w = g.shape[:2]
    side = max(h, w)
    sc = 256.0 / side if side > 256 else 1.0
    gg = cv2.resize(g, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA) if sc != 1.0 else g
    std = float(gg.std())
    lap = float(cv2.Laplacian(gg, cv2.CV_64F).var())
    edge = float((cv2.Canny(gg, 50, 150) > 0).mean())
    _, bw = cv2.threshold(gg, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = float((bw > 0).mean())
    return {"std": round(std, 2), "lap": round(lap, 2), "edge": round(edge, 5),
            "ink": round(ink, 4), "side": int(max(gg.shape[:2]))}


def _looks_blank(m: dict) -> bool:
    return (m["lap"] <= BLANK_LAP_MAX and m["edge"] <= BLANK_EDGE_MAX
            and m["std"] <= BLANK_STD_MAX and m["ink"] <= BLANK_INK_MAX)


def _crop_margin(bgr: np.ndarray, nb: list) -> tuple:
    """按归一化 bbox 裁剪并外扩。返回 (crop, 全局px坐标 x0,y0)。"""
    H, W = bgr.shape[:2]
    nx, ny, nw, nh = nb
    x0 = nx * W
    y0 = ny * H
    w = nw * W
    h = nh * H
    mx = max(w * MARGIN_RATIO, MARGIN_MIN_PX)
    my = max(h * MARGIN_RATIO, MARGIN_MIN_PX)
    cx0 = int(max(0, x0 - mx))
    cy0 = int(max(0, y0 - my))
    cx1 = int(min(W, x0 + w + mx))
    cy1 = int(min(H, y0 + h + my))
    return bgr[cy0:cy1, cx0:cx1].copy(), (cx0, cy0)


def _verify_qr(crop: np.ndarray) -> dict:
    """多检测器多尺度局部解码。

    【M6 修正：多检测器兜底 + ink_ratio 辅助】
    cv2-qr 对小码/模糊码召回差。增加 pyzbar（zbar 真解码）和 zxing-cpp（ZXing C++ 实现）
    作为补充检测器。三者均失败时，用 ink_ratio 辅助判断「码存在但无法解码」。
    返回中性观测：found/data/detector/scales_tried/ink_ratio。
    """
    det = cv2.QRCodeDetector()
    tried = []
    detectors_tried = []
    scales = _qr_scales_for(crop)

    # --- 1. cv2-qr 多尺度 ---
    for s in scales:
        im = crop if s == 1.0 else cv2.resize(crop, None, fx=s, fy=s,
                                              interpolation=cv2.INTER_CUBIC)
        tried.append(s)
        try:
            data, pts, _ = det.detectAndDecode(im)
        except Exception:
            data, pts = "", None
        if data and pts is not None:
            a = np.asarray(pts).reshape(-1, 2) / s
            return {
                "found": True,
                "data": data,
                "detector": "cv2-qr",
                "scale": s,
                "scales_tried": tried,
                "detectors_tried": ["cv2-qr"],
                "bbox": [float(a[:, 0].min()), float(a[:, 1].min()),
                         float(a[:, 0].max()), float(a[:, 1].max())],
                "ink_ratio": _ink_ratio(crop),
            }
    detectors_tried.append("cv2-qr")

    # --- 2. pyzbar 多尺度（真解码，比 cv2-qr 召回更高） ---
    try:
        from pyzbar.pyzbar import decode as pz_decode
        for s in scales:
            im = crop if s == 1.0 else cv2.resize(crop, None, fx=s, fy=s,
                                                  interpolation=cv2.INTER_CUBIC)
            try:
                results = pz_decode(im)
            except Exception:
                results = []
            if results:
                r = results[0]
                rect = r.rect
                a = np.array([[rect.left, rect.top],
                              [rect.left + rect.width, rect.top + rect.height]],
                             dtype=float) / s
                return {
                    "found": True,
                    "data": r.data.decode("utf-8", "ignore") if r.data else None,
                    "detector": "pyzbar",
                    "scale": s,
                    "scales_tried": tried,
                    "detectors_tried": detectors_tried + ["pyzbar"],
                    "bbox": [float(a[0, 0]), float(a[0, 1]),
                             float(a[1, 0]), float(a[1, 1])],
                    "ink_ratio": _ink_ratio(crop),
                }
        detectors_tried.append("pyzbar")
    except ImportError:
        pass

    # --- 3. zxing-cpp 多尺度（ZXing C++ 实现，第三检测器） ---
    try:
        import zxingcpp
        from zxingcpp import BarcodeFormat
        for s in scales:
            im = crop if s == 1.0 else cv2.resize(crop, None, fx=s, fy=s,
                                                  interpolation=cv2.INTER_CUBIC)
            try:
                results = zxingcpp.read_barcodes(im, formats=BarcodeFormat.QRCode)
            except Exception:
                results = []
            if results:
                r = results[0]
                pos = r.position
                a = np.array([[pos.top_left.x, pos.top_left.y],
                              [pos.bottom_right.x, pos.bottom_right.y]],
                             dtype=float) / s
                return {
                    "found": True,
                    "data": r.text,
                    "detector": "zxing-cpp",
                    "scale": s,
                    "scales_tried": tried,
                    "detectors_tried": detectors_tried + ["zxing-cpp"],
                    "bbox": [float(a[0, 0]), float(a[0, 1]),
                             float(a[1, 0]), float(a[1, 1])],
                    "ink_ratio": _ink_ratio(crop),
                }
        detectors_tried.append("zxing-cpp")
    except ImportError:
        pass

    # --- 4. 所有检测器失败 → ink_ratio 辅助判断 ---
    ink = _ink_ratio(crop)
    return {"found": False, "data": None, "detector": "multi-failed",
            "scale": None, "scales_tried": tried,
            "detectors_tried": detectors_tried, "bbox": None,
            "ink_ratio": ink}


def _ink_ratio(crop: np.ndarray) -> float:
    """裁剪区前景墨迹占比（Otsu）。用于 QR 存在性辅助判断。"""
    if crop.size == 0:
        return 0.0
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return round(float((bw > 0).mean()), 4)


def _verify_text(crop: np.ndarray) -> list:
    """局部 OCR。小裁剪先加白边（见 TEXT_PAD_MIN 注释），坐标映射回全局。"""
    ch, cw = crop.shape[:2]
    pad = max(TEXT_PAD_MIN, int(max(ch, cw) * TEXT_PAD_RATIO))
    canvas = cv2.copyMakeBorder(crop, pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, None, (255, 255, 255))

    # 性能：限制 OCR 输入尺寸（含白边同比缩放），rscale 用于把坐标映射回原 crop
    rscale = 1.0
    if TEXT_MAX_SIDE > 0:
        mside = max(canvas.shape[:2])
        if mside > TEXT_MAX_SIDE:
            rscale = TEXT_MAX_SIDE / float(mside)
            canvas = cv2.resize(canvas, None, fx=rscale, fy=rscale,
                                interpolation=cv2.INTER_AREA)
    s = 1.0
    if min(canvas.shape[:2]) < TEXT_MIN_SIDE:
        s = min(TEXT_UPSCALE, TEXT_MIN_SIDE / max(min(canvas.shape[:2]), 1))
    im = canvas if s == 1.0 else cv2.resize(canvas, None, fx=s, fy=s,
                                            interpolation=cv2.INTER_CUBIC)
    regions = T.detect(im)
    out = []
    for r in regions:
        x0, y0, x1, y1 = r.bbox
        # 坐标映射回原 crop：im --/s--> 缩放后 canvas --/rscale--> 原 canvas --(-pad)--> crop
        # ⚠️ 必须 v/(s*rscale) - pad，不能 (v/rscale - pad)/s：后者把 pad 也除了 s，
        # 在「既缩放又放大」时（宽扁裁剪：长边超阈值、短边不足 96）坐标会整体偏移。
        # rscale=1 时化为 v/s - pad，与原实现逐字等价。
        sc = s * rscale
        out.append({
            "text": r.text,
            "conf": round(r.conf, 4),
            "bbox": [round((v / sc) - pad, 2) for v in (x0, y0, x1, y1)],
            "angle": r.angle,
        })
    return out


def _presence(crop: np.ndarray) -> dict:
    """存在性观测：裁剪区的前景墨迹占比（Otsu）。图标类「只验存在性与位置」的证据。"""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if g.size == 0:
        return {"ink_ratio": 0.0}
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return {"ink_ratio": round(float((bw > 0).mean()), 4)}


def verify_image(bgr: np.ndarray, regions: list, do_warp: bool = True) -> dict:
    """对照片上若干预期位置做定点感知。

    regions 元素：{id, kind: "qr"|"text"|"any", norm_bbox: [x,y,w,h], label?: str}
    """
    t0 = time.perf_counter()
    warped, geo = G.normalize(bgr, do_warp=do_warp)
    H, W = warped.shape[:2]

    out_regions = []
    for reg in regions:
        rid = str(reg.get("id", ""))
        kind = str(reg.get("kind", "any"))
        raw_nb = reg.get("norm_bbox")
        t1 = time.perf_counter()
        if not raw_nb or len(raw_nb) != 4:
            out_regions.append({
                "id": rid, "kind": kind, "label": reg.get("label"),
                "norm_bbox_requested": None, "error": "missing/invalid norm_bbox",
                "ms": 0.0,
            })
            continue
        nb = [float(v) for v in raw_nb]
        crop, (ox, oy) = _crop_margin(warped, nb)

        entry = {
            "id": rid,
            "kind": kind,
            "label": reg.get("label"),
            "norm_bbox_requested": [round(v, 6) for v in nb],
        }
        if crop.size == 0:
            entry["error"] = "empty crop"
            out_regions.append(entry)
            continue

        metrics = _content_metrics(crop)
        entry["content"] = metrics
        if BLANK_SKIP_ENABLED and _looks_blank(metrics):
            # 空白区：直接给出与「OCR 未检出」等价的中性观测，跳过昂贵的多尺度 OCR
            entry["blank_skipped"] = True
            if kind in ("qr", "any"):
                entry["qr"] = {"found": False, "data": None, "detector": "blank-skipped",
                               "scale": None, "scales_tried": [], "detectors_tried": [],
                               "bbox": None, "ink_ratio": metrics["ink"]}
            if kind in ("icon", "any"):
                entry["presence"] = {"ink_ratio": metrics["ink"]}
            if kind in ("text", "any"):
                entry["texts"] = []
            entry["ms"] = round((time.perf_counter() - t1) * 1000, 1)
            out_regions.append(entry)
            continue

        if kind in ("qr", "any"):
            q = _verify_qr(crop)
            if q["bbox"] is not None:
                bx = q["bbox"]
                q["norm_bbox"] = G.norm_bbox(bx[0] + ox, bx[1] + oy,
                                             bx[2] + ox, bx[3] + oy, W, H)
            entry["qr"] = q
        if kind in ("icon", "any"):
            entry["presence"] = _presence(crop)
        if kind in ("text", "any"):
            texts = _verify_text(crop)
            for t in texts:
                x0, y0, x1, y1 = t["bbox"]
                t["norm_bbox"] = G.norm_bbox(x0 + ox, y0 + oy, x1 + ox, y1 + oy, W, H)
            entry["texts"] = texts
        entry["ms"] = round((time.perf_counter() - t1) * 1000, 1)
        out_regions.append(entry)

    return {
        "schema": SCHEMA,
        "generator": {"name": "observe-verify", "version": VERSION,
                      "cv2": cv2.__version__},
        "geometry": geo.to_dict(),
        "regions": out_regions,
        "diagnostics": {
            "verify_ms": round((time.perf_counter() - t0) * 1000, 1),
            "region_count": len(out_regions),
            "blank_skipped": sum(1 for r in out_regions if r.get("blank_skipped")),
            "blank_skip_enabled": BLANK_SKIP_ENABLED,
        },
    }


def verify_file(path: str, regions: list, do_warp: bool = True) -> dict:
    data = np.fromfile(path, dtype=np.uint8)
    bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError(f"cannot read image: {path}")
    return verify_image(bgr, regions, do_warp=do_warp)
