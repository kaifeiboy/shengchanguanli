# -*- coding: utf-8 -*-
"""
geo_score.py —— 图像匹配「几何辅助」腿（Track 1：OCR 为主 + 几何辅助）
=====================================================================
给定一张手机照 + 若干图纸切块图，返回每张切块相对照片的几何/表观分。
供 DrawingService.MatchBlock 在 OCR 已有弱信号时做「几何确认/加分」。

算法（与 bench_match.py 同源，已验证）：
  照片 → 灰度/CLAHE → SIFT 特征
  切块 → 同上 → FLANN/KDTree 索引
  1:N 匹配 → Lowe ratio 过滤 → RANSAC 单应变换 →
      geo = inlier 占比（主杠杆，跨域亦可用）
      app = 对齐后 NCC 表观相似（CAD 线框↔实景 跨域时趋零，仅作辅证）

输出：JSON 数组，元素与输入切块顺序一一对应：
  [{"idx":0,"geo":0.67,"app":0.00,"fused":0.40}, ...]
  idx 即命令行中切块参数的位置下标（MatchBlock 据此对齐 BIdx）。

设计要点（对齐「忽略规则不变」）：
  - 仅做几何/表观比对，不读 OCR 文本，与 OCR 主链路正交、互不污染；
  - 任何异常 → 返回该块 {"geo":0,"app":0,"fused":0,"missing":true}，
    绝不抛错中断上级调用。
"""
import os, sys, json, math
import cv2
import numpy as np
from PIL import Image

MATCHER = "sift"
MAX_DIM = 1100
CLAHE = True


def load_gray(path):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_DIM:
        s = MAX_DIM / max(w, h)
        img = img.resize((int(w * s), int(h * s)))
    arr = np.asarray(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if CLAHE:
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    return gray


def make_matcher():
    if MATCHER == "sift":
        return cv2.SIFT_create(), True
    return cv2.ORB_create(nfeatures=2000), False


def extract(gray, alg, is_float):
    kp, desc = alg.detectAndCompute(gray, None)
    if desc is None:
        return kp, np.array([])
    if not is_float and desc.dtype != np.uint8:
        desc = desc.astype(np.uint8)
    return kp, desc


def build_index(is_float):
    if is_float:
        return cv2.FlannBasedMatcher(dict(algorithm=1, trees=5), dict(checks=60))
    return cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)


def ncc(a, b):
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    a -= a.mean()
    b -= b.mean()
    den = math.sqrt((a * a).sum() * (b * b).sum())
    if den < 1e-6:
        return 0.0
    return float((a * b).sum() / den)


def score_against(photo_gray, kp_p, desc_p, blk_gray, blk_kp, blk_desc, matcher):
    """返回 (fused, geo, app)。"""
    if desc_p.size == 0 or blk_desc.size == 0:
        return 0.0, 0.0, 0.0
    try:
        matches = matcher.knnMatch(desc_p, blk_desc, k=2)
    except Exception:
        matches = []
    good = []
    for m in matches:
        if len(m) == 2:
            if m[0].distance < 0.75 * m[1].distance:
                good.append(m[0])
        elif len(m) == 1:
            good.append(m[0])
    if len(good) < 4:
        return 0.0, 0.0, 0.0
    pts_p = np.float32([kp_p[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    pts_b = np.float32([blk_kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(pts_p, pts_b, cv2.RANSAC, 5.0)
    if H is None or mask is None:
        return 0.0, 0.0, 0.0
    inliers = int(mask.sum())
    inlier_ratio = inliers / len(good)
    bh, bw = blk_gray.shape
    try:
        warped = cv2.warpPerspective(photo_gray, H, (bw, bh))
        app = ncc(warped, blk_gray)
    except Exception:
        app = 0.0
    app = max(0.0, min(1.0, app))
    fused = 0.6 * inlier_ratio + 0.4 * app
    return float(fused), float(inlier_ratio), float(app)


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: geo_score.py <photo> <blk0.png> [blk1.png ...]\n")
        sys.exit(2)

    photo_path = sys.argv[1]
    blk_paths = sys.argv[2:]
    if not os.path.exists(photo_path):
        print(json.dumps([{"idx": i, "geo": 0.0, "app": 0.0, "fused": 0.0, "missing": True}
                         for i in range(len(blk_paths))], ensure_ascii=False))
        sys.exit(0)

    photo_gray = load_gray(photo_path)
    alg, is_float = make_matcher()
    kp_p, desc_p = extract(photo_gray, alg, is_float)
    fm = build_index(is_float)

    out = []
    for i, p in enumerate(blk_paths):
        if not os.path.exists(p):
            out.append({"idx": i, "geo": 0.0, "app": 0.0, "fused": 0.0, "missing": True})
            continue
        blk_gray = load_gray(p)
        blk_kp, blk_desc = extract(blk_gray, alg, is_float)
        fused, geo, app = score_against(photo_gray, kp_p, desc_p, blk_gray, blk_kp, blk_desc, fm)
        out.append({"idx": i, "geo": round(geo, 4), "app": round(app, 4), "fused": round(fused, 4)})
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
