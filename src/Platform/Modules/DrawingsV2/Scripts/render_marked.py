#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_marked.py — 把一次比对的「已标示照片」渲染成图片（V2 匹配记录用）。

输入：
  --photo    <原始照片绝对路径>
  --response <本次比对完整响应 JSON 文件路径>（含 blockMatch）
  --out      <输出图片路径，jpg/png 由扩展名决定>
  --max-side <int>  最长边像素；0=保持原尺寸（full），>0=缩略图

规则（与 H5 drawPhotoAnnotations 新引擎完全一致，单一事实源）：
  - 仅取 blockMatch.enabled 且存在 top 的记录；
  - 候选 = top.elements + extra；
  - 跳过 photoNorm 缺失（L3 无照片坐标）的元素；
  - 跳过 kind 为 qr / icon（图纸与照片两侧均保留，无需额外标示框，2026-09-29）；
  - 四色：green=#16a34a / red=#dc2626 / yellow=#d97706 / gray=#888780
    （cv2 用 BGR：绿(74,163,22) 红(38,38,220) 黄(6,119,217) 灰(128,135,136)）；
  - 线宽 = max(2, 画布宽/400)，与前端一致。
老记录（无 blockMatch / response 非契约）仅原样缩放输出，不画框。
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np

# cv2 用 BGR
COLORS = {
    "green":  (74, 163, 22),
    "red":    (38, 38, 220),
    "yellow": (6, 119, 217),
    "gray":   (128, 135, 136),
}
DEFAULT_COLOR = (128, 135, 136)
SKIP_KINDS = ("qr", "icon")


def _collect_items(response):
    bm = response.get("blockMatch") if isinstance(response, dict) else None
    if not isinstance(bm, dict) or not bm.get("enabled") or not bm.get("top"):
        return []
    items = []
    top = bm.get("top") or {}
    items.extend(top.get("elements") or [])
    items.extend(bm.get("extra") or [])
    return items


def render(photo_path, response_path, out_path, max_side):
    img = cv2.imread(photo_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit(f"无法读取照片：{photo_path}")
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    # 载入响应
    response = {}
    try:
        with open(response_path, "r", encoding="utf-8") as f:
            response = json.load(f) or {}
    except Exception as e:
        print(f"[render_marked] response 读取失败，仅输出原图：{e}", file=sys.stderr)

    ih, iw = img.shape[:2]
    # 先按 max_side 缩放（框线宽基于缩放后尺寸计算，与前端一致）
    if max_side and max_side > 0:
        scale = min(1.0, max_side / max(float(iw), float(ih)))
        if scale < 1.0:
            nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
            ih, iw = img.shape[:2]

    lw = max(2, iw // 400)

    for e in _collect_items(response):
        if not isinstance(e, dict):
            continue
        pn = e.get("photoNorm")
        if not isinstance(pn, (list, tuple)) or len(pn) < 4:
            continue  # L3：无照片坐标，不画框
        if e.get("kind") in SKIP_KINDS:
            continue  # qr/icon 两侧保留、无需额外标示框
        color = COLORS.get(e.get("color"), DEFAULT_COLOR)
        try:
            x, y, w, h = (float(v) for v in pn)
        except Exception:
            continue
        px = int(round(x * iw)); py = int(round(y * ih))
        pw = int(round(w * iw)); ph = int(round(h * ih))
        if pw <= 0 or ph <= 0:
            continue
        cv2.rectangle(img, (px, py), (px + pw, py + ph), color, lw)

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    ok, buf = cv2.imencode(os.path.splitext(out_path)[1].lower() or ".jpg", img,
                           [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        raise SystemExit(f"图像编码失败：{out_path}")
    with open(out_path, "wb") as f:
        f.write(buf)
    print(f"[render_marked] ok {out_path} {iw}x{ih}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", required=True)
    ap.add_argument("--response", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-side", type=int, default=0)
    args = ap.parse_args()
    render(args.photo, args.response, args.out, args.max_side)


if __name__ == "__main__":
    main()
