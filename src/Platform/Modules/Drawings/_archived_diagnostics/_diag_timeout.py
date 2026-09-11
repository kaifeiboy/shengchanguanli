# -*- coding: utf-8 -*-
"""
超时根因诊断：量化 ocr_cli 主链路的真实耗时与 OCR 调用次数。
worker 内 _ocr.Recognize -> OcrWorkerClient.OcrOne -> ocr_cli.main，本脚本直测该链路。
"""
import sys, os, time
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PHOTO = r"E:\workaaa\shengchanguanli\data\seg\116\_user_vq_photo.jpg"

_t_import = time.time()
import ocr_cli
print(f"[IMPORT] ocr_cli {time.time()-_t_import:.2f}s", flush=True)

_calls = []
_orig_raw = ocr_cli._rapidocr_raw


def _patched(image_path):
    t0 = time.time()
    try:
        r = _orig_raw(image_path)
    except Exception as e:
        r = []
        print(f"[OCR-CALL] EXC {type(e).__name__}:{e}", flush=True)
    dt = time.time() - t0
    _calls.append((os.path.basename(str(image_path)), dt))
    print(f"[OCR-CALL] {os.path.basename(str(image_path))}  {dt:.2f}s  lines={len(r) if r else 0}", flush=True)
    return r


ocr_cli._rapidocr_raw = _patched

print(f"\n=== 照片: {PHOTO} ===", flush=True)
print(f"存在: {os.path.exists(PHOTO)}  尺寸: {Image.open(PHOTO).size if os.path.exists(PHOTO) else 'N/A'}", flush=True)

# Step1: _auto_roi（首次OCR + 聚类 + 二次OCR 裁剪放大）
t0 = time.time()
try:
    roi = ocr_cli._auto_roi(PHOTO)
except Exception as e:
    roi = None
    print(f"[AUTO_ROI] EXC {type(e).__name__}:{e}", flush=True)
t_roi = time.time() - t0
print(f"\n[AUTO_ROI] {t_roi:.2f}s  result={repr((roi or '')[:120])}", flush=True)

# Step2: 若 roi 为空则走全图 _run_rapidocr
if not (roi and roi.strip()):
    t0 = time.time()
    text, conf, _ = ocr_cli._run_rapidocr(PHOTO)
    print(f"[RUN_RAPIDOCR] {time.time()-t0:.2f}s len={len(text)}", flush=True)
else:
    print("[RUN_RAPIDOCR] skipped (roi 命中)", flush=True)

total_ocr = sum(d for _, d in _calls)
print(f"\n===== 汇总 =====", flush=True)
print(f"_rapidocr_raw 调用次数: {len(_calls)}", flush=True)
for name, dt in _calls:
    print(f"   - {name}: {dt:.2f}s", flush=True)
print(f"OCR 引擎纯耗时合计: {total_ocr:.2f}s", flush=True)
print(f"_auto_roi 总耗时: {t_roi:.2f}s", flush=True)
print(f"非OCR开销(聚类/裁剪/IO等): {t_roi - total_ocr:.2f}s", flush=True)
