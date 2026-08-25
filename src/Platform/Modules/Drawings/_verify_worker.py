"""单图验证 worker（子进程隔离，崩了不影响批量）。
直接调生产路径 segment_blocks_auto.detect_auto_regions()，产出标注图+切块+result.json。
退出码 0=成功；非0（含139段错误）=失败，由 driver 重试。
"""
import sys, os, json
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
sys.path.insert(0, r"E:/workaaa/shengchanguanli/src/Platform/Modules/Drawings")
import segment_blocks_auto as sba
import fitz
from PIL import Image, ImageDraw
import numpy as np

def main():
    pdf, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    doc = fitz.open(pdf); page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(200/72, 200/72))
    png = os.path.join(outdir, "page.png"); pix.save(png)
    W, H = pix.width, pix.height
    color = Image.open(png).convert("RGB")
    gray = np.array(color.convert("L"))
    regs = sba.detect_auto_regions(pdf, color, gray, W, H, 200) or []
    WH = W * H
    ann = color.copy(); d = ImageDraw.Draw(ann)
    colors = ["#FF3B30", "#34C759", "#007AFF", "#FF9500", "#AF52DE", "#FF2D55", "#5AC8FA", "#FFCC00"]
    res = []
    bdir = os.path.join(outdir, "blocks"); os.makedirs(bdir, exist_ok=True)
    for bi, (x0, y0, x1, y1) in enumerate(regs):
        c = colors[bi % len(colors)]
        d.rectangle([x0, y0, x1, y1], outline=c, width=3)
        d.text((x0 + 4, y0 + 4), f"blk{bi:02d}", fill=c)
        w = x1 - x0; h = y1 - y0
        ar = max(w, h) / max(min(w, h), 1)
        res.append({"blk": bi, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "w": w, "h": h, "aspect": round(ar, 2), "area_ratio": round((w*h)/WH, 4)})
        color.crop((x0, y0, x1, y1)).save(os.path.join(bdir, f"blk_{bi:02d}.png"))
    ann.save(os.path.join(outdir, "page_annotated.png"))
    out = {"file": os.path.basename(pdf), "page_w": W, "page_h": H,
           "n_blocks": len(regs), "blocks": res}
    with open(os.path.join(outdir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"OK blocks={len(regs)}")

if __name__ == "__main__":
    main()
