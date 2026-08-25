import faulthandler, sys, os
faulthandler.enable()
import numpy as np
from PIL import Image

PADDLE_DIR = "C:/Users/Administrator/.paddlex/official_models/PP-DocLayoutV3"
PNG = "E:/workaaa/shengchanguanli/data/seg/_diag/pp_v3_in.png"

img = Image.open(PNG).convert("RGB")
W, H = img.size
print("ORIG", W, H)
img_r = img.resize((800,800), Image.BICUBIC)
x = np.asarray(img_r).astype(np.float32)
x = x.transpose(2,0,1)[None, ...]
print("INPUT", x.shape, x.dtype)

import paddle
print("paddle", paddle.__version__)
try:
    paddle.enable_static()
    print("enable_static OK")
except Exception as e:
    print("enable_static note", repr(e))

# load_inference_model may live in paddle.static or paddle.base.io
def try_load(exe):
    import importlib
    for modpath, fn in [
        ("paddle.static", "load_inference_model"),
        ("paddle.base.io", "load_inference_model"),
        ("paddle.base", "load_inference_model"),
    ]:
        try:
            mod = importlib.import_module(modpath)
            f = getattr(mod, fn)
            prog, feed, fetch = f(path_prefix=PADDLE_DIR, executor=exe,
                                  model_filename="inference.pdmodel",
                                  params_filename="inference.pdiparams")
            print("LOAD_OK via", modpath, "feed=", feed, "fetch=", fetch)
            return prog, feed, fetch
        except Exception as e:
            print("LOAD_via", modpath, "FAIL", repr(e))
    return None

# Executor
try:
    exe = paddle.static.Executor(paddle.CPUPlace())
    res = try_load(exe)
    if res is None:
        print("ALL_LOAD_FAILED"); sys.exit(3)
    program, feed_names, fetch_names = res
except Exception as e:
    print("EXE_FAIL", repr(e)); sys.exit(3)

print("RUN_START")
try:
    results = exe.run(program, feed={feed_names[0]: x},
                      fetch_list=fetch_names, return_numpy=True)
    print("INFER_OK n_out=", len(results))
    for i, r in enumerate(results):
        print("  out[%d] shape=%s dtype=%s" % (i, r.shape, r.dtype))
        if r.size < 30:
            print("    vals=", r.ravel()[:15])
except Exception as e:
    print("INFER_FAIL", repr(e))
    import traceback; traceback.print_exc()
    sys.exit(4)
print("DONE")
