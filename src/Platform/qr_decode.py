"""
H5 扫码服务端兜底：接收 base64 dataURL，用 zxing-cpp 强解码。
输入：stdin 读取 {"image": "data:image/jpeg;base64,..."}（或直接 base64 字符串）
输出：stdout 输出 {"text": "...", "format": "qr_code", "method": "..."} 或 {"text": null, "error": "..."}

设计要点：
- 自动定位白底标签矩形（zxing-js 浏览器版只有 LocalAverage，对反光/低对比度失败；
  这里用 zxing-cpp 的 GlobalHistogram + CLAHE + 多尺度重击，并对标签做正方形子候选裁剪，
  解决反光白底标签导致 QR 定位失败的问题）。
- 解码顺序：整图快速扫描 → 标签 ROI 多尺度（核心修复路径）→ 整图多尺度兜底。
- 单帧总耗时限制在 budget_s 内（默认 4s）。
"""
import sys, os, json, base64, time
import cv2
import numpy as np
try:
    import zxingcpp
except ImportError:
    print(json.dumps({"text": None, "error": "zxingcpp not installed"}))
    sys.exit(0)


def load_image():
    raw = sys.stdin.read()
    if not raw:
        return None
    try:
        body = json.loads(raw)
        b64 = body.get("image") or raw
    except Exception:
        b64 = raw
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    b64 = b64.strip()
    try:
        data = base64.b64decode(b64)
    except Exception:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def try_decode(img, bz):
    try:
        rs = zxingcpp.read_barcodes(img, try_rotate=True,
                                    try_downscale=False, try_invert=True,
                                    binarizer=bz)
    except Exception:
        return None
    for r in rs:
        if r.text and r.text.strip():
            return r.text
    return None


def find_bright_rects(gray, threshold, min_area, max_aspect=1.7, min_aspect=0.6):
    """在灰度图中找高亮矩形 ROI（白底标签）。返回 [(x, y, w, h), ...] 按面积降序。"""
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    if (mask > 0).sum() < min_area * 0.5:
        return []
    k1 = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k1)
    k2 = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, k2)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    H, W = gray.shape
    img_area = H * W
    rects = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        if area < min_area or area > img_area * 0.25:
            continue
        aspect = w / h if h else 0
        if aspect < min_aspect or aspect > max_aspect:
            continue
        rects.append((x, y, w, h, area))
    rects.sort(key=lambda r: -r[4])
    return rects


def gen_sq_candidates(roi_bgr):
    """从标签 ROI 生成正方形子候选（self + 上/中/下 或 左/中/右 对齐），避免 QR 不居中漏检。"""
    candidates = [("self", roi_bgr)]
    h, w = roi_bgr.shape[:2]
    if h == w:
        return candidates
    short = min(h, w)
    half = short // 2
    if h >= w:
        for name, cy in [("top-sq", short // 2),
                         ("center-sq", h // 2),
                         ("bottom-sq", h - short // 2)]:
            cx = w // 2
            y0 = max(0, cy - half); y1 = min(h, cy + half)
            x0 = max(0, cx - half); x1 = min(w, cx + half)
            candidates.append((name, roi_bgr[y0:y1, x0:x1]))
    else:
        for name, cx in [("left-sq", short // 2),
                         ("center-sq", w // 2),
                         ("right-sq", w - short // 2)]:
            cy = h // 2
            y0 = max(0, cy - half); y1 = min(h, cy + half)
            x0 = max(0, cx - half); x1 = min(w, cx + half)
            candidates.append((name, roi_bgr[y0:y1, x0:x1]))
    return candidates


def try_decode_roi(roi_bgr, scale_set, clip_set, bz, time_budget=None, pct=True):
    """在 ROI 上尝试 raw + CLAHE + 百分位拉伸 的 (scale, clip) 组合。返回 (text, method) 或 None。"""
    if roi_bgr is None or roi_bgr.size == 0:
        return None
    t0 = time.time() if time_budget else None
    bz_name = bz.name.split('.')[-1]
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    # 1) raw
    text = try_decode(roi_bgr, bz)
    if text:
        return text, f"raw {bz_name}"
    # 2) CLAHE + scale
    for clip in clip_set:
        if time_budget and (time.time() - t0) > time_budget:
            return None
        cl = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8)).apply(gray)
        for sc in scale_set:
            if time_budget and (time.time() - t0) > time_budget:
                return None
            up = cl if sc == 1 else cv2.resize(cl, (w * sc, h * sc), interpolation=cv2.INTER_CUBIC)
            text = try_decode(up, bz)
            if text:
                return text, f"CLAHE={clip} {sc}x {bz_name}"
    # 3) 百分位对比拉伸（应对均匀反光/低对比度）
    if pct and clip_set:
        for lo_pct, hi_pct in [(1, 99), (2, 98)]:
            if time_budget and (time.time() - t0) > time_budget:
                return None
            lo, hi = np.percentile(gray, [lo_pct, hi_pct])
            if hi <= lo:
                continue
            cs = np.clip((gray.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
            for sc in scale_set:
                if time_budget and (time.time() - t0) > time_budget:
                    return None
                up = cs if sc == 1 else cv2.resize(cs, (w * sc, h * sc), interpolation=cv2.INTER_CUBIC)
                text = try_decode(up, bz)
                if text:
                    return text, f"pct{lo_pct}-{hi_pct} {sc}x {bz_name}"
    return None


def main():
    t0 = time.time()
    img = load_image()
    if img is None:
        print(json.dumps({"text": None, "error": "invalid image"}))
        return
    H, W = img.shape[:2]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    budget_s = 4.0

    # 1) 自动定位白底标签矩形
    min_area = max(2000, int(H * W * 0.005))
    rects = find_bright_rects(g, threshold=180, min_area=min_area,
                              min_aspect=0.85, max_aspect=1.15)

    # 2) 构建候选 ROI：(面积, bgr, 名称)。标签在前，整图兜底在后。
    rois = []  # (area, bgr, name)
    for rect in rects[:5]:
        x, y, w, h, _ = rect
        sub = img[y:y + h, x:x + w]
        if sub.size == 0:
            continue
        rois.append((sub.shape[0] * sub.shape[1], sub, f"label@{x},{y},{w}x{h}"))
    rois.append((H * W, img, "full"))  # 整图兜底

    tight_rois = []  # (area, bgr, name)
    for area, bgr, name in rois:
        is_full = (name == "full")
        # 整图只保留 self（避免生成多份 1080x1080 大图浪费）；标签生成正方形子候选
        cands = [("self", bgr)] if is_full else gen_sq_candidates(bgr)
        for cn, cb in cands:
            tight_rois.append((cb.shape[0] * cb.shape[1], cb, f"{name}/{cn}"))

    LA = zxingcpp.Binarizer.LocalAverage
    GH = zxingcpp.Binarizer.GlobalHistogram

    def run_pass(roi_filter, scale_set, clip_set, bz, pb):
        """在 tight_rois 上跑一趟；roi_filter 决定只跑整图或只跑标签。命中即返回 True。"""
        pass_t0 = time.time()
        for area, roi_bgr, name in tight_rois:
            if (time.time() - t0) > budget_s:
                return False
            if (time.time() - pass_t0) > pb:
                return False
            if not roi_filter(name):
                continue
            remaining = max(0.3, pb - (time.time() - pass_t0))
            res = try_decode_roi(roi_bgr, scale_set, clip_set, bz, time_budget=remaining)
            if res:
                text, sub_method = res
                print(json.dumps({
                    "text": text, "format": "qr_code",
                    "method": f"{name} {sub_method}",
                    "elapsed_ms": int((time.time() - t0) * 1000)
                }))
                return True
        return False

    is_full = lambda n: n.startswith("full/")
    is_label = lambda n: not n.startswith("full/")

    # ── Pass 0: 小码快速通道（短边 < 200px 的标签 ROI 优先高倍放大重击）──
    # 极小低对比度灰底码（如设备标签 QR 在画面中仅占 5-8%）需要 4-6x 放大+CLAHE
    # 才能让 zxing-cpp 的 finder pattern 检测器锁定。此通道预算 1.2s。
    _tiny_rois = [(a, b, n) for a, b, n in tight_rois
                  if not n.startswith("full/") and b.shape[0] > 0 and b.shape[1] > 0
                  and min(b.shape[0], b.shape[1]) < 200]
    if _tiny_rois:
        # 只取面积最大的一个小码 ROI（通常就是含 QR 的标签）
        _best = max(_tiny_rois, key=lambda x: x[0])
        _ta, _tb, _tn = _best
        for _bz in [LA, GH]:
            if (time.time() - t0) > budget_s:
                break
            _res = try_decode_roi(_tb, [4, 5, 6], [2.0, 4.0], _bz, time_budget=1.2)
            if _res:
                text, sub_method = _res
                print(json.dumps({
                    "text": text, "format": "qr_code",
                    "method": f"{tn} {sub_method}",
                    "elapsed_ms": int((time.time() - t0) * 1000)
                }))
                return

    # Pass A: 整图 1x 快速（LA + GH，均带 CLAHE）—— 廉价，覆盖非反光/简单场景（如横屏）
    if run_pass(is_full, [1], [2.0, 3.0, 4.0], LA, 0.8):
        return
    if run_pass(is_full, [1], [2.0, 3.0, 4.0], GH, 0.8):
        return
    # Pass B: 标签 ROI 多尺度（核心修复路径）
    if run_pass(is_label, [1, 2], [2.0, 3.0, 4.0], LA, 1.0):
        return
    if run_pass(is_label, [1, 2, 3, 4], [2.0, 3.0, 4.0], GH, 2.0):
        return
    # Pass C: 整图多尺度兜底
    if run_pass(is_full, [2, 3, 4], [2.0, 3.0, 4.0], GH, 0.6):
        return

    print(json.dumps({"text": None, "error": "no QR detected",
                      "elapsed_ms": int((time.time() - t0) * 1000)}))


if __name__ == "__main__":
    main()
