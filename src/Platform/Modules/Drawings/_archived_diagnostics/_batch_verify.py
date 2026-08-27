"""批量验证 PP-DocLayoutV3 auto 切图规则（2026-07-23 跨图验证）。
直接调 segment_blocks_auto.detect_auto_regions() —— 即生产里 auto 优先那条路径，
统计每张图检出的块数、每个块的 label/尺寸/宽高比/面积占比，产出标注原图 + 汇总 JSON。
不改动任何生产代码。
"""
import sys, os, glob, json, traceback
sys.path.insert(0, r"E:/workaaa/shengchanguanli/src/Platform/Modules/Drawings")
import segment_blocks_auto as sba
import fitz
from PIL import Image
import numpy as np

SRC = r"E:/生产打标效果图"
OUT = r"E:/workaaa/shengchanguanli/data/seg/_batch_verify"
os.makedirs(OUT, exist_ok=True)

DPI = 200
# 安全环境变量（避免 Paddle 原生崩溃）
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

def sanitize(name):
    return name.replace(" ", "_").replace("(", "_").replace(")", "_").replace("/", "_")

def main():
    pdfs = sorted(glob.glob(os.path.join(SRC, "*.pdf")))
    summary = []
    print(f"FOUND {len(pdfs)} PDFs\n")
    for i, pdf in enumerate(pdfs):
        base = os.path.basename(pdf)
        sname = sanitize(base)
        sub = os.path.join(OUT, sname)
        os.makedirs(sub, exist_ok=True)
        rec = {"file": base, "idx": i, "blocks": [], "error": None}
        try:
            doc = fitz.open(pdf)
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(DPI/72, DPI/72))
            png = os.path.join(sub, "page.png")
            pix.save(png)
            W, H = pix.width, pix.height
            color = Image.open(png).convert("RGB")
            gray = np.array(color.convert("L"))
            regs = sba.detect_auto_regions(pdf, color, gray, W, H, DPI)
            regs = regs or []
            WH = W * H
            # 标注原图
            from PIL import ImageDraw
            ann = color.copy()
            d = ImageDraw.Draw(ann)
            colors = ["#FF3B30", "#34C759", "#007AFF", "#FF9500", "#AF52DE", "#FF2D55", "#5AC8FA", "#FFCC00"]
            for bi, (x0, y0, x1, y1) in enumerate(regs):
                c = colors[bi % len(colors)]
                d.rectangle([x0, y0, x1, y1], outline=c, width=3)
                d.text((x0+4, y0+4), f"blk{bi:02d}", fill=c)
                w = x1 - x0; h = y1 - y0
                ar = max(w, h) / max(min(w, h), 1)
                area_r = (w * h) / WH
                rec["blocks"].append({
                    "blk": bi, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "w": w, "h": h, "aspect": round(ar, 2),
                    "area_ratio": round(area_r, 4),
                })
            ann.save(os.path.join(sub, "page_annotated.png"))
            # 切出块
            bdir = os.path.join(sub, "blocks")
            os.makedirs(bdir, exist_ok=True)
            for bi, (x0, y0, x1, y1) in enumerate(regs):
                crop = color.crop((x0, y0, x1, y1))
                crop.save(os.path.join(bdir, f"blk_{bi:02d}.png"))
            rec["page_w"], rec["page_h"] = W, H
            rec["n_blocks"] = len(regs)
            flag = "OK" if regs else "EMPTY(no auto blocks->would fallback v6)"
            print(f"[{i+1:02d}/{len(pdfs)}] {base[:42]:42} blocks={len(regs):2d} {flag}")
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[{i+1:02d}/{len(pdfs)}] {base[:42]:42} ERROR {rec['error']}")
            traceback.print_exc()
        summary.append(rec)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    # 打印紧凑汇总表
    print("\n=== SUMMARY TABLE ===")
    print(f"{'#':>3} {'blocks':>6} {'file':<46}")
    for r in summary:
        bs = ",".join(f"b{b['blk']}:{b['w']}x{b['h']} AR{b['aspect']}" for b in r["blocks"])
        print(f"{r['idx']+1:>3} {str(r.get('n_blocks','?')):>6} {r['file'][:46]:<46} {bs}")
    print(f"\nTOTAL={len(pdfs)}  wrote summary.json + annotated PNGs to {OUT}")

if __name__ == "__main__":
    main()
