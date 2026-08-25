#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单配置探针：import + 构建 LayoutDetection + 对空白图 predict。打印 OK_PREDICT 表示成功。"""
import faulthandler, sys, os, tempfile
faulthandler.enable()
faulthandler.dump_traceback_later(70, exit=True)  # 卡死则70s自杀
from paddleocr import LayoutDetection
model = LayoutDetection(model_name="PP-DocLayoutV3", device="cpu")
from PIL import Image
img = Image.new("RGB", (240, 180), (255, 255, 255))
fd, tp = tempfile.mkstemp(suffix=".png")
os.close(fd)
img.save(tp)
try:
    res = model.predict(tp)
    print("OK_PREDICT blocks=%d" % (len(res[0].get("boxes", [])) if isinstance(res, list) and res else -1), flush=True)
finally:
    try: os.remove(tp)
    except Exception: pass
