import faulthandler, sys
faulthandler.enable()
import numpy as np
from PIL import Image

DIR = "C:/Users/Administrator/.paddlex/official_models/PP-DocLayoutV3"
PNG = "E:/workaaa/shengchanguanli/data/seg/_diag/pp_v3_in.png"
YML = "C:/Users/Administrator/.paddlex/official_models/PP-DocLayoutV3/inference.yml"

img = Image.open(PNG).convert("RGB")
W, H = img.size
img_r = img.resize((800,800), Image.BICUBIC)
x = np.asarray(img_r).astype(np.float32).transpose(2,0,1)[None, ...]

import paddle, yaml
print("paddle", paddle.__version__)
paddle.enable_static()
cfg = yaml.safe_load(open(YML))
LABELS = cfg["label_list"]
print("n_labels", len(LABELS))

exe = paddle.static.Executor(paddle.CPUPlace())
prog, feed, fetch = paddle.static.io.load_inference_model_pir(
    path_prefix=DIR, executor=exe,
    model_filename="inference.json", params_filename="inference.pdiparams")
print("PIR_LOAD_OK feed=", feed, "fetch=", [str(f) for f in fetch])

# feeds per PaddleX Resize/ToBatch
im_shape = np.array([[800.0, 800.0]], dtype=np.float32)          # [h,w] network input
scale_factor = np.array([[800.0/H, 800.0/W]], dtype=np.float32)  # [h_scale, w_scale]
print("feeds im_shape", im_shape.shape, "scale_factor", scale_factor.shape)

fd = {feed[0]: im_shape, feed[1]: x, feed[2]: scale_factor}
print("RUN_START")
res = exe.run(prog, feed=fd, fetch_list=fetch, return_numpy=True)
print("INFER_OK n=", len(res))
raw = res[0]            # [-1,7]
box_num = int(res[1].reshape(-1)[0])
print("box_num", box_num, "raw.shape", raw.shape)
boxes7 = raw[:box_num]
print("boxes7[:3] (cls,score,x1,y1,x2,y2,order):")
for r in boxes7[:6]:
    print("  ", [round(float(v),2) for v in r])
print("coord ranges: x2max=%.1f y2max=%.1f  (orig %dx%d)" % (boxes7[:,4].max(), boxes7[:,5].max(), W, H))

# 接 LayoutAnalysisProcess
from paddlex.inference.models.layout_analysis.processors import LayoutAnalysisProcess
proc = LayoutAnalysisProcess(labels=LABELS, scale_size=[800,800])
out_boxes = proc.apply(boxes7, (W, H), threshold=0.5, layout_nms=None,
                       layout_unclip_ratio=None, layout_merge_bboxes_mode=None,
                       layout_shape_mode="rect")
print("=== FINAL BOXES (rect mode) ===")
for b in out_boxes:
    print("  %-14s score=%.3f coord=%s" % (b["label"], b["score"], b["coordinate"]))
print("DONE")
