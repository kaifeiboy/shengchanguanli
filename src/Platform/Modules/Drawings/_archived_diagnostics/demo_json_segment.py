#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一次性演示：用离线 PaddleOCR-VL JSON 的拆分方案 + 通用舍弃规则，
把 JQ PDF 在 dpi=200 渲染图上切出"保留块"，并与现行 v6 红框规则对比展示。

不涉及生产代码改动；产物在 data/seg/_jsondemo/。
"""
import json, os, sys
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = "C:/Users/Administrator/Desktop/02-PC-P1HJQ效果图25.7.15dwg-Model.pdf_by_PaddleOCR-VL-1.6.json"
PDF_PATH  = "E:/生产打标效果图/02-PC-P1HJQ效果图25.7.15dwg-Model.pdf"
OUT_DIR   = "E:/workaaa/shengchanguanli/data/seg/_jsondemo"
DPI = 200

# ── 通用舍弃规则（与 plan §3 一致）──
KEEP_LABELS = {"image", "figure", "chart", "diagram"}
DROP_LABELS = {
    "table","text","paragraph_title","vision_footnote","paragraph",
    "caption","formula","abstract",
    "number","footnote","header","header_image","footer","footer_image","aside_text",
    "seal","qr_code","barcode","logo",
}
# 几何层
MIN_MAIN_RATIO_RAW  = 0.15  # 原始：小于最大保留块 15% → 丢（仅去极小图标/碎片）
MIN_MAIN_RATIO_FINAL = 0.40 # 对齐 v6：小于最大保留块 40% 且窄边小 → 丢（去间隙细节块）
MIN_AREA_RATIO = 0.008  # 小于整图 0.8% → 丢（极小碎片）

# v6 参考（现行红框规则在 112 上的 4 块，来自 data/seg/112/blocks/112_blocks.json）
V6_BLOCKS = [
    ("v6 blk_0", 288, 50, 674, 327),
    ("v6 blk_1", 380, 450, 583, 502),
    ("v6 blk_2", 1550, 529, 454, 476),
    ("v6 blk_3", 380, 1009, 543, 262),
]

def load_json_blocks(path):
    raw = json.loads(open(path, encoding="utf-8").read())
    pr = raw[0]["prunedResult"]
    blocks = []
    for b in pr["parsing_res_list"]:
        lab = b.get("block_label")
        box = b.get("block_bbox")  # [x1,y1,x2,y2]
        if box and len(box) == 4:
            blocks.append((lab, tuple(box)))
    return blocks

def main():
    import fitz  # pymupdf
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(os.path.join(OUT_DIR, "kept"), exist_ok=True)

    blocks = load_json_blocks(JSON_PATH)
    print(f"JSON 块总数: {len(blocks)}")

    # 渲染 PDF 到 dpi=200（与 v6 同系）
    doc = fitz.open(PDF_PATH)
    page = doc[0]
    mat = fitz.Matrix(DPI / 72.0, DPI / 72.0)
    pix = page.get_pixmap(matrix=mat)
    W, H = pix.width, pix.height
    print(f"渲染尺寸: {W} x {H} (dpi={DPI})")
    base = Image.frombytes("RGB", (W, H), pix.samples)

    # 推断 JSON 坐标系 → 渲染系 的缩放：用所有块最大坐标作为 JSON 页面范围
    maxx = max(b[1][2] for b in blocks)
    maxy = max(b[1][3] for b in blocks)
    sx, sy = W / maxx, H / maxy
    print(f"JSON 坐标范围: x<= {maxx}, y<= {maxy}  ->  缩放 sx={sx:.3f} sy={sy:.3f}")

    # 分类 + 几何过滤（两版：raw=0.15，final=0.40 对齐 v6）
    def classify(ratio):
        cls = []
        kc = []
        for i, (lab, box) in enumerate(blocks):
            x1, y1, x2, y2 = box
            X1, Y1, X2, Y2 = int(round(x1*sx)), int(round(y1*sy)), int(round(x2*sx)), int(round(y2*sy))
            area = (X2-X1)*(Y2-Y1)
            if lab in KEEP_LABELS:
                kc.append((i, lab, X1, Y1, X2, Y2, area))
                cls.append((i, lab, X1, Y1, X2, Y2, "KEEP_CAND", area))
            elif lab in DROP_LABELS:
                cls.append((i, lab, X1, Y1, X2, Y2, "DROP_LABEL", area))
            else:
                cls.append((i, lab, X1, Y1, X2, Y2, "DROP_UNKNOWN", area))
        if kc:
            max_area = max(c[6] for c in kc)
            page_area = W * H
            for c in kc:
                i, lab, X1, Y1, X2, Y2, area = c
                narrow = min(X2-X1, Y2-Y1)
                if area < max_area * ratio or area < page_area * MIN_AREA_RATIO:
                    for k, item in enumerate(cls):
                        if item[0] == i:
                            cls[k] = (item[0], item[1], item[2], item[3], item[4], item[5], "DROP_GEO", item[7])
                            break
        return cls

    classified_raw = classify(MIN_MAIN_RATIO_RAW)
    classified = classify(MIN_MAIN_RATIO_FINAL)  # 最终采用
    kept = [c for c in classified if c[6] == "KEEP_CAND"]
    kept_raw = [c for c in classified_raw if c[6] == "KEEP_CAND"]
    print(f"[raw 0.15] 保留块: {len(kept_raw)}  | [final 0.40] 保留块: {len(kept)}  | 丢弃: {sum(1 for c in classified if c[6]!='KEEP_CAND')}")

    # 切出保留块
    manifest = {"drawingId": "112-demo", "width": W, "height": H,
                "algorithm": "json-demo+discard-rule", "mode": "paddleocr-vl-json",
                "blocks": []}
    for j, c in enumerate(kept):
        i, lab, X1, Y1, X2, Y2, dec, area = c
        crop = base.crop((X1, Y1, X2, Y2))
        fname = f"kept/{lab}_{i:02d}.png"
        crop.save(os.path.join(OUT_DIR, fname))
        manifest["blocks"].append({
            "idx": j, "x": X1, "y": Y1, "w": X2-X1, "h": Y2-Y1,
            "file": fname, "type": "engineering", "srcLabel": lab, "srcIdx": i
        })

    # 全景标注图（两版）
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
    except Exception:
        font = ImageFont.load_default()

    def box(draw, x1, y1, x2, y2, color, label=None, dash=False, width=3):
        if dash:
            step = 14
            for o in range(0, x2-x1, step*2):
                draw.line([x1+o, y1, min(x1+o+step, x2), y1], fill=color, width=width)
                draw.line([x1+o, y2, min(x1+o+step, x2), y2], fill=color, width=width)
            for o in range(0, y2-y1, step*2):
                draw.line([x1, y1+o, x1, min(y1+o+step, y2)], fill=color, width=width)
                draw.line([x2, y1+o, x2, min(y1+o+step, y2)], fill=color, width=width)
        else:
            draw.rectangle([x1, y1, x2, y2], outline=color, width=width)
        if label:
            tw = draw.textlength(label, font=font)
            draw.rectangle([x1, y1-26, x1+tw+8, y1], fill=color)
            draw.text((x1+4, y1-24), label, fill="white", font=font)

    def draw_annot(cls_list, kept_list, out_name, title):
        ann = base.copy()
        d = ImageDraw.Draw(ann)
        # v6 参考（蓝色虚线）
        for name, x, y, w, h in V6_BLOCKS:
            box(d, x, y, x+w, y+h, (0, 90, 220), dash=True, width=2)
            d.text((x, y-48), name, fill=(0, 90, 220), font=font)
        # 丢弃块（红）
        for c in cls_list:
            if c[6] != "KEEP_CAND":
                i, lab, X1, Y1, X2, Y2, dec, area = c
                box(d, X1, Y1, X2, Y2, (220, 40, 40), label=f"{i}:{lab}", width=2)
        # 保留块（绿）
        for c in kept_list:
            i, lab, X1, Y1, X2, Y2, dec, area = c
            box(d, X1, Y1, X2, Y2, (30, 180, 60), label=f"KEEP {i}:{lab}", width=4)
        d.text((10, 10), title, fill=(0, 0, 0), font=font)
        ann.save(os.path.join(OUT_DIR, out_name))

    draw_annot(classified_raw, kept_raw, "page_annotated_raw5.png",
               f"RAW auto (ratio=0.15): {len(kept_raw)} kept + v6 ref(blue)")
    draw_annot(classified, kept, "page_annotated.png",
               f"FINAL (ratio=0.40, align v6): {len(kept)} kept + v6 ref(blue)")

    with open(os.path.join(OUT_DIR, "auto_blocks.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print("\n=== 分类摘要 ===")
    for c in classified:
        i, lab, X1, Y1, X2, Y2, dec, area = c
        print(f"  [{i:2d}] {lab:16s} {dec:12s} area={area:>9d}  box=({X1},{Y1})-({X2},{Y2})")
    print(f"\n保留块数: {len(kept)}  | v6 块数: {len(V6_BLOCKS)}")
    print(f"产物目录: {OUT_DIR}")

if __name__ == "__main__":
    main()
