# -*- coding: utf-8 -*-
"""
inventory_ocr.py —— 库存尾数拍照识别引擎（独立进程，不影响 Drawings 的 OCR worker）

命令：
  correct --in <图片> --out <矫正后图片> [--pts "x1,y1;x2,y2;x3,y3;x4,y4"]
      自动检测文档最大四边形做透视矫正（方案C：默认自动；传 --pts 则用手动四点修正），
      叠加白平衡（灰度世界），输出矫正图。
      stdout: {"ok":true,"w":..,"h":..,"auto":true|false}
              {"error":"..."} 失败

  boxes --in <矫正图> --boxes <boxes.json> --out <out.json>
      boxes.json: [{"idx":0,"x":..,"y":..,"w":..,"h":..}, ...]（相对矫正图像素坐标）
      对每个框裁剪 → 放大 → RapidOCR 识别 → out.json: [{"idx":0,"text":"..."}]
      stdout: {"ok":true,"count":N} / {"error":"..."}
"""
import os, sys, json, argparse
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import cv2
    import numpy as np
except Exception as e:
    print(json.dumps({"error": "依赖不可用: %s" % e}, ensure_ascii=False)); sys.exit(1)

MAX_DIM = 2400  # 矫正前缩放上限，避免超大照片过慢


def _load(path):
    img = cv2.imread(path)
    if img is None:
        raise RuntimeError("无法读取图片: %s" % path)
    h, w = img.shape[:2]
    s = max(h, w)
    if s > MAX_DIM:
        k = MAX_DIM / float(s)
        img = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
    return img


def _order_points(pts):
    """四角点排序：左上、右上、右下、左下（按 x+y 与 x-y 判定）。"""
    pts = np.array(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).flatten()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(diff)]
    bl = pts[np.argmax(diff)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def _auto_detect_quad(gray):
    """自动检测文档最大四边形（白底文档边缘）。返回排序后的四点或 None。"""
    # 自适应阈值 + 形态学闭合，突出文档边界
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY_INV, 15, 15)
    kernel = np.ones((5, 5), np.uint8)
    closed = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    h, w = gray.shape[:2]
    total = float(h * w)
    for c in contours[:8]:
        area = cv2.contourArea(c)
        if area < total * 0.15:          # 至少占图 15%
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4:
            return _order_points(approx.reshape(4, 2))
    return None


def _white_balance(img):
    """灰度世界白平衡（BGR）。"""
    b, g, r = cv2.split(img.astype(np.float32))
    mb, mg, mr = b.mean(), g.mean(), r.mean()
    avg = (mb + mg + mr) / 3.0
    sb = min(avg / mb, 3.0) if mb > 1 else 1.0
    sg = min(avg / mg, 3.0) if mg > 1 else 1.0
    sr = min(avg / mr, 3.0) if mr > 1 else 1.0
    out = cv2.merge([np.clip(b * sb, 0, 255), np.clip(g * sg, 0, 255), np.clip(r * sr, 0, 255)])
    return out.astype(np.uint8)


def cmd_correct(args):
    img = _load(args.inp)
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    auto_used = True
    quad = None
    if args.pts:
        try:
            pts = [list(map(float, p.split(","))) for p in args.pts.split(";")]
            if len(pts) != 4:
                raise ValueError("pts 需要 4 个点")
            quad = _order_points(pts)
            auto_used = False
        except Exception as e:
            print(json.dumps({"error": "手动四点解析失败: %s" % e}, ensure_ascii=False)); sys.exit(1)
    else:
        quad = _auto_detect_quad(gray)

    if quad is None:
        # 未检测到：整图作为四边形
        quad = _order_points([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])

    (tl, tr, br, bl) = quad
    w1 = np.linalg.norm(tr - tl); w2 = np.linalg.norm(br - bl)
    h1 = np.linalg.norm(bl - tl); h2 = np.linalg.norm(br - tr)
    out_w = int(round(max(w1, w2)))
    out_h = int(round(max(h1, h2)))
    if out_w < 10 or out_h < 10:
        print(json.dumps({"error": "矫正区域过小"}, ensure_ascii=False)); sys.exit(1)

    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(quad, dst)
    warped = cv2.warpPerspective(img, M, (out_w, out_h))

    # 白平衡
    balanced = _white_balance(warped)

    ok = cv2.imwrite(args.out, balanced, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        print(json.dumps({"error": "无法写入矫正图: %s" % args.out}, ensure_ascii=False)); sys.exit(1)
    print(json.dumps({"ok": True, "w": out_w, "h": out_h, "auto": auto_used}, ensure_ascii=False))


# ---------------- OCR 引擎（RapidOCR，惰性加载） ----------------
_engine = None
def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _ocr_region(img, x, y, w, h):
    """裁剪 + 放大 + RapidOCR，返回文本（每行 \n 连接）。失败返回 ''。"""
    H, W = img.shape[:2]
    x0 = max(0, int(x)); y0 = max(0, int(y))
    x1 = min(W, int(x + w)); y1 = min(H, int(y + h))
    if x1 <= x0 or y1 <= y0:
        return ""
    crop = img[y0:y1, x0:x1]
    # 小区域放大 2 倍提升识别率
    cw, ch = crop.shape[1], crop.shape[0]
    if max(cw, ch) < 800:
        crop = cv2.resize(crop, (cw * 2, ch * 2), interpolation=cv2.INTER_CUBIC)
    try:
        result, _ = _get_engine()(crop)
    except Exception:
        return ""
    if not result:
        return ""
    lines = []
    for item in result:
        if len(item) >= 2:
            t = str(item[1] or "").strip()
            if t:
                lines.append(t)
    return "\n".join(lines)


def cmd_boxes(args):
    img = _load(args.inp)
    with open(args.boxes, "r", encoding="utf-8") as f:
        boxes = json.load(f)
    if not isinstance(boxes, list):
        print(json.dumps({"error": "boxes 需要是数组"}, ensure_ascii=False)); sys.exit(1)

    out = []
    for b in boxes:
        idx = b.get("idx", 0)
        text = _ocr_region(img, b.get("x", 0), b.get("y", 0), b.get("w", 0), b.get("h", 0))
        out.append({"idx": idx, "text": text})

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(json.dumps({"ok": True, "count": len(out)}, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: inventory_ocr.py correct|boxes ..."}, ensure_ascii=False)); sys.exit(2)
    cmd = sys.argv[1]
    p = argparse.ArgumentParser(prog="inventory_ocr.py " + cmd)
    p.add_argument("--in", dest="inp", required=True)
    if cmd == "correct":
        p.add_argument("--out", dest="out", required=True)
        p.add_argument("--pts", dest="pts", default="")
        a = p.parse_args(sys.argv[2:])
        cmd_correct(a)
    elif cmd == "boxes":
        p.add_argument("--boxes", dest="boxes", required=True)
        p.add_argument("--out", dest="out", required=True)
        a = p.parse_args(sys.argv[2:])
        cmd_boxes(a)
    else:
        print(json.dumps({"error": "未知命令: %s" % cmd}, ensure_ascii=False)); sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as ex:
        print(json.dumps({"error": "异常: %s" % ex}, ensure_ascii=False)); sys.exit(1)
