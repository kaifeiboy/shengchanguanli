#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocr_cli.py v7 — RapidOCR 检测型引擎（主）+ Tesseract（降级备用）
=================================================================
v7 重构：以 RapidOCR（PaddleOCR-ONNX 轻量版）为主引擎，替代 v6 的
  「多候选生成 + Tesseract 串行」整条管线。

  为什么换：
    - v6 有 ~600 行自研预处理（白平衡/CLAHE/Sauvola/Retinex/形态学去线/
      ROI裁剪/unsharp/Niblack\u2026），用于对抗「杂线干扰 / 光照不均」。
    - RapidOCR 的检测阶段（DB网络）天然从背景中抠出文字区域，
      CAD杂乱线条、手、桌面、地面等环境因素在检测层就被排除，
      不需要任何后处理补丁。实测贴近拍摄照片：
        \u00b7 Tesseract 管线：p1hjq / 400860 (截断/乱码) / 噪声碎片
        \u00b7 RapidOCR：PC-P1HJQ服务热线：400860111（完整输出，conf=0.907）

  接口不变：python ocr_cli.py <input_image>  -> 最佳文本走 stdout
  环境变量 OCR_JSON=1 -> 输出 JSON {text, conf, model_candidate}

  Legacy 函数（保留供 cut_region.py 兼容，已不再被 main() 调用）：
    preprocess_block() / run_tess() / score_text() / build_candidates() / ...
"""

# ---- 诊断日志（固定绝对路径）----
_CRASH_LOG = r"E:\workaaa\shengchanguanli\data\ocr\ocr_crash.log"

def _log_crash(tag, exc):
    try:
        import datetime, traceback
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%H:%M:%S}] {tag}: {exc!r}\n")
            traceback.print_exc(file=f)
    except Exception:
        pass

# ---- 导入 ----
try:
    import sys, os, re, time, json, subprocess, uuid, tempfile
    import numpy as np
    from PIL import Image, ImageOps
    from scipy import ndimage
    # 主引擎：RapidOCR（PaddleOCR-ONNX）
    from rapidocr_onnxruntime import RapidOCR
    _rapidocr = None
    def _get_rapidocr():
        global _rapidocr
        if _rapidocr is None:
            _rapidocr = RapidOCR()
        return _rapidocr
except Exception as imp_err:
    _log_crash("IMPORT_FAIL", imp_err)
    raise SystemExit(1)

TESS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TMP = tempfile.gettempdir()


# ═══════════════════════════════════════════════════════════════
#  公共工具函数（保持对外接口兼容）
# ═══════════════════════════════════════════════════════════════

def norm_path(p):
    """Git-Bash /e/work/... -> Windows e:/work/..."""
    if not p:
        return p
    if len(p) > 2 and p[0:1] == "/" and p[2:3] == "/":
        drive = p[1:2]
        if drive.isalpha():
            return drive + ":/" + p[3:]
    return p


def load_rgb_gray(path):
    img = Image.open(path)
    # ⭐ 缩小图片到最大边 960px，OCR 加速 2-4 倍
    MAX_DIM = 960
    w, h = img.size
    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    rgb = np.asarray(img.convert("RGB")).astype(np.float32)
    gray = np.asarray(img.convert("L")).astype(np.float32)
    return rgb, gray


def extract_qr_codes(gray_arr):
    """用 OpenCV QRCodeDetector 检测并解码图片中的所有二维码。"""
    try:
        import cv2
        if gray_arr.dtype != np.uint8:
            gray_u8 = np.clip(gray_arr, 0, 255).astype(np.uint8)
        else:
            gray_u8 = gray_arr
        detector = cv2.QRCodeDetector()
        data, pts, straight = detector.detectAndDecodeMulti(gray_u8)
        if data is not None and len(data) > 0:
            return [d.strip() for d in data if d.strip()]
    except Exception:
        pass
    return []


_MODEL_RE = re.compile(r"[A-Za-z]{2,}-?[A-Za-z]*[0-9][A-Za-z0-9]*")
def is_model_candidate(text):
    """含长度>=5且带数字的字母数字串 -> True（镜像 C# ContainsModelIdentifier）。"""
    for m in _MODEL_RE.findall(text or ""):
        if len(m.replace("-", "")) >= 5:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  Legacy 函数（保留供 cut_region.py 兼容；main() 不再调用）
# ═══════════════════════════════════════════════════════════════
# 以下函数是 v6 Tesseract 管线的核心组件。
#  v7 切换到 RapidOCR 主引擎后，它们仅作为 fallback / 外部模块
#  （cut_region.py）的依赖保留。未来 cut_region.py 也迁移后可整体删除。

def white_balance(rgb):
    flat = rgb.reshape(-1, 3); means = flat.mean(0); means = np.maximum(means, 1.0)
    scale = np.clip(128.0 / means, 0.6, 1.8)
    return np.clip(rgb * scale, 0, 255).astype(np.uint8)

def clahe(gray, tile=16, lo_p=2.0, hi_p=98.0):
    h, w = gray.shape
    ph, pw = (tile - h % tile) % tile, (tile - w % tile) % tile
    work = gray if (ph == 0 and pw == 0) else np.pad(gray, ((0, ph), (0, pw)), mode="edge")
    out = np.empty_like(work, dtype=np.float32)
    for y in range(0, work.shape[0], tile):
        for x in range(0, work.shape[1], tile):
            blk = work[y:y + tile, x:x + tile]
            lo, hi = np.percentile(blk, [lo_p, hi_p])
            out[y:y + tile, x:x + tile] = np.clip((blk - lo) / (hi - lo) * 255.0, 0, 255) if hi > lo else blk
    return out[:h, :w]

def sauvola(gray, k=0.20, R=128.0, win=None):
    H, W = gray.shape
    if win is None: win = max(15, int(min(H, W) / 30) | 1)
    g = gray.astype(np.float32)
    mean = ndimage.uniform_filter(g, win)
    sq = ndimage.uniform_filter(g * g, win)
    std = np.sqrt(np.maximum(0.0, sq - mean * mean))
    thr = mean * (1.0 - k * (1.0 - std / R))
    return np.where(g >= thr, 255, 0).astype(np.uint8)

def gamma(gray, g):
    return np.clip(np.power(np.clip(gray, 0, 255) / 255.0, g) * 255.0, 0, 255).astype(np.uint8)

def retinex(gray, sigma=80.0):
    g = gray.astype(np.float32)
    blurred = ndimage.gaussian_filter(g, sigma=sigma)
    blurred = np.where(blurred < 1.0, 1.0, blurred)
    return np.clip(g / blurred * 128.0, 0, 255).astype(np.uint8)

def needs_invert(gray):
    hist, _ = np.histogram(gray, 256, [0, 256]); total = float(gray.size)
    if total == 0: return False
    sumv = float(np.dot(np.arange(256), hist)); sum_b = 0.0; w_b = 0; mx = 0.0; thr = 127
    for i in range(256):
        w_b += int(hist[i])
        if w_b == 0: continue
        w_f = total - w_b
        if w_f == 0: break
        sum_b += i * int(hist[i]); m_b = sum_b / w_b; m_f = (sumv - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > mx: mx = between; thr = i
    fg = gray[gray < thr]; bg = gray[gray >= thr]
    if fg.size == 0 or bg.size == 0: return False
    return float(fg.mean()) > float(bg.mean())

def run_tess(arr, psm):
    """Tesseract 单次调用（legacy fallback）。"""
    if arr.dtype != np.uint8: arr = arr.astype(np.uint8)
    if arr.ndim == 2: arr = np.ascontiguousarray(arr)
    tmp = os.path.join(TMP, "ocr7_%s.png" % uuid.uuid4().hex)
    txt = tmp + ".ocrtmp.txt"
    try:
        Image.fromarray(arr).save(tmp)
        subprocess.run([TESS, tmp, tmp + ".ocrtmp", "-l", "chi_sim+eng", "--psm", str(psm)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
        with open(txt, encoding="utf-8", errors="ignore") as f: return f.read()
    except Exception: return ""
    finally:
        for f in (tmp, txt):
            try: os.remove(f)
            except OSError: pass

def run_tess_tsv(arr, psm):
    """Tesseract TSV 置信度（legacy，JSON 模式用）。"""
    if arr.dtype != np.uint8: arr = arr.astype(np.uint8)
    arr = np.ascontiguousarray(arr)
    tmp = os.path.join(TMP, "ocrtsv_%s.png" % uuid.uuid4().hex)
    out_base = tmp + ".ocrtsv"; out_tsv = out_base + ".tsv"
    try:
        Image.fromarray(arr).save(tmp)
        subprocess.run([TESS, tmp, out_base, "-l", "chi_sim+eng", "--psm", str(psm),
                        "-c", "tessedit_create_tsv=1"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
        confs = []
        with open(out_tsv, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("level"): continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 12: continue
                try: c = float(parts[10])
                except (ValueError, IndexError): continue
                if c >= 0: confs.append(c)
        return (sum(confs) / len(confs)) if confs else 0.0
    except Exception: return 0.0
    finally:
        for fp in (tmp, out_tsv, out_base):
            try: os.remove(fp)
            except OSError: pass

def score_text(text):
    """OCR 结果评分（legacy，cut_region.py 用）。"""
    if not text.strip(): return 0
    score = 0.0; has_strong = False
    digit_runs = re.findall(r"\d{4,}", text)
    if digit_runs: has_strong = True; score += sum(min(len(d), 13) for d in digit_runs) * 3.0
    if (re.search(r"PC[- ]?[A-Za-z0-9]{2,}", text, re.I)
        or re.search(r"QHR\d{1,}", text, re.I)
        or "HITACHI" in text.upper() or "POWERLINE" in text.upper()
        or "SHINTECH" in text.upper()):
        has_strong = True; score += 100.0
    for m in re.findall(r"[A-Za-z]{2,}[-\s]?[A-Za-z0-9]{2,}", text):
        m2 = m.replace("-", "").replace(" ", "")
        if len(m2) >= 5 and (any(c.isdigit() for c in m2) or m2.isupper()):
            has_strong = True; score += 35.0
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    score += min(cn, 40) * (0.5 if has_strong else 0.1)
    for line in text.strip().split("\n"):
        line = line.strip()
        if len(line) <= 3 and not re.search(r"\d", line) and not re.search(r"[A-Za-z]{2,}", line):
            score -= 1.0
    return max(0.0, score)

def is_drawing_block(gray):
    """Legacy：图纸块检测（v6 用，RapidOCR 不需要）。"""
    if gray.ndim != 2: return False
    h, w = gray.shape
    return float(gray.mean()) > 220 and float((gray < 175).mean()) < 0.05 and (max(h, w) < 800 or h * w < 600_000)

def median_denoise(gray, size=3):
    return ndimage.median_filter(gray, size=size).astype(np.float32)

def unsharp(gray_u8, amount=1.4, sigma=0.8):
    try:
        import cv2
        blur = cv2.GaussianBlur(gray_u8, (0, 0), sigma)
        sharp = gray_u8.astype(np.int16) + amount * (gray_u8.astype(np.int16) - blur.astype(np.int16))
        return np.clip(sharp, 0, 255).astype(np.uint8)
    except Exception: return gray_u8

def niblack_thr(gray_f, k=0.15, win=25):
    mean = ndimage.uniform_filter(gray_f, win)
    sq = ndimage.uniform_filter(gray_f * gray_f, win)
    std = np.sqrt(np.clip(sq - mean * mean, 0, None))
    return np.where(gray_f >= (mean + k * std), 255, 0).astype(np.uint8)

def preprocess_block(gray):
    """Legacy：图纸块专用预处理候选生成（cut_region.py 用）。
    
    注意：v7 主流程已切换到 RapidOCR，此函数仅保留给 cut_region.py
    的 _ocr_region() 使用。新代码不应调用它。"""
    H, W = gray.shape; cands = []
    gray_dn = median_denoise(gray)
    binary_a = (gray_dn < 200).astype(np.uint8) * 255
    struct_s = ndimage.generate_binary_structure(2, 2)
    eroded = ndimage.binary_erosion(binary_a // 255, structure=struct_s, iterations=1).astype(np.uint8) * 255
    opened = ndimage.binary_dilation(eroded // 255, structure=struct_s, iterations=1).astype(np.uint8) * 255
    labeled, n = ndimage.label(opened // 255)
    if n > 0:
        sizes = ndimage.sum(opened // 255, labeled, range(1, n + 1))
        top_k = min(4, n); top_labels = np.argsort(sizes)[-top_k:] + 1
        all_ys, all_xs = [], []
        for lbl in top_labels:
            ys, xs = np.where(labeled == lbl)
            all_ys.extend(ys.tolist()); all_xs.extend(xs.tolist())
        if all_ys and all_xs:
            y0, y1 = int(min(all_ys)), int(max(all_ys)) + 1
            x0, x1 = int(min(all_xs)), int(max(all_xs)) + 1
            ph, pw = y1 - y0, x1 - x0
            pad_h, pad_w = max(8, ph // 10), max(8, pw // 10)
            y0 = max(0, y0 - pad_h); y1 = min(H, y1 + pad_h)
            x0 = max(0, x0 - pad_w); x1 = min(W, x1 + pad_w)
            roi_opened = opened[y0:y1, x0:x1]; roi_gray = gray_dn[y0:y1, x0:x1]
        else: roi_opened, roi_gray = opened, gray_dn
    else: roi_opened, roi_gray = opened, gray_dn
    rh, rw = roi_opened.shape
    scale = 1
    if max(rh, rw) < 400: scale = 3
    elif max(rh, rw) < 600: scale = 2
    if scale > 1:
        new_rw, new_rh = rw * scale, rh * scale
        roi_opened = np.array(Image.fromarray(roi_opened).resize((new_rw, new_rh), Image.LANCZOS))
        roi_gray_u8 = np.array(Image.fromarray(roi_gray.astype(np.uint8)).resize((new_rw, new_rh), Image.LANCZOS))
        roi_gray_u8 = unsharp(roi_gray_u8); roi_gray = roi_gray_u8.astype(np.float32)
    else: roi_gray_u8 = roi_gray.astype(np.uint8)
    cands.append(("blk_open_psm6", roi_opened, 6))
    cands.append(("blk_open_psm11", roi_opened, 11))
    roi_gray_f = roi_gray.astype(np.float32) if roi_gray.dtype != np.float32 else roi_gray
    cands.append(("blk_sauvola_roi", sauvola(roi_gray_f), 6))
    cands.append(("blk_clahe_sauvola", sauvola(clahe(roi_gray_f, tile=8)), 6))
    cands.append(("blk_niblack_roi", niblack_thr(roi_gray_f), 6))
    binary_b = (gray_dn < 190).astype(np.uint8) * 255
    if scale > 1:
        bh, bw = gray_dn.shape
        binary_b = np.array(Image.fromarray(binary_b).resize((bw * scale, bh * scale), Image.LANCZOS))
    cands.append(("blk_thr190_psm6", binary_b, 6))
    if scale > 1:
        fh, fw = gray_dn.shape
        gray_scaled_full = np.array(Image.fromarray(gray_dn.astype(np.uint8)).resize((fw*scale, fh*scale), Image.LANCZOS))
    else: gray_scaled_full = gray_dn
    cands.append(("blk_full_sauvola", sauvola(gray_scaled_full.astype(np.float32)), 6))
    ac = np.array(Image.fromarray(gray_dn.astype(np.uint8)).convert('L'), dtype=np.float32)
    lo, hi = np.percentile(ac, [1, 99])
    if hi > lo: ac = np.clip((ac - lo) / (hi - lo) * 255, 0, 255)
    if scale > 1:
        fh, fw = ac.shape
        ac_scaled = np.array(Image.fromarray(ac.astype(np.uint8)).resize((fw*scale, fh*scale), Image.LANCZOS))
    else: ac_scaled = ac
    for thr_d in (150, 160, 170):
        cands.append((f"blk_ac_thr{thr_d}", np.where(ac_scaled < thr_d, 0, 255).astype(np.uint8), 6))
    if H < 200 and W / H > 2:
        scale_e = max(3, int(300 / H))
        fh_e, fw_e = gray_dn.shape
        img_3x = np.array(Image.fromarray(gray_dn.astype(np.uint8)).resize((fw_e * scale_e, fh_e * scale_e), Image.LANCZOS))
        cands.append(("blk_strip_sauvola", sauvola(img_3x.astype(np.float32)), 7))
        ac_e = np.array(Image.fromarray(gray_dn.astype(np.uint8)).convert('L'), dtype=np.float32)
        lo_e, hi_e = np.percentile(ac_e, [1, 99])
        if hi_e > lo_e: ac_e = np.clip((ac_e - lo_e) / (hi_e - lo_e) * 255, 0, 255)
        ac_e_3x = np.array(Image.fromarray(ac_e.astype(np.uint8)).resize((fw_e * scale_e, fh_e * scale_e), Image.LANCZOS))
        for thr_e in (140, 150, 160):
            cands.append((f"blk_strip_ac{thr_e}", np.where(ac_e_3x < thr_e, 0, 255).astype(np.uint8), 7))
    return cands

def build_candidates(rgb, gray):
    """Legacy：照片多候选生成（v6 用，RapidOCR 替代）。"""
    wb = white_balance(rgb)
    wb_gray = np.asarray(Image.fromarray(wb).convert("L")).astype(np.float32)
    enh = np.asarray(ImageOps.autocontrast(Image.fromarray(wb_gray.astype(np.uint8)), cutoff=2).convert("L")).astype(np.float32)
    cands = [("auto_thr170", np.where(enh < 170, 0, 255).astype(np.uint8), 6)]
    cands.append(("wb_sauvola", sauvola(wb_gray), 6))
    med = float(np.median(wb_gray)); g = 0.7 if med < 80 else (1.4 if med > 180 else 1.0)
    cands.append(("wb_gamma%.1f_sauvola" % g, sauvola(gamma(wb_gray, g)), 6))
    if needs_invert(wb_gray): cands.append(("wb_invert_sauvola", sauvola(255.0 - wb_gray), 6))
    cands.append(("wb_retinex_sauvola", sauvola(retinex(wb_gray).astype(np.float32)), 6))
    return cands

def focus_engrave_bbox(gray):
    """Legacy：激光打标区域聚焦（v7 用，RapidOCR 检测阶段已替代）。"""
    H, W = gray.shape; total_px = float(H * W)
    thresh_val = float(np.percentile(gray, 70)); bright_mask = gray > thresh_val
    bright_ratio = float(bright_mask.mean())
    if bright_ratio < 0.15 or bright_ratio > 0.90: return None
    labeled, n = ndimage.label(bright_mask)
    if n == 0: return None
    sizes = ndimage.sum(bright_mask, labeled, range(1, n + 1))
    largest_label = int(np.argmax(sizes)) + 1; product_mask = (labeled == largest_label)
    product_ratio = float(product_mask.sum()) / total_px
    if product_ratio < 0.08: return None
    ys, xs = np.where(product_mask)
    y0, y1 = int(ys.min()), int(ys.max()) + 1; x0, x1 = int(xs.min()), int(xs.max()) + 1
    ph, pw = y1 - y0, x1 - x0
    pad_h = max(20, int(ph * 0.05)); pad_w = max(20, int(pw * 0.05))
    y0 = max(0, y0 - pad_h); y1 = min(H, y1 + pad_h); x0 = max(0, x0 - pad_w); x1 = min(W, x1 + pad_w)
    crop_area = (y1 - y0) * (x1 - x0)
    if crop_area < total_px * 0.12: return None
    return (y0, y1, x0, x1)


# ═══════════════════════════════════════════════════════════════
#  v7 核心：RapidOCR 主引擎 + Tesseract 降级
# ═══════════════════════════════════════════════════════════════

def _rapidocr_raw(image_path):
    """返回结构化检测结果 [(box, text, conf), ...]，供自动 ROI 聚类用。"""
    engine = _get_rapidocr()
    try:
        result, _ = engine(image_path)
    except Exception as ex:
        _log_crash("RAPIDOCR", ex)
        return []
    out = []
    if result:
        for item in result:
            if len(item) >= 3:
                box, txt, conf = item[0], item[1], float(item[2])
            elif len(item) == 2:
                box, txt, conf = item[0], item[1], 0.9
            else:
                continue
            txt = (txt or "").strip()
            if txt:
                out.append((box, txt, conf))
    return out


def _auto_roi(image_path):
    """自动 ROI：用 RapidOCR 检测框定位打标区（型号+服务热线通常同区），
    裁剪放大后二次 OCR，从而忽略手/桌面/地面/CAD 杂线等环境。
    失败/无框时返回 None，由调用方回退到全图 RapidOCR。

    设计要点（对齐「忽略规则不变」）：
      - 仅对检测到的文字簇做二次识别，背景/手/桌面/地面天然被排除；
      - 优先选含型号/服务热线标识的簇，避免把标题栏/尺寸表误当打标区；
      - 失败安全：任何异常都返回 None，不影响原有全图 RapidOCR 路径。
    """
    det = _rapidocr_raw(image_path)
    if not det:
        return None
    try:
        with Image.open(image_path) as im:
            W_img, H_img = im.size
    except Exception:
        return None
    if W_img <= 0 or H_img <= 0:
        return None

    # 解析每个检测框：中心 + 包围盒 + 文本
    pts = []
    for box, txt, conf in det:
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except Exception:
            continue
        if not xs or not ys:
            continue
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        pts.append((cx, cy, min(xs), min(ys), max(xs), max(ys), txt))

    n = len(pts)
    if n == 0:
        return None

    # 单连锁聚类（中心近 或 框相邻 即同簇）
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    thr = 0.25 * max(W_img, H_img)
    for i in range(n):
        for j in range(i + 1, n):
            di = ((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2) ** 0.5
            ax0, ay0, ax1, ay1 = pts[i][2], pts[i][3], pts[i][4], pts[i][5]
            bx0, by0, bx1, by1 = pts[j][2], pts[j][3], pts[j][4], pts[j][5]
            ox = not (ax1 < bx0 - thr * 0.5 or bx1 < ax0 - thr * 0.5)
            oy = not (ay1 < by0 - thr * 0.5 or by1 < ay0 - thr * 0.5)
            if di < thr or (ox and oy):
                union(i, j)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    def has_sig(t):
        return bool(re.search(r"[A-Za-z]{2,}-?[A-Za-z0-9]*[0-9]", t)) \
            or bool(re.search(r"400[-]?\d{7}", t)) \
            or ("服务热线" in t)

    best = None
    best_score = -1.0
    for idxs in clusters.values():
        area = sum((pts[i][4] - pts[i][2]) * (pts[i][5] - pts[i][3]) for i in idxs)
        sig = any(has_sig(pts[i][6]) for i in idxs)
        sc = (2.0 if sig else 0.0) + area / float(W_img * H_img)
        if sc > best_score:
            best_score = sc
            best = idxs
    if best is None:
        return None

    # ⭐ v19.8 修复：合并邻近簇（同产品面板上物理分离的标签区不应被丢弃）
    #   例：背面板左侧主标签(LOWVOLTAGE/DC15) 与右侧功能标签(地暖阀) 分属不同簇，
    #   但都在同一产品面上，应一并纳入 ROI。
    #   判定：其他簇的 bbox 与最佳簇 bbox 的中心距 < 图像短边 * 0.4 → 合并
    merged = set(best)
    best_cx = sum((pts[i][2] + pts[i][4]) / 2 for i in best) / len(best)
    best_cy = sum((pts[i][3] + pts[i][5]) / 2 for i in best) / len(best)
    short_side = min(W_img, H_img)
    merge_thr = short_side * 0.4
    for idxs_other in clusters.values():
        if idxs_other[0] in merged:
            continue
        other_cx = sum((pts[i][2] + pts[i][4]) / 2 for i in idxs_other) / len(idxs_other)
        other_cy = sum((pts[i][3] + pts[i][5]) / 2 for i in idxs_other) / len(idxs_other)
        dist = ((best_cx - other_cx) ** 2 + (best_cy - other_cy) ** 2) ** 0.5
        if dist < merge_thr:
            merged.update(idxs_other)
    best = list(merged)

    # ⭐ v19.41 B 中期：簇内全部为小字（CAD 12px 打标区）→ 跳过二次 OCR。
    #   二次 OCR（裁剪+LANCZOS 2x 放大+rapidocr_raw）会劣化：6607→8807、NSWATGNCWO_→WATBNCWO
    #   （已实跑 5 变体预处理：1x/2x/4x/锐化/对比度均无法救回）。全图原始 _rapidocr_raw
    #   对 6607 正确（conf=0.93）。返回 None → main 回退全图原始检测，避免固化错误入 DB。
    SMALL_TEXT_HEIGHT = 15
    if all((pts[i][5] - pts[i][3]) < SMALL_TEXT_HEIGHT for i in best):
        return None

    x0 = min(pts[i][2] for i in best)
    y0 = min(pts[i][3] for i in best)
    x1 = max(pts[i][4] for i in best)
    y1 = max(pts[i][5] for i in best)
    pad_x = max(10, int((x1 - x0) * 0.15))
    pad_y = max(10, int((y1 - y0) * 0.15))
    x0 = max(0, int(x0 - pad_x)); y0 = max(0, int(y0 - pad_y))
    x1 = min(W_img, int(x1 + pad_x)); y1 = min(H_img, int(y1 + pad_y))

    try:
        crop = Image.open(image_path).convert("RGB").crop((x0, y0, x1, y1))
        scale = 2.0
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
        tmp = os.path.join(TMP, "roi_%s.png" % uuid.uuid4().hex)
        crop.save(tmp)
        det2 = _rapidocr_raw(tmp)
        try:
            os.remove(tmp)
        except OSError:
            pass
        texts2 = [t for _, t, _ in det2]
        if not texts2:
            # ⭐ 修复：二次OCR对超扁/极端长宽比裁剪图可能检测失败(0框)。
            #   此时回退用第一次检测的原始文本(已含型号/热线/NFC)，避免整体回退 None
            #   → 触发全图OCR产生跨块溢出假差异。
            texts2 = [pts[i][6] for i in best]
        return "\n".join(texts2)
    except Exception as ex:
        _log_crash("AUTOROI", ex)
        # ⭐ 修复：异常时也回退第一次检测文本，而非返回 None
        if best:
            return "\n".join(pts[i][6] for i in best)
        return None


def _run_rapidocr(image_path):
    """用 RapidOCR 做检测型 OCR。
    
    返回 (text: str, confidence: float, elapsed: float) 或 (None, 0, 0) 失败。
    """
    engine = _get_rapidocr()
    try:
        # ⭐ 加载图片并缩小到最大边 960px，避免超高分辨率图（1008x2560 手机截图）极慢
        from PIL import Image
        import numpy as np
        pil = Image.open(image_path).convert("RGB")
        w, h = pil.size
        max_dim = 960
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            pil = pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img_arr = np.asarray(pil)
        result, elapsed = engine(img_arr)
        if not result:
            return "", 0.0, elapsed
        # 合并所有检测结果为文本
        texts = []
        conf_sum = 0.0
        count = 0
        for item in result:
            # RapidOCR 返回 (box, text, confidence)
            if len(item) >= 3:
                box, txt, conf = item[0], item[1], float(item[2])
            elif len(item) == 2:
                box, txt, conf = item[0], item[1], 0.9
            else:
                continue
            txt = (txt or "").strip()
            if txt:
                texts.append(txt)
                conf_sum += conf
                count += 1
        full_text = "\n".join(texts)
        avg_conf = conf_sum / max(count, 1)
        return full_text, avg_conf, elapsed
    except Exception as ex:
        _log_crash("RAPIDOCR", ex)
        return "", 0.0, 0.0


def _tesseract_fallback(image_path):
    """极简 Tesseract 降级：单次 PSM6 全图识别（无候选生成）。"""
    try:
        img = Image.open(image_path).convert("L")
        arr = np.asarray(img)
        # 简单自适应阈值
        if arr.mean() > 200:  # 可能是白底图纸块
            arr = np.where(arr < 180, 0, 255).astype(np.uint8)
        return run_tess(arr, 6)
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: ocr_cli.py <input>\n")
        sys.exit(2)

    inp = norm_path(sys.argv[1])
    if not os.path.exists(inp):
        sys.stderr.write(f"file not found: {inp}\n")
        sys.exit(4)

    # ── Step 1: RapidOCR 主引擎 ──
    engine_used = "rapidocr"
    roi_text = _auto_roi(inp)
    if roi_text and roi_text.strip():
        text, conf = roi_text, 0.9
        engine_used = "rapidocr_roi"
    else:
        text, conf, _ = _run_rapidocr(inp)

    # ── Step 2: 若 RapidOCR 无输出，Tesseract 降级 ──
    fallback_used = False
    if not text.strip():
        text = _tesseract_fallback(inp)
        fallback_used = True

    # ── Step 3: QR 码增强提取 ──
    try:
        _, gray = load_rgb_gray(inp)
        qr_texts = extract_qr_codes(gray.astype(np.uint8))
        for qt in qr_texts:
            text += f"\n[QR:]{qt}"
    except Exception:
        pass

    # ── Step 4: JSON 模式（batch_ocr 触发）──
    if os.environ.get("OCR_JSON") == "1":
        printed = text.split("\n[QR:]")[0].split("\n")[0] if text else ""
        mc = is_model_candidate(printed)
        sys.stdout.write(json.dumps({
            "text": text,
            "conf": round(conf, 1),
            "model_candidate": mc,
            "engine": "tesseract_fallback" if fallback_used else engine_used,
        }, ensure_ascii=False))
        return

    sys.stdout.write(text)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as ex:
        try:
            import traceback
            with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{__import__('datetime').datetime.now():%H:%M:%S}] CRASH: {ex}\n")
                traceback.print_exc(file=f)
        except:
            pass
        sys.exit(1)
