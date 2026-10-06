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
from . import deskew as D


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


def _imread_oriented(path: str):
    """读图 + EXIF 方向转置（H5 照片自动扶正·2026-09-30 问题3）。

    cv2 解码不读 EXIF：手机竖拍照片（Orientation=6/8）像素「躺倒」，浏览器 <img>
    自动应用 EXIF 显示为正 →「预览对、OCR 坐标系错位」。此处按 EXIF（3/6/8 纯旋转）
    内存转置，并发生旋转时用转置后像素**回写原文件**（JPEG q=95），使 H5 展示、
    render_marked 历史缩略图、verify 定点裁剪等所有下游与 OCR 坐标系天然一致。

    ⚠ 无 EXIF 的 90° 像素侧歪**不做自动判向**——OCR 证据分/文本框角度/PCA 三种信号
    均已被实验证伪（RapidOCR 全方向可读、quad 已归一化、PCA 被画面污染，详见
    deskew.py 注释）。宁可不转、不可转错。

    返回 (bgr_or_None, orientation_deg)；回写失败只降级不阻断
    （比对仍用转置后内存像素，仅落盘文件未扶正）。
    """
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        return None, 0
    bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if bgr is None:
        return None, 0
    ori = D.exif_orientation(data.tobytes())
    if ori == 1:
        return bgr, 0
    bgr = cv2.rotate(bgr, D._EXIF_ROT_CV[ori])
    deg = {3: 180, 6: 90, 8: 270}[ori]
    try:
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if ok:
            buf.tofile(path)
    except Exception:
        pass   # 回写失败不影响比对，仅落盘文件未扶正
    return bgr, deg


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


SUBJECT_PAD = 0.08       # 内容并集外扩比例（给主体边缘留余量，避免过紧误伤）
SUBJECT_MIN_ITEMS = 2    # 少于此数量的内容不足以确定主体区域


def _unrotate_regions(items, src_hw, dst_hw, deg: float):
    """把「旋转后图（dst）」上得到的像素框逆变换回「未旋转图（src）」坐标系并重算归一化框。

    【坐标映射加强·2026-09-30】deskew 之后 OCR 的坐标属于旋转图，而展示端画的是原图。
    逆用的正是 deskew._rotate_cv 的同一矩阵（绕中心旋转 + expand 平移），保证数学上严格互逆。
    """
    h, w = int(src_hw[0]), int(src_hw[1])
    H, W = int(dst_hw[0]), int(dst_hw[1])
    try:
        m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), deg, 1.0)
        m[0, 2] += (W - w) / 2.0
        m[1, 2] += (H - h) / 2.0
        inv = cv2.invertAffineTransform(m)
    except Exception:
        return items
    for it in items:
        bb = getattr(it, "bbox", None)
        if not bb or len(bb) < 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in bb[:4])
        pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)
        one = np.concatenate([pts, np.ones((4, 1), np.float32)], axis=1)
        out = one @ inv.T
        nx0, ny0 = float(out[:, 0].min()), float(out[:, 1].min())
        nx1, ny1 = float(out[:, 0].max()), float(out[:, 1].max())
        it.bbox = [nx0, ny0, nx1, ny1]
        it.norm_bbox = G.norm_bbox(nx0, ny0, nx1, ny1, w, h)
    return items


def _subject_of(texts, codes, pad: float = SUBJECT_PAD, min_items: int = SUBJECT_MIN_ITEMS):
    """从检测到的文字/码推导照片里的产品主体（有效部位）区域。

    文档 §5：通过质量检查后要检测「文字区域、二维码区域、图标区域和产品轮廓」，
    匹配应以产品部位为单位（§4：图块以视图或打标区域为单位）。
    打标内容必然位于产品表面，因此内容分布的外接区就是有效部位区域的保守近似 ——
    用它把「照片里与产品无关的区域」排除出比对，避免拿整张原图乱匹配。

    返回归一化 [x, y, w, h]；内容不足（<min_items）时返回 None —— 此时不启用主体约束。
    """
    boxes = []
    for t in (texts or []):
        nb = getattr(t, "norm_bbox", None)
        if nb and len(nb) >= 4:
            boxes.append(nb)
    for c in (codes or []):
        nb = getattr(c, "norm_bbox", None)
        if nb and len(nb) >= 4:
            boxes.append(nb)
    if len(boxes) < min_items:
        return None

    x0 = min(float(b[0]) for b in boxes)
    y0 = min(float(b[1]) for b in boxes)
    x1 = max(float(b[0]) + float(b[2]) for b in boxes)
    y1 = max(float(b[1]) + float(b[3]) for b in boxes)
    x0 = max(0.0, x0 - pad)
    y0 = max(0.0, y0 - pad)
    x1 = min(1.0, x1 + pad)
    y1 = min(1.0, y1 + pad)
    if x1 - x0 <= 0.01 or y1 - y0 <= 0.01:
        return None

    return {
        "norm_bbox": [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)],
        "items": len(boxes),
        "source": "content_union",
    }


def _whiten_code_regions(bgr: np.ndarray, codes: list, pad: int = 0) -> np.ndarray:
    """把已检出的码（QR/方块码）区域白化，返回供文本 OCR 使用的新图。

    【指令 N·QR 白化，2026-10-06】
    目的：RapidOCR 会把 QR 码图形（定位方块 + 码内嵌文本）误读成文本行，
    生成「口口QHR45」这类伪前缀，进而污染 photoTexts、触发 degraded。
    此处仅在「文本识别」环节把 QR 图形从输入图抹掉，降低误识别。

    ⚠ 范围约束（用户拍板）：
      - 只白化 M.detect 已检出的 **码区域**；图标不在 codes 内，绝不被白化。
      - 不改变 codes 输出、不改变 QR 的匹配标示规则与实现（M.detect 在原 proc 上跑，结果原样返回）。
      - 文本 OCR 看到的只是"无 QR 图形"的图，匹配逻辑对 QR 的判定不变量。
    pad=0 用精确 bbox（zxing-cpp/cv2 的 QR bbox 已含定位方块，精确框即可消除口口，
    实测 pad=-4~8 均干净且 73K0016 等邻字无夹损）；codes 为空则原样返回（零成本）。
    """
    if not codes:
        return bgr
    H, W = bgr.shape[:2]
    out = bgr.copy()
    for c in codes:
        nb = getattr(c, "norm_bbox", None)
        if not nb or len(nb) < 4:
            continue
        x0 = int(round(nb[0] * W)) + pad
        y0 = int(round(nb[1] * H)) + pad
        x1 = int(round((nb[0] + nb[2]) * W)) - pad
        y1 = int(round((nb[1] + nb[3]) * H)) - pad
        if x1 <= x0 or y1 <= y0:
            continue
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(W, x1); y1 = min(H, y1)
        cv2.rectangle(out, (x0, y0), (x1, y1), (255, 255, 255), -1)
    return out


def observe_image(bgr, source_name: str = "", sha256: str = "", do_warp: bool = True,
                  max_side: int = MAX_SIDE) -> dict:
    """对已加载的 BGR 图像做感知，返回 observe/1 文档。"""
    t0 = time.perf_counter()

    # 质量在**原图**上评：缩放会改变清晰度与噪声的统计量
    quality = Q.assess(bgr)
    warped, geo = G.normalize(bgr, do_warp=do_warp)

    # OCR 在缩放后的图上跑（耗时的主要来源）
    resized, _ = _resize_max_side(warped, max_side)
    # 【2026-09-29 迁移增强】复用 V1 平面内旋转纠偏：透视矫正后再做纯旋转纠偏，
    # 提升照片侧 L2 落点准确度与稳定性。平正照片 angle≈0 不旋转（零回归）。
    proc, deskew_deg = D.deskew(resized)

    # 【指令 N·QR 白化，2026-10-06】先检出码（M.detect 在原 proc 上跑，结果原样返回，
    # 不影响 QR 匹配标示规则），再把码区域白化后交给文本 OCR，避免 QR 图形被误读为文本。
    # 顺序：M.detect → 白化 → T.detect（同一张 proc 几何不变，_unrotate_regions 仍成立）。
    codes, detector, degraded = M.detect(proc)
    proc_for_text = _whiten_code_regions(proc, codes)
    texts = T.detect(proc_for_text)

    # 【2026-09-30 坐标映射加强】deskew 旋转后 OCR 拿到的是**旋转图**坐标系，
    # 而 H5/render_marked 都把框画在**原图**上 → 小角度旋转（实测 1~2°）会让整片框偏移。
    # 此处把 OCR 框逆变换回「未旋转（仅等比缩放）」坐标系：等比缩放下归一化坐标不变，
    # 故只需按旋转矩阵求逆、再对未旋转图尺寸归一化即可（零额外成本）。
    if deskew_deg:
        texts = _unrotate_regions(texts, resized.shape[:2], proc.shape[:2], deskew_deg)
        codes = _unrotate_regions(codes, resized.shape[:2], proc.shape[:2], deskew_deg)

    # 【文档 §5 / §6】OCR 是「能否比对」的前置判断依据：
    # 能正常取出内容就不以清晰度为由拒绝，质量类理由降级为提示。
    Q.apply_ocr_evidence(quality, [t.to_dict() for t in texts], [c.to_dict() for c in codes])

    # 【文档 §4/§5】有效部位区域：产品主体所在范围，供比对只在主体内建立一对一关系
    subject = _subject_of(texts, codes)

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
        "subject": subject,
        "texts": [t.to_dict() for t in texts],
        "codes": [c.to_dict() for c in codes],
        "diagnostics": {
            "observe_ms": ms,
            "code_detector": detector,
            "code_degraded": degraded,
            "text_regions": len(texts),
            "code_count": len(codes),
            "deskew_deg": deskew_deg,
        },
    }


def observe_file(path: str, do_warp: bool = True) -> dict:
    bgr, orient_deg = _imread_oriented(path)
    if bgr is None:
        raise RuntimeError(f"cannot read image: {path}")
    doc = observe_image(bgr, source_name=path, sha256=_sha256(path), do_warp=do_warp)
    # 方向扶正审计字段：orientation_deg=施加的净旋转（0=无；文件已按扶正后像素回写）
    try:
        doc["diagnostics"]["orientation_deg"] = orient_deg
    except Exception:
        pass
    return doc
