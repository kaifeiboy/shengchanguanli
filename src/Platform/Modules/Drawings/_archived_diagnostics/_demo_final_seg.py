import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import segment_blocks as sb
from PIL import Image, ImageDraw

PDF = r"E:/生产打标效果图/02-4K3GR线控器效果图-Model.pdf"
OUT = r"E:/workaaa/shengchanguanli/data/seg/_v3final_4k3gr"
os.makedirs(OUT, exist_ok=True)

t0 = time.time()
man = sb.segment(PDF, "9999", OUT, dpi=200)
print(f"[segment] algorithm={man['algorithm']} mode={man['mode']} "
      f"blocks={len(man['blocks'])}  ({time.time()-t0:.1f}s)", flush=True)
for b in man["blocks"]:
    print(f"   blk{b['idx']:02d} {b['file']} x={b['x']} y={b['y']} w={b['w']} h={b['h']}", flush=True)

# 标注原图
full = Image.open(os.path.join(OUT, "4k3gr_full.png")).convert("RGB")
draw = ImageDraw.Draw(full)
for b in man["blocks"]:
    draw.rectangle([b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]],
                   outline=(255, 0, 0), width=5)
full.save(os.path.join(OUT, "page_annotated.png"))
print("[annotated] saved page_annotated.png", flush=True)

# 与用户提供的 PaddleOCR-VL-1.6 JSON 比对
with open(r"C:/Users/Administrator/Desktop/02-4K3GR线控器效果图-Model.pdf_by_PaddleOCR-VL-1.6.json",
          encoding="utf-8") as f:
    jd = json.load(f)
json_blocks = jd[0]["prunedResult"]["parsing_res_list"]
json_imgs = [b for b in json_blocks if b["block_label"] == "image"]

xs = [c for b in json_blocks for c in (b["block_bbox"][0], b["block_bbox"][2])]
ys = [c for b in json_blocks for c in (b["block_bbox"][1], b["block_bbox"][3])]
jw, jh = max(xs), max(ys)
fw, fh = man["width"], man["height"]
sx, sy = fw / jw, fh / jh
print(f"[scale] json {jw}x{jh} -> render {fw}x{fh}  (sx={sx:.3f}, sy={sy:.3f})", flush=True)


def iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix, iy = max(ax0, bx0), max(ay0, by0)
    ix2, iy2 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix2 - ix), max(0, iy2 - iy)
    inter = iw * ih
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / ua if ua > 0 else 0.0


detected = [(b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]) for b in man["blocks"]]
print("\n=== 与 JSON image 块对齐 (IoU) ===", flush=True)
for jb in json_imgs:
    c = jb["block_bbox"]
    jbox = (c[0] * sx, c[1] * sy, c[2] * sx, c[3] * sy)
    best = max(((iou(jbox, d), i) for i, d in enumerate(detected)), default=(0, -1))
    print(f"  json#{jb['block_id']} {jb['block_bbox']} -> det#{best[1]} IoU={best[0]:.3f}", flush=True)
print(f"[summary] detected={len(detected)}  json_images={len(json_imgs)}", flush=True)
print(f"[done] total {time.time()-t0:.1f}s", flush=True)
