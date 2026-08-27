"""HSXC-FN01 水模块 PDF 实切演示：PP-DocLayoutV3 + 舍弃规则。"""
import sys, json, os
sys.path.insert(0, os.path.dirname(__file__))

import fitz
from PIL import Image
import numpy as np
import segment_blocks as sb

PDF = r"E:/生产打标效果图/02-水模块HSXC-FN01效果图-Model.pdf"
OUT = r"E:/workaaa/shengchanguanli/data/seg/_v3final_hsxc"
DID = 7777  # 占位整数 did，仅用于命名/落库
DPI = 200

os.makedirs(OUT, exist_ok=True)
os.makedirs(os.path.join(OUT, "blocks"), exist_ok=True)

# 渲染首张 PDF 页
doc = fitz.open(PDF)
page = doc[0]
pix = page.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72))
png_path = os.path.join(OUT, "page.png")
pix.save(png_path)
print(f"[render] {png_path}  {pix.width}x{pix.height}")

# 调新 auto 主路径（PP-DocLayoutV3 + 舍弃规则；无块则 v6 保底）
man = sb.segment(PDF, str(DID), OUT, dpi=DPI)
print(f"[algorithm] {man.get('algorithm')}  mode={man.get('mode')}")
blocks = man.get("blocks", [])
print(f"[blocks] count={len(blocks)}")

# 标注原图
img = Image.open(png_path).convert("RGB")
W, H = img.size
from PIL import ImageDraw
draw = ImageDraw.Draw(img)
colors = ["#FF3B30", "#34C759", "#007AFF", "#FF9500", "#AF52DE", "#FF2D55", "#5AC8FA", "#FFCC00"]
for i, b in enumerate(blocks):
    x0, y0, x1, y1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    c = colors[i % len(colors)]
    draw.rectangle([x0, y0, x1, y1], outline=c, width=3)
    draw.text((x0 + 4, y0 + 4), f"{i}:{b.get('type','?')}", fill=c)
    print(f"  blk{i:02d} type={b.get('type'):<8} bbox=({x0},{y0},{x1},{y1}) w={b['w']} h={b['h']}")

annot = os.path.join(OUT, "page_annotated.png")
img.save(annot)
print(f"[annot] {annot}")

# 裁剪块图
page_img = Image.open(png_path).convert("RGB")
for i, b in enumerate(blocks):
    x0, y0, x1, y1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    crop = page_img.crop((x0, y0, x1, y1))
    crop.save(os.path.join(OUT, "blocks", f"{DID}_blk_{i:02d}.png"))
print(f"[done] blocks saved to {os.path.join(OUT,'blocks')}")
