# -*- coding: utf-8 -*-
"""解析式推算：各阈值下 _verify_text 真正喂给 OCR 的 im 尺寸（不跑 OCR）。"""
import json, os, sys
import cv2, numpy as np

SCRIPTS = r"E:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2\Scripts"
sys.path.insert(0, SCRIPTS)
from observe import verify as V, geometry as G  # noqa: E402

recs = json.load(open(r"E:\workaaa\shengchanguanli\data\_exp\textsize_calib.json", encoding="utf-8"))
# 复用标定里的照片集合
import sqlite3
DB = r"E:\workaaa\shengchanguanli\data\drawingsv2.db"
con = sqlite3.connect(DB); con.execute("PRAGMA journal_mode=WAL")
rows = con.execute("SELECT photo_path, verdicts_json FROM v2_compare_records WHERE usable=1 "
                   "ORDER BY id DESC LIMIT 400").fetchall(); con.close()
path_of = {}
for p, vj in rows:
    if p and os.path.exists(p):
        path_of[os.path.basename(p)] = (p, json.loads(vj or "[]"))

seen, shapes = set(), []
for r in recs:
    key = r["photo"]
    if key in seen:
        continue
    seen.add(key)
    p, vs = path_of[key]
    bgr = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
    warped, _ = G.normalize(bgr, do_warp=True)
    for ri, v in enumerate([x for x in vs if x.get("expectedBbox")][:30]):
        crop, _ = V._crop_margin(warped, [float(x) for x in v["expectedBbox"]])
        if crop.size == 0:
            continue
        ch, cw = crop.shape[:2]
        pad = max(V.TEXT_PAD_MIN, int(max(ch, cw) * V.TEXT_PAD_RATIO))
        ch2, cw2 = ch + 2 * pad, cw + 2 * pad
        row = {"k": f"{key[:10]}r{ri}", "crop": (ch, cw), "canvas": (ch2, cw2), "im": {}}
        for t in (0, 1024, 1280, 1600):
            rs = 1.0
            H, W = ch2, cw2
            if t > 0 and max(H, W) > t:
                rs = t / float(max(H, W))
                H, W = int(round(H * rs)), int(round(W * rs))
            s = 1.0
            if min(H, W) < V.TEXT_MIN_SIDE:
                s = min(V.TEXT_UPSCALE, V.TEXT_MIN_SIDE / max(min(H, W), 1))
            row["im"][str(t)] = (int(round(H * s)), int(round(W * s)), round(s, 2))
        shapes.append(row)

big = [x for x in shapes if max(x["canvas"]) > 1024]
print(f"总 region {len(shapes)}，其中大 canvas {len(big)}\n")
print(f"{'region':14} {'crop(h,w)':>14} {'canvas':>14} | {'0':>12} {'1024':>12} {'1280':>12} {'1600':>12}")
for x in big:
    def f(t):
        h, w, s = x["im"][str(t)]
        return f"{h}x{w}{'*' if s > 1 else ''}"
    print(f"{x['k']:14} {str(x['crop']):>14} {str(x['canvas']):>14} | "
          f"{f(0):>12} {f(1024):>12} {f(1280):>12} {f(1600):>12}")
print("\n(* 表示触发了短边<96 的放大 s>1；此时旧 bbox 公式会出错)")
n_both = sum(1 for x in big for t in (1024, 1280, 1600) if x["im"][str(t)][2] > 1)
print(f"大 canvas region 中，存在「缩放且放大」组合的条数：{n_both}/{len(big)*3}")
