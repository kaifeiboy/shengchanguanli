#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""精确定位 Paddle 段错误发生阶段：import / 模型构建 / predict。"""
import faulthandler, sys, os, tempfile
faulthandler.enable()
faulthandler.dump_traceback_later(80, exit=True)  # 若卡死则在80s自杀，避免挂起

print("=== step1: import paddleocr ===", flush=True)
from paddleocr import LayoutDetection
print("=== step2: build LayoutDetection(PP-DocLayoutV3) ===", flush=True)
model = LayoutDetection(model_name="PP-DocLayoutV3", device="cpu")
print("=== step3: predict on blank image ===", flush=True)
from PIL import Image
img = Image.new("RGB", (240, 180), (255, 255, 255))
fd, tp = tempfile.mkstemp(suffix=".png")
os.close(fd)
img.save(tp)
try:
    res = model.predict(tp)
    print("=== step4: OK predict returned ===", flush=True)
    print(repr(res)[:300], flush=True)
finally:
    try: os.remove(tp)
    except Exception: pass
print("=== DONE ===", flush=True)
