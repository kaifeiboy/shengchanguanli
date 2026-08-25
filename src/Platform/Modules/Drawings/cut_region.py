#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cut_region.py — 按手动圈定区域裁剪 + OCR（命名块）
==============================================
用途：打标首件对比「定义阶段」的人工圈定裁剪。
  给定图纸 PDF + 一个或多个矩形区域(x,y,w,h, @dpi)，
  把每个区域从渲染图上裁下 → 复用 ocr_cli 的预处理/OCR 流水线
  → 取最优候选文本作为「块名称」→ 输出 JSON + 裁剪 PNG。

坐标空间：与 segment_blocks.py 一致，均为「渲染 dpi 下的像素」。
  默认 dpi=200（必须与 segment 写入 drawing_blocks 的 X/Y/W/H 同空间）。

用法（CLI）：
  cut_region.py <pdf> <out_dir> [--dpi 200] \
      --region x,y,w,h [--name "打标规格"] [--region ...]

  也可一次性传多个 --region，每个会生成独立命名块。

作为模块被 C# 调用时：
  from cut_region import cut_regions
  res = cut_regions(pdf, out_dir, dpi, [(x,y,w,h,"name"), ...])
  # res: [{ "file","x","y","w","h","tokens","rawText","name","bestScore" }, ...]
"""
import sys
import os
import json
import numpy as np
from PIL import Image
import fitz  # PyMuPDF

# 同目录复用 ocr_cli 的预处理 / OCR / 评分
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ocr_cli


def render_page_rgb(pdf_path, dpi=200):
    """把 PDF 首页渲染成 RGB numpy 数组（不改动原始 PDF）。"""
    doc = fitz.open(pdf_path)
    try:
        page = doc[0]
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=mat)
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        arr = arr.reshape(pix.height, pix.width, pix.n)
        return arr[:, :, :3].copy()
    finally:
        doc.close()


def _ocr_region(rgb_region):
    """对一个裁剪出的 RGB 区域做 OCR，返回最优候选文本与评分。"""
    gray = rgb_region.mean(axis=2).astype(np.uint8)
    cands = ocr_cli.preprocess_block(gray)  # [(name, bin_arr, psm), ...]
    best_text = ""
    best_score = -1
    for cname, bin_arr, psm in cands:
        try:
            txt = ocr_cli.run_tess(bin_arr, psm)
        except Exception:
            continue
        txt = (txt or "").strip()
        if not txt:
            continue
        sc = ocr_cli.score_text(txt)
        if sc > best_score:
            best_score = sc
            best_text = txt
    # token 列表：简单空白+标点切分，过滤空串
    toks = [t for t in " ".join(best_text.split()).split() if t]
    return best_text, toks, best_score


def cut_one(rgb_page, out_dir, dpi, x, y, w, h, name_hint=""):
    """裁剪单个区域并 OCR，落盘 PNG + 返回元数据 dict。"""
    H, W = rgb_page.shape[:2]
    # 轻微外扩 4px，避免贴边裁掉笔画（人工圈定通常已留白，这里仅保险）
    pad = 4
    x0 = max(0, int(x) - pad)
    y0 = max(0, int(y) - pad)
    x1 = min(W, int(x) + int(w) + pad)
    y1 = min(H, int(y) + int(h) + pad)
    region = rgb_page[y0:y1, x0:x1]
    if region.size == 0:
        return None
    # 文件名：用坐标哈希避免重名
    fname = f"region_{int(x)}_{int(y)}_{int(w)}_{int(h)}.png"
    fpath = os.path.join(out_dir, fname)
    Image.fromarray(region).save(fpath)
    raw, toks, score = _ocr_region(region)
    # 块名称：优先用用户给定 name；否则用 OCR 首行 + 关键 token 拼成可读名
    if name_hint:
        name = name_hint
    else:
        name = (raw.replace("\n", " ")[:60]).strip()
    return {
        "file": fname,
        "x": int(x), "y": int(y), "w": int(w), "h": int(h),
        "tokens": toks,
        "rawText": raw,
        "name": name,
        "bestScore": round(score, 2),
    }


def cut_regions(pdf_path, out_dir, dpi, regions):
    """
    regions: list of (x, y, w, h, name_hint)
    返回 list[dict]
    """
    os.makedirs(out_dir, exist_ok=True)
    rgb = render_page_rgb(pdf_path, dpi)
    results = []
    for (x, y, w, h, nh) in regions:
        r = cut_one(rgb, out_dir, dpi, x, y, w, h, nh or "")
        if r:
            results.append(r)
    return results


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("out_dir")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--region", action="append", required=True,
                    help="x,y,w,h  (可多次)；可选带名称用 'x,y,w,h,名称'")
    args = ap.parse_args()

    regions = []
    for r in args.region:
        parts = [p.strip() for p in r.split(",")]
        if len(parts) >= 4:
            x, y, w, h = (int(float(parts[0])), int(float(parts[1])),
                          int(float(parts[2])), int(float(parts[3])))
            name = ",".join(parts[4:]) if len(parts) > 4 else ""
            regions.append((x, y, w, h, name))

    res = cut_regions(args.pdf, args.out_dir, args.dpi, regions)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
