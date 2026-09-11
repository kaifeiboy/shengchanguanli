# -*- coding: utf-8 -*-
"""
超时根因诊断 4：按【生产真实尺寸】测量。
MatchBlock 会先把照片缩到最大边 960px 再做 OCR / diff，故必须用 960 版本测。
"""
import sys, os, time
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SRC = r"E:\workaaa\shengchanguanli\data\seg\116\_user_vq_photo.jpg"
P960 = r"E:\workaaa\shengchanguanli\data\seg\116\_diag_photo_960.jpg"
BLOCK = r"E:\workaaa\shengchanguanli\data\seg\116\blocks\116_blk_04.png"
OUT = r"E:\workaaa\shengchanguanli\data\seg\116\_prod_out.jpg"

im = Image.open(SRC).convert("RGB")
w, h = im.size
s = 960.0 / max(w, h)
if s < 1.0:
    im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
im.save(P960, quality=92)
print(f"[resize] 原图 {(w,h)} -> 生产尺寸 {im.size}", flush=True)

import ocr_cli
import diff_visualizer as DV

calls = []
_orig = ocr_cli._rapidocr_raw


def patched(p):
    t = time.time()
    r = _orig(p)
    calls.append((os.path.basename(str(p)), time.time() - t))
    return r


ocr_cli._rapidocr_raw = patched

# ---- 生产步骤1：_ocr.Recognize（worker -> ocr_cli）----
t0 = time.time()
roi = ocr_cli._auto_roi(P960)
t_ocr = time.time() - t0
print(f"\n[步骤1 OCR _auto_roi @960] {t_ocr:.2f}s", flush=True)
for n, d in calls:
    print(f"      - {n}: {d:.2f}s", flush=True)
print(f"      roi={repr((roi or '')[:80])}", flush=True)

# ---- 生产步骤2：ComputeDiffFromPath（fresh 子进程 -> diff_visualizer.run）----
t0 = time.time()
r = DV.run(P960, BLOCK, OUT,
           block_bbox_cache=None,
           block_text_override=None,
           photo_text_override=roi,
           icon_regions_override=None)
t_diff = time.time() - t0
print(f"\n[步骤2 diff run() @960] {t_diff:.2f}s", flush=True)

print(f"\n===== 生产链路合计 @960 =====", flush=True)
print(f"OCR: {t_ocr:.2f}s  +  diff: {t_diff:.2f}s  =  {t_ocr + t_diff:.2f}s", flush=True)
print(f"前端 abort 阈值: 45s  ->  {'❌ 超限' if (t_ocr + t_diff) > 45 else '✅ 未超限'}", flush=True)
