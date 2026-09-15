# -*- coding: utf-8 -*-
"""码感知：二维码 / 条码的**存在性与位置**，不做业务解码判定。

【方案约束】只对二维码验「存在 + 位置 + 能否扫出」，不把扫出的内容当判定依据 ——
图纸侧同样只记录了二维码的位置（R3-qr），两侧可比的是「有没有、在不在那」。

【为什么不必需 pyzbar】
pyzbar 在部分环境缺 zbar DLL。这里做成可选：不可用时降级为
「用轮廓特征找疑似二维码方块」，并在 diagnostics 里写明降级原因，
绝不因为依赖缺失就让整条感知链崩掉。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from . import geometry as G


@dataclass
class CodeMark:
    id: str
    type: str
    data: str | None
    bbox: list
    norm_bbox: list
    detector: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "data": self.data,
            "bbox": [round(v, 3) for v in self.bbox],
            "norm_bbox": self.norm_bbox,
            "detector": self.detector,
        }


def _with_pyzbar(bgr: np.ndarray) -> tuple:
    """一级：pyzbar（真解码）。返回 (结果, 是否可用)。"""
    try:
        from pyzbar.pyzbar import decode
    except Exception:
        return [], False

    try:
        found = decode(bgr)
    except Exception:
        return [], False

    out = []
    for f in found:
        r = f.rect
        out.append(
            {
                "type": str(f.type),
                "data": f.data.decode("utf-8", "ignore") if f.data else None,
                "bbox": [float(r.left), float(r.top), float(r.left + r.width), float(r.top + r.height)],
            }
        )
    return out, True


def _with_zxing_cpp(bgr: np.ndarray) -> tuple:
    """二级：zxing-cpp（ZXing C++ 实现，无外部 DLL 依赖）。

    返回 (结果, API是否可用)。语义同 _with_cv2_qr：
    「API 可用但没检出」返回 ([], True)，不是「检测器坏了」。
    """
    try:
        import zxingcpp
        from zxingcpp import BarcodeFormat
    except ImportError:
        return [], False

    try:
        results = zxingcpp.read_barcodes(bgr, formats=BarcodeFormat.QRCode)
    except Exception:
        return [], False

    out = []
    for r in results:
        pos = r.position
        x0 = min(pos.top_left.x, pos.bottom_left.x)
        y0 = min(pos.top_left.y, pos.top_right.y)
        x1 = max(pos.top_right.x, pos.bottom_right.x)
        y1 = max(pos.bottom_left.y, pos.bottom_right.y)
        out.append({
            "type": "QRCODE",
            "data": r.text,
            "bbox": [float(x0), float(y0), float(x1), float(y1)],
        })
    return out, True


def _with_cv2_qr(bgr: np.ndarray) -> tuple:
    """三级：OpenCV 内建 QR 检测（无外部 DLL 依赖）。

    返回 (结果, API是否可用)。注意语义：「API 可用但没检出」返回 ([], True)，
    不能把「检出 0 个」误报成「检测器不可用」——否则会错误降级到轮廓法。
    （实测教训：detectMulti 对小码常返回 ok=False，detectAndDecode 返回空 data，
    二者都是「可用的检测器没找到」，不是「检测器坏了」。）
    """
    try:
        det = cv2.QRCodeDetector()
    except Exception:
        return [], False

    try:
        ok, infos = det.detectMulti(bgr)
    except Exception:
        # detectMulti 在部分 OpenCV 构建里不存在，退回单点 detect
        try:
            data, pts, _ = det.detectAndDecode(bgr)
        except Exception:
            return [], False
        if not data or pts is None:
            return [], True
        pts = np.asarray(pts).reshape(-1, 2)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        return [{"type": "QRCODE", "data": data, "bbox": [float(x0), float(y0), float(x1), float(y1)]}], True

    if not ok or infos is None or len(infos) == 0:
        # detectMulti 未定位到码；再给单点 detectAndDecode 一次机会
        try:
            data, pts, _ = det.detectAndDecode(bgr)
        except Exception:
            return [], True
        if data and pts is not None:
            pts = np.asarray(pts).reshape(-1, 2)
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            return [{"type": "QRCODE", "data": data, "bbox": [float(x0), float(y0), float(x1), float(y1)]}], True
        return [], True
    out = []
    for p in infos:
        pts = np.asarray(p).reshape(-1, 2)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        out.append({"type": "QRCODE", "data": None, "bbox": [float(x0), float(y0), float(x1), float(y1)]})
    return out, True


def _cv2_qr_tiled(bgr: np.ndarray, grid=(4, 4), overlap: float = 0.25,
                  upscale: float = 2.0) -> list:
    """分块扫描：cv2 QR 对全图小码（码边长 < 图宽 5%）召回差的实测解法。

    【实测依据（2026-09-12，GT 渲染图 3 张）】
      - 全图 detectMulti：3 张全挂（码边仅 45~106px，占图宽 2~5%）
      - 4x4 分块 + 2x 放大逐块 detectAndDecode：3 张检出 2 张，耗时 ~0.7s
      - 剩下 1 张在「按预期位置定点裁剪」后解码成功（见匹配层 targeted verify）
    网格间加 25% 重叠，避免码恰跨块边界被切掉；块内 2x 立方插值放大提高定位率。
    """
    try:
        det = cv2.QRCodeDetector()
    except Exception:
        return []

    H, W = bgr.shape[:2]
    gx, gy = grid
    tw, th = W // gx, H // gy
    if tw < 40 or th < 40:
        return []
    ox, oy = int(tw * overlap), int(th * overlap)

    found = []
    for iy in range(gy):
        for ix in range(gx):
            x0 = max(0, ix * tw - ox)
            y0 = max(0, iy * th - oy)
            x1 = min(W, (ix + 1) * tw + ox)
            y1 = min(H, (iy + 1) * th + oy)
            tile = bgr[y0:y1, x0:x1]
            if tile.size == 0:
                continue
            if upscale != 1.0:
                tile = cv2.resize(tile, None, fx=upscale, fy=upscale,
                                  interpolation=cv2.INTER_CUBIC)
            try:
                data, pts, _ = det.detectAndDecode(tile)
            except Exception:
                continue
            if not data or pts is None:
                continue
            a = np.asarray(pts).reshape(-1, 2) / upscale
            found.append({
                "type": "QRCODE",
                "data": data,
                "bbox": [float(a[:, 0].min() + x0), float(a[:, 1].min() + y0),
                         float(a[:, 0].max() + x0), float(a[:, 1].max() + y0)],
            })

    # 去重：块间重叠会重复命中同一码
    out = []
    for b in found:
        if not any(abs(b["bbox"][0] - o["bbox"][0]) < 30 and
                   abs(b["bbox"][1] - o["bbox"][1]) < 30 for o in out):
            out.append(b)
    return out


def _square_contours(bgr: np.ndarray) -> list:
    """降级方案：找近似正方形、内部高对比度的轮廓（二维码的视觉特征）。"""
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    # 自适应阈值对光照不均更稳
    bw = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)

    contours, _ = cv2.findContours(bw, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    img_area = float(W * H)
    out = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < img_area * 0.0008 or area > img_area * 0.08:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.05 * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        x, y, w, h = cv2.boundingRect(approx)
        if w < 24 or h < 24:
            continue
        ar = w / float(h)
        if not (0.75 <= ar <= 1.35):  # 近正方
            continue
        # 内部黑白混杂程度：二维码内部方差高
        roi = gray[y : y + h, x : x + w]
        if roi.size == 0:
            continue
        if float(roi.std()) < 40:
            continue
        out.append({"type": "SQUARE", "data": None, "bbox": [float(x), float(y), float(x + w), float(y + h)]})
    return out


def detect(bgr: np.ndarray) -> tuple:
    """返回 (CodeMark 列表, 检测器名, 是否降级)。

    【四级降级，且诚实标注】
      1. pyzbar        —— 真解码（zbar DLL 已修复：MSVCR120.dll 已安装）
      2. zxing-cpp     —— ZXing C++ 实现，无外部 DLL 依赖，第二检测器
      3. cv2 QR        —— OpenCV 内建；全图 detectMulti + 分块扫描两级召回
      4. 轮廓法        —— 找近正方高对比度块（会误报，但至少给出候选位置）
    前三级可用但均检出 0 个时，是**真结果**（degraded=False）：
    不该用轮廓法去硬凑假阳性；残存的召回缺口由匹配层的「定点裁剪多检测器解码」兜底
    （verify.py 的 _verify_qr 已集成 pyzbar + zxing-cpp + cv2-qr 三检测器+ ink_ratio 辅助）。
    **degraded=True 时上层必须把二维码类 mark 判为 not_comparable，而不是谎报 missing** ——
    这正是 v2 八态里 not_comparable 存在的意义。
    """
    H, W = bgr.shape[:2]
    raw, ok = _with_pyzbar(bgr)
    detector, degraded = "pyzbar", False

    if not ok or not raw:
        # pyzbar 不可用或检出 0 个 → 尝试 zxing-cpp
        raw_zx, ok_zx = _with_zxing_cpp(bgr)
        if ok_zx and raw_zx:
            raw, detector, degraded = raw_zx, "zxing-cpp", False
        elif not ok or not ok_zx:
            # pyzbar 和 zxing-cpp 都不可用或都检出 0 → 尝试 cv2 QR
            raw2, ok2 = _with_cv2_qr(bgr)
            if ok2 and raw2:
                raw, detector, degraded = raw2, "cv2-qr", False
            elif ok2:
                tiled = _cv2_qr_tiled(bgr)
                if tiled:
                    raw, detector, degraded = tiled, "cv2-qr-tiled", False
                else:
                    raw, detector, degraded = [], "cv2-qr", False
            else:
                raw, detector, degraded = _square_contours(bgr), "contour-square(degraded)", True

    out = []
    for i, r in enumerate(raw):
        x0, y0, x1, y1 = r["bbox"]
        out.append(
            CodeMark(
                id=f"c{i:04d}",
                type=r["type"],
                data=r["data"],
                bbox=[x0, y0, x1, y1],
                norm_bbox=G.norm_bbox(x0, y0, x1, y1, W, H),
                detector=detector,
            )
        )
    return out, detector, degraded
