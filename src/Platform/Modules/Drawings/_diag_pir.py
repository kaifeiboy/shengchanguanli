import faulthandler, sys
faulthandler.enable()
import numpy as np
from PIL import Image

DIR = "C:/Users/Administrator/.paddlex/official_models/PP-DocLayoutV3"
PNG = "E:/workaaa/shengchanguanli/data/seg/_diag/pp_v3_in.png"

img = Image.open(PNG).convert("RGB")
W, H = img.size
img_r = img.resize((800,800), Image.BICUBIC)
x = np.asarray(img_r).astype(np.float32).transpose(2,0,1)[None, ...]

import paddle
print("paddle", paddle.__version__)
try:
    paddle.enable_static()
except Exception as e:
    print("note", repr(e))

exe = paddle.static.Executor(paddle.CPUPlace())

combos = [
    ("inference.json", "inference.pdiparams"),
    ("inference.json", "inference.pdmodel"),
    ("inference.pdmodel", "inference.pdiparams"),
    (None, None),
]
prog=None; feed=None; fetch=None
for mf, pf in combos:
    try:
        kw = dict(path_prefix=DIR, executor=exe)
        if mf: kw["model_filename"]=mf
        if pf: kw["params_filename"]=pf
        prog, feed, fetch = paddle.static.load_inference_model_pir(**kw)
        print("PIR_LOAD_OK mf=%s pf=%s feed=%s fetch=%s" % (mf, pf, feed, fetch))
        break
    except Exception as e:
        print("PIR_LOAD_FAIL mf=%s pf=%s -> %s" % (mf, pf, repr(e)[:200]))

if prog is None:
    print("NO_PIR_LOAD"); sys.exit(3)

print("RUN_START")
try:
    res = exe.run(prog, feed={feed[0]: x}, fetch_list=fetch, return_numpy=True)
    print("INFER_OK n=%d" % len(res))
    for i,r in enumerate(res):
        print("  out[%d] shape=%s dtype=%s" % (i, r.shape, r.dtype))
        flat = r.ravel()
        print("    head=", flat[:12])
        if len(flat) > 12:
            print("    tail=", flat[-12:])
except Exception as e:
    print("INFER_FAIL", repr(e))
    import traceback; traceback.print_exc()
    sys.exit(4)
print("DONE")
