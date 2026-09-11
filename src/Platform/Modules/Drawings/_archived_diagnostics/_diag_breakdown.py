# -*- coding: utf-8 -*-
"""
超时根因诊断 3：把 diff 的 16s 拆成 导入 / 模型加载 / 实际计算 三段。
"""
import sys, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

t0 = time.time()
import diff_visualizer as DV
print(f"[1] import diff_visualizer : {time.time()-t0:.2f}s", flush=True)

t0 = time.time()
try:
    DV._get_rapidocr()
    print(f"[2] _get_rapidocr() 模型加载: {time.time()-t0:.2f}s", flush=True)
except Exception as e:
    print(f"[2] _get_rapidocr EXC {type(e).__name__}:{e}", flush=True)

photo = r"E:\workaaa\shengchanguanli\data\seg\116\_user_vq_photo.jpg"
block = r"E:\workaaa\shengchanguanli\data\seg\116\blocks\116_blk_04.png"
out1 = r"E:\workaaa\shengchanguanli\data\seg\116\_bd_out1.jpg"
out2 = r"E:\workaaa\shengchanguanli\data\seg\116\_bd_out2.jpg"

kw = dict(block_bbox_cache=None,
          block_text_override=None,
          photo_text_override="QHRW5 200512 7437232",
          icon_regions_override=None)

t0 = time.time()
r1 = DV.run(photo, block, out1, **kw)
print(f"[3] run() 第1次(引擎已加载) : {time.time()-t0:.2f}s", flush=True)

t0 = time.time()
r2 = DV.run(photo, block, out2, **kw)
print(f"[4] run() 第2次(全热)      : {time.time()-t0:.2f}s", flush=True)

try:
    print(f"[结果一致性] v1={r1.get('_debugVersion')} v2={r2.get('_debugVersion')}", flush=True)
    print(f"[结果一致性] green1={len(r1.get('greenRegions') or [])} green2={len(r2.get('greenRegions') or [])}", flush=True)
    print(f"[结果一致性] yellow1={len(r1.get('yellowRegions') or [])} yellow2={len(r2.get('yellowRegions') or [])}", flush=True)
    print(f"[结果一致性] red1={len(r1.get('redRegions') or [])} red2={len(r2.get('redRegions') or [])}", flush=True)
except Exception as e:
    print(f"[结果对比 EXC] {e}", flush=True)
