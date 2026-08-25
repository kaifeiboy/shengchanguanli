#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
segment_blocks_auto.py - 图纸切图【auto-paddlev3 自动识别图块】
=========================================================

目标：
  提供「自动识别」图纸上需要匹配的图块（打标标识区），无需人工圈红框。
  旧「红框/标记」人工圈定规则已彻底移除（见 segment_blocks.py 历史），
  本文件承载现行 auto-paddlev3 规则的自动识别入口。

设计约束：
  1) 与 C# EnsureSegmentedCore 的 manifest 契约保持完全一致：
       {drawingId, source, width, height, blocks:[{idx,x,y,w,h,file,type}], algorithm, mode}
     C# 后续处理（MinBlock 过滤 / BatchOcr / KeepViewBlock / 入库）一律不动。
  2) 仅做「舍弃」不做「合并」：auto 路径的图块分离完全由 pp_doclayout_onnx.detect()
     内部的 anti-merge（过度覆盖舍弃 + 子检测冗余舍弃）完成，本文件不再做任何合并。

入口：
  detect_auto_regions() —— 调 PP-DocLayoutV3 ONNX（pp_doclayout_onnx.detect），
    返回 [(x0,y0,x1,y1), ...]；无有效块/模型不可用时返回 None。
"""

import sys
import os
import json

import numpy as np
from PIL import Image

# 复用旧规则的共享实现（旧文件已备份、稳定，不会变动）
try:
    import segment_blocks as sb
except Exception:  # 兜底：同目录直接 import
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import segment_blocks as sb

DPI = 200  # 与 C# 调用保持一致（EnsureSegmentedCore 传 200）


def norm_path(p):
    return sb.norm_path(p)


# ────────────────────────────────────────────────────────────────────────
# 【新自动识别算法实现】—— PaddleOCR 版面检测（PP-DocLayoutV3）
# ────────────────────────────────────────────────────────────────────────
# 关键认知（2026-07-23，对比用户提供的 PaddleOCR-VL-1.6 JSON 校正）：
#   旧路线用 PP-DocLayout_plus-L（对 CAD 线框视图「盲」）退化为轮廓检测，是
#   错误方案。真正的 PaddleOCR 切图能力来自 PP-DocLayoutV3 视觉语言版面模型——
#   它能把工程图里的各部件视图直接标成 label="image" 的块，与用户 JSON 的
#   image 块 1:1 对齐。本函数即调用该模型取 image 块，再叠加舍弃规则滤掉
#   表格/文字/标题及少数低置信碎块，得到干净的匹配候选图。

# 保留的版式标签：视觉内容块（部件图/图/表/示意图）
KEEP_LABELS = {"image", "figure", "chart", "diagram"}

# 舍弃规则阈值
MIN_SCORE = 0.60        # 低于此置信度的块视为噪点（如 0.57 的小标注块）
MIN_AREA_RATIO = 0.003  # 面积占整页 < 0.3% 的极小碎块丢弃
MAX_AREA_RATIO = 0.40   # 面积占整页 > 40% 视为整页轮廓误检，丢弃
MAX_ASPECT_RATIO = 10.0 # 长边/短边 > 10 的极端扁长/细高块丢弃（非工程部件视图，
                        # 如 HSXC 的 1068×76=14:1 过渡条）。正常部件视图宽高比不会超 10:1。

# 说明：本机 CPU 无 AVX2，Paddle Inference 预测器必崩。故自动切图改用
# PP-DocLayoutV3 的 ONNX（同一 PaddleOCR 模型、经 Paddle2ONNX 转换）由
# onnxruntime 推理，绕开 AVX2。详见 pp_doclayout_onnx.py。
_LAYOUT_MODEL = None  # 保留占位，避免旧引用报错


def detect_auto_regions(pdf_path, color, gray, W, H, dpi):
    """自动识别图纸上「需要匹配的图块」区域（PP-DocLayoutV3 ONNX + 舍弃规则）。

    返回 [(x0,y0,x1,y1), ...]（渲染像素坐标）；无有效块或模型不可用时返回
    None，由调用方走旧 v6 兜底。

    坐标空间：color 即 dpi=200 渲染全页（尺寸 W,H），ONNX 输出已映射回该空间，
    与下游 segment() 的裁切一致。
    """
    try:
        import pp_doclayout_onnx as pp
        if not pp.is_available():
            sys.stderr.write("[auto] PP-DocLayoutV3 ONNX 不可用，fallback v6\n")
            return None
        regions = pp.detect(color, W, H)
        return regions
    except Exception as e:
        sys.stderr.write(f"[auto] onnx detect error, fallback v6: {e}\n")
        return None


# ────────────────────────────────────────────────────────────────────────
# 裁剪 + 写出 manifest（与 v6 的 segment() 产出完全一致）
# ────────────────────────────────────────────────────────────────────────
def _crop_and_write(color, regions, did, out_dir, dpi, mode):
    blocks_dir = os.path.join(out_dir, "blocks")
    os.makedirs(blocks_dir, exist_ok=True)

    pending = []
    for r in regions:
        x0, y0, x1, y1 = [int(v) for v in r]
        pending.append({
            "box": (x0, y0, x1, y1),
            "x": x0, "y": y0,
            "w": x1 - x0, "h": y1 - y0,
        })
    # 排序：从上到下、从左到右
    pending.sort(key=lambda b: (b["y"], b["x"]))

    blocks = []
    for k, pb in enumerate(pending):
        crop = color.crop(pb["box"])
        bname = f"{did}_blk_{k:02d}.png"
        crop.save(os.path.join(blocks_dir, bname))
        blocks.append({
            "idx": k,
            "x": pb["x"], "y": pb["y"],
            "w": pb["w"], "h": pb["h"],
            "file": f"blocks/{bname}",
            "type": "engineering",
        })

    manifest = {
        "drawingId": int(did),
        "source": os.path.basename(os.path.join(out_dir, f"{did}_full.png")),
        "width": int(color.width),
        "height": int(color.height),
        "blocks": blocks,
        "algorithm": "v7-auto-paddlev3",
        "mode": mode,
    }
    json_path = os.path.join(blocks_dir, f"{did}_blocks.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def segment_auto(pdf_path, did, out_dir, dpi=DPI):
    """auto-paddlev3 主入口：自动识别图块 → 裁剪 → 写 manifest。

    若 detect_auto_regions 返回区域，用 auto 规则产出（mode=auto）；
    否则直接复用 segment_blocks.segment()（同为 auto-paddlev3，无红框兜底）。
    """
    os.makedirs(out_dir, exist_ok=True)
    full_png = os.path.join(out_dir, f"{did}_full.png")
    color, W, H = sb.render_page(pdf_path, full_png, dpi)
    gray = np.asarray(color.convert("L"))

    # 1) 新自动识别
    regions = detect_auto_regions(pdf_path, color, gray, W, H, dpi)
    if regions:
        return _crop_and_write(color, regions, did, out_dir, dpi, mode="auto")

    # 2) 回退：复用 segment_blocks.segment()（同为 auto-paddlev3，无红框兜底）
    man = sb.segment(pdf_path, did, out_dir, dpi)
    man["algorithm"] = "v7-auto"
    blocks_dir = os.path.join(out_dir, "blocks")
    json_path = os.path.join(blocks_dir, f"{did}_blocks.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    return man


def main():
    if len(sys.argv) < 4:
        print(json.dumps({
            "ok": False,
            "error": ("usage: segment_blocks_auto.py <render|segment> "
                      "<pdf> <did> <out_dir> [dpi]")
        }))
        sys.exit(2)

    cmd = sys.argv[1]
    pdf = norm_path(sys.argv[2])
    did = sys.argv[3]
    out_dir = norm_path(sys.argv[4])
    dpi = int(sys.argv[5]) if len(sys.argv) > 5 else DPI

    if cmd == "render":
        sb.render_page(pdf, os.path.join(out_dir, f"{did}_full.png"), dpi)
        print(json.dumps({"ok": True, "full": f"{did}_full.png"}))
    elif cmd == "segment":
        manifest = segment_auto(pdf, did, out_dir, dpi)
        print(json.dumps(manifest))
    else:
        print(json.dumps({"ok": False, "error": "unknown cmd: " + cmd}))
        sys.exit(2)


if __name__ == "__main__":
    main()
