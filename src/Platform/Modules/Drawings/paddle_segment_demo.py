# -*- coding: utf-8 -*-
"""
paddle_segment_demo.py — 本地 PaddleOCR 版面检测 + 通用舍弃规则 实切演示
用法: python paddle_segment_demo.py <pdf_path> <out_dir> [dpi=200]

流程（与 segment_blocks_auto.py 未来的 detect_auto_regions 同构）:
  1. PyMuPDF 按 dpi 渲染 PDF 第一页 -> 全彩底图（坐标系与生产切图一致）
  2. PaddleOCR LayoutDetection (PP-DocLayout_plus-L) 就地检测 -> 带语义标签的块
  3. 舍弃规则:
     - 标签层: 仅保留 {image, figure, chart, diagram}; 其余(table/text/标题/页眉脚/印章/编号等)全丢
     - 几何层: 丢 面积<整图0.8% 的碎片; 丢 面积<最大保留块40% 的孤立细节块
  4. 产出: kept/ 保留块切图 + page_annotated.png 全景标注(绿保留/红丢弃) + auto_blocks.json
仅演示用，不改任何生产文件。
"""
import os, sys, json

KEEP_LABELS = {"image", "figure", "chart", "diagram"}
MIN_AREA_RATIO = 0.008   # < 整图 0.8% -> 丢（碎片/小图标）
MIN_MAIN_RATIO = 0.40    # < 最大保留块 40% -> 丢（孤立细节块，对齐 v6 决策）


def render_pdf(pdf_path, dpi):
    import fitz
    doc = fitz.open(pdf_path)
    page = doc[0]
    zoom = dpi / 72.0
    pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    from PIL import Image
    img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    doc.close()
    return img


def detect_layout(img_path):
    """返回 [(label, score, [x1,y1,x2,y2]), ...]"""
    from paddleocr import LayoutDetection
    model = LayoutDetection(model_name="PP-DocLayout_plus-L")
    out = model.predict(img_path, batch_size=1)
    blocks = []
    for res in out:
        d = res.json if isinstance(res.json, dict) else json.loads(res.json)
        r = d.get("res", d)
        for b in r.get("boxes", []):
            label = b.get("label")
            score = float(b.get("score", 0))
            x1, y1, x2, y2 = [int(round(v)) for v in b.get("coordinate")]
            blocks.append((label, score, [x1, y1, x2, y2]))
    return blocks


def main():
    pdf_path = sys.argv[1]
    out_dir = sys.argv[2]
    dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    os.makedirs(out_dir, exist_ok=True)
    kept_dir = os.path.join(out_dir, "kept")
    os.makedirs(kept_dir, exist_ok=True)

    print(f"[1/4] 渲染 PDF dpi={dpi} ...")
    base = render_pdf(pdf_path, dpi)
    W, H = base.size
    page_path = os.path.join(out_dir, "page_full.png")
    base.save(page_path)
    print(f"      页面尺寸 {W}x{H}")

    print("[2/4] PaddleOCR 版面检测 ...")
    blocks = detect_layout(page_path)
    print(f"      检出 {len(blocks)} 块")

    # ── 舍弃规则 ──
    classified = []  # (i, label, score, box, decision, area)
    cands = []
    for i, (lab, score, box) in enumerate(blocks):
        x1, y1, x2, y2 = box
        area = max(0, x2 - x1) * max(0, y2 - y1)
        if lab in KEEP_LABELS:
            cands.append((i, area))
            classified.append([i, lab, score, box, "KEEP_CAND", area])
        else:
            classified.append([i, lab, score, box, "DROP_LABEL", area])

    if cands:
        max_area = max(a for _, a in cands)
        page_area = W * H
        for c in classified:
            if c[4] != "KEEP_CAND":
                continue
            if c[5] < page_area * MIN_AREA_RATIO:
                c[4] = "DROP_TINY"
            elif c[5] < max_area * MIN_MAIN_RATIO:
                c[4] = "DROP_DETAIL"

    kept = [c for c in classified if c[4] == "KEEP_CAND"]
    print(f"[3/4] 舍弃规则: 检出{len(blocks)} -> 标签保留{len(cands)} -> 最终保留{len(kept)}")
    for c in classified:
        i, lab, score, box, dec, area = c
        mark = "✅" if dec == "KEEP_CAND" else "❌"
        print(f"   {mark} [{i:2d}] {lab:18s} score={score:.3f} box={box} area%={area/(W*H)*100:.2f} {dec}")

    # ── 切图 + 标注 ──
    from PIL import ImageDraw, ImageFont
    ann = base.copy()
    d = ImageDraw.Draw(ann)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
    except Exception:
        font = ImageFont.load_default()

    manifest = {"pdf": os.path.basename(pdf_path), "width": W, "height": H,
                "algorithm": "paddle-layout-v1", "dpi": dpi, "blocks": []}

    for c in classified:
        i, lab, score, box, dec, area = c
        x1, y1, x2, y2 = box
        if dec == "KEEP_CAND":
            color = (30, 180, 60); width = 5
            tag = f"KEEP {i}:{lab}"
        else:
            color = (220, 40, 40); width = 2
            tag = f"{i}:{lab}"
        d.rectangle([x1, y1, x2, y2], outline=color, width=width)
        tw = d.textlength(tag, font=font)
        ty = max(0, y1 - 30)
        d.rectangle([x1, ty, x1 + tw + 8, ty + 30], fill=color)
        d.text((x1 + 4, ty + 2), tag, fill="white", font=font)

    for k, c in enumerate(sorted(kept, key=lambda c: (c[3][1], c[3][0]))):
        i, lab, score, box, dec, area = c
        x1, y1, x2, y2 = box
        crop = base.crop((x1, y1, x2, y2))
        f = f"blk_{k:02d}_src{i}.png"
        crop.save(os.path.join(kept_dir, f))
        manifest["blocks"].append({"idx": k, "x": x1, "y": y1,
                                   "w": x2 - x1, "h": y2 - y1,
                                   "file": f, "type": "engineering",
                                   "src_label": lab, "score": round(score, 3)})

    ann_path = os.path.join(out_dir, "page_annotated.png")
    ann.save(ann_path)
    with open(os.path.join(out_dir, "auto_blocks.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)
    print(f"[4/4] 完成: 保留 {len(kept)} 块 -> {kept_dir}")
    print(f"      标注图: {ann_path}")


if __name__ == "__main__":
    main()
