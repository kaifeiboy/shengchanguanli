# -*- coding: utf-8 -*-
"""
超时根因诊断 2：实测 ComputeDiffFromPath 的 fresh 子进程耗时。
生产调用（DrawingService.cs:2790）：
  diff_visualizer.py <photo> <blockPng> <outFile> <bboxCache> <blockRawText> <photoText> "" <icons>
"""
import subprocess, time, os, sys

PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
script = os.path.join(HERE, "diff_visualizer.py")

photo = r"E:\workaaa\shengchanguanli\data\seg\116\_user_vq_photo.jpg"
block = r"E:\workaaa\shengchanguanli\data\seg\116\blocks\116_blk_04.png"
out = r"E:\workaaa\shengchanguanli\data\seg\116\_diag_diff_out.jpg"

PHOTO_TEXT = "QHRW5 200512 7437232"

cases = [
    ("A 生产典型(photoText已给, bbox空)", [photo, block, out, "", "", PHOTO_TEXT, "", ""]),
    ("B 全空(最坏: 块需OCR)", [photo, block, out, "", "", "", "", ""]),
]

for name, extra in cases:
    if os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass
    args = [PY, script] + extra
    print(f"\n===== 用例 {name} =====", flush=True)
    t = time.time()
    try:
        r = subprocess.run(args, capture_output=True, text=True, cwd=HERE, timeout=200)
        dt = time.time() - t
        print(f"[DIFF 子进程耗时] {dt:.2f}s  rc={r.returncode}", flush=True)
        so = (r.stdout or "").strip()
        print(f"[STDOUT 前400] {so[:400]}", flush=True)
        if r.stderr:
            print(f"[STDERR 尾600] {(r.stderr or '')[-600:]}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[DIFF 子进程耗时] >200s TIMEOUT!!", flush=True)
