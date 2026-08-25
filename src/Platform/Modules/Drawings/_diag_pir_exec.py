import faulthandler, sys
faulthandler.enable()
import numpy as np
from PIL import Image

DIR = "C:/Users/Administrator/.paddlex/official_models/PP-DocLayoutV3"
PNG = "E:/workaaa/shengchanguanli/data/seg/_diag/pp_v3_in.png"
OUT = "E:/workaaa/shengchanguanli/data/seg/_diag/pir_outs.npz"

img = Image.open(PNG).convert("RGB")
W, H = img.size
img_r = img.resize((800,800), Image.BICUBIC)
x = np.asarray(img_r).astype(np.float32).transpose(2,0,1)[None, ...]

import paddle
print("paddle", paddle.__version__)
paddle.enable_static()

exe = paddle.static.Executor(paddle.CPUPlace())
try:
    prog, feed, fetch = paddle.static.io.load_inference_model_pir(
        path_prefix=DIR, executor=exe,
        model_filename="inference.json", params_filename="inference.pdiparams")
    print("PIR_LOAD_OK feed=", feed, "fetch=", fetch)
except Exception as e:
    print("PIR_LOAD_FAIL", repr(e)); import traceback; traceback.print_exc(); sys.exit(3)

print("RUN_START")
try:
    res = exe.run(prog, feed={feed[0]: x}, fetch_list=fetch, return_numpy=True)
    print("INFER_OK n=", len(res))
    for i,r in enumerate(res):
        print("  out[%d] shape=%s dtype=%s" % (i, r.shape, r.dtype))
        flat = r.ravel()
        print("    head=", flat[:12])
        if len(flat) > 12: print("    tail=", flat[-12:])
    np.savez(OUT, **{"out%d"%i:r for i,r in enumerate(res)})
    print("SAVED", OUT)
except Exception as e:
    print("INFER_FAIL", repr(e)); import traceback; traceback.print_exc(); sys.exit(4)
print("DONE")
