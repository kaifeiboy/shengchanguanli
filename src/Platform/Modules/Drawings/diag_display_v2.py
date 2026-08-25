#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LCD 屏显检测 v2 几何面板标定脚本（只读测量，不改动生产）。
对每块图 OCR 提名 LCD 形态文本，测量其深色面板几何（面积/宽高比/面板-文本余量比），
用于设置 _detect_display_regions v2 的阈值，根除 did115 误报且保留 did112 真屏显。

用法: python diag_display_v2.py [did ...]   （不传则扫描全部 99 块）
"""
import os, sys, cv2, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import photo_registration as PR
from diff_visualizer import _norm_text, _bbox_rect

SEG = r"E:\workaaa\shengchanguanli\data\seg"
DARK_THR = 100          # 灰值<该值视为"深色面板/标签"像素
PANEL_DARK_FRAC = 0.30  # 局部"真深色"像素占比下限（面板/标签底色）
PANEL_KERNEL = 5        # 膨胀核，桥接同屏数字间隙
PANEL_MIN_AREA = 2500   # 深色面板连通域最小面积（屏显>>印刷暗标签）
PANEL_MIN_AR = 1.5      # 面板宽高比下限（屏显偏宽）
MIN_LEN = 2
LCD_SCORE_MIN = 0.80
ALPHA_RATIO_MAX = 0.15


def is_lcd_form(t):
    n = _norm_text(t)
    if len(n) < MIN_LEN:
        return False
    if any('\u4e00' <= ch <= '\u9fff' for ch in n):
        return False
    total = max(len(n), 1)
    letters = sum(1 for ch in n if ch.isalpha())
    if letters / total > ALPHA_RATIO_MAX:
        return False
    sym = sum(1 for ch in n if ch.isdigit() or ch in ':.-/ ')
    return (sym / total) >= LCD_SCORE_MIN


def main():
    dids = sys.argv[1:]
    rows = []
    # 收集块图
    targets = []
    for d in sorted(os.listdir(SEG)):
        dp = os.path.join(SEG, d)
        if not os.path.isdir(dp) or not d.isdigit():
            continue
        if dids and d not in dids:
            continue
        bdir = os.path.join(dp, "blocks")
        if not os.path.isdir(bdir):
            continue
        for fn in sorted(os.listdir(bdir)):
            if fn.endswith(".png") and "_blk_" in fn:
                targets.append((d, os.path.join(bdir, fn)))

    print(f"# blocks to scan: {len(targets)}")
    for did, png in targets:
        try:
            lines = PR.ocr_photo_tiled(png)
        except Exception as e:
            print(f"!! OCR fail {did} {os.path.basename(png)}: {e}")
            continue
        g = cv2.cvtColor(cv2.imread(png), cv2.COLOR_BGR2GRAY)
        h, w = g.shape
        _, dark = cv2.threshold(g, DARK_THR, 255, cv2.THRESH_BINARY_INV)  # 深=255
        for ln in lines:
            bb = _bbox_rect(ln["bbox"]) if ln.get("bbox") else None
            if not bb:
                continue
            t = ln.get("text", "")
            if not is_lcd_form(t):
                continue
            x, y, bw, bh = bb
            cx, cy = x + bw / 2.0, y + bh / 2.0
            # v1 dark-ratio (non-white in +4px)
            ex, ey = max(0, x - 4), max(0, y - 4)
            ebw, ebh = min(w - ex, bw + 8), min(h - ey, bh + 8)
            roi = g[ey:ey + ebh, ex:ex + ebw]
            v1 = (1.0 - (roi == 255).mean()) if roi.size else 0.0
            tb_area = max(bw * bh, 1)
            rec = {"did": did, "blk": os.path.basename(png), "text": t,
                   "bbox": [x, y, bw, bh], "v1_nonwhite": round(float(v1), 3)}
            for K in (3, 5, 7, 9):
                kern = np.ones((K, K), np.uint8)
                dd = cv2.dilate(dark, kern, iterations=1)
                num, _, stats, _ = cv2.connectedComponentsWithStats(dd, 8)
                best = None
                for ci in range(1, num):
                    sx, sy, sw, sh, sa = stats[ci]
                    if sa < 50:
                        continue
                    if sx <= cx < sx + sw and sy <= cy < sy + sh:
                        best = (sa, sw, sh)
                        break
                if best:
                    sa, sw, sh = best
                    rec[f"K{K}_area"] = int(sa)
                    rec[f"K{K}_ar"] = round(sw / max(sh, 1), 2)
                    rec[f"K{K}_ratio"] = round(sa / tb_area, 2)
                else:
                    rec[f"K{K}_area"] = 0
            # v2 局部"真深色"占比（面板/标签底色），expanded 6px
            ex2, ey2 = max(0, x - 6), max(0, y - 6)
            ebw2, ebh2 = min(w - ex2, bw + 12), min(h - ey2, bh + 12)
            roi2 = g[ey2:ey2 + ebh2, ex2:ex2 + ebw2]
            dark_frac = float((roi2 < DARK_THR).mean()) if roi2.size else 0.0
            k5_area = rec.get("K5_area", 0)
            k5_ar = rec.get("K5_ar", 0)
            v2_pass = (dark_frac >= PANEL_DARK_FRAC) and (k5_area >= PANEL_MIN_AREA) and (k5_ar >= PANEL_MIN_AR)
            rec["dark_frac"] = round(dark_frac, 3)
            rec["v2_pass"] = bool(v2_pass)
            rows.append(rec)
            print(json.dumps(rec, ensure_ascii=False))
    # 汇总
    print(f"\n# total LCD-form candidates: {len(rows)}")


if __name__ == "__main__":
    main()
