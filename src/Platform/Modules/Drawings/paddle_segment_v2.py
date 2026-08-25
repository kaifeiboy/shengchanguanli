# -*- coding: utf-8 -*-
"""
paddle_segment_v2.py — 工程图纸自动切图（PaddleOCR + 几何轮廓 混合策略）

问题: PP-DocLayout_plus-L 对纯线框工程视图漏检严重(4K3GR 只检出1/4个视图)
方案: 不依赖 layout 模型的 image 标签，改用:
  1. OpenCV 轮廓检测 -> 找矩形区域候选(工程视图通常有明确边框)
  2. PaddleOCR OCR 文本定位 -> 找文字密集区(打标标识区必有文字)
  3. 两者融合 + 舍弃规则(面积/位置/内容) -> 产出保留块

用法: python paddle_segment_v2.py <pdf_path> <out_dir> [dpi=200]
"""
import os, sys, json
import numpy as np

# ── 参数 ──
MIN_AREA_RATIO = 0.005      # < 整图 0.5% -> 丢碎片
MIN_MAIN_RATIO = 0.45       # < 最大(有效)块 45% -> 丢小图/图标/注释/BOM
MAX_PAGE_RATIO = 0.40       # > 整图 40% -> 丢超大轮廓(整页外框/背景)
MAX_BLOCKS = 12             # 安全上限，防止碎块爆炸


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


def detect_by_contours(img_np):
    """OpenCV 轮廓检测: 找矩形区域作为视图候选"""
    import cv2
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    
    # 二值化: 线框图线条是黑的，背景白的
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    
    # 膨胀连接断线，再找轮廓
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    dilated = cv2.dilate(binary, kernel, iterations=2)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    boxes = []
    W, H = gray.shape[1], gray.shape[0]
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = w * h
        if area < W * H * MIN_AREA_RATIO:
            continue
        if w < 30 or h < 30:
            continue
        # 过滤掉全页大轮廓(整张图本身)
        if area > W * H * 0.85:
            continue
        boxes.append(("contour", 0.0, [x, y, x + w, y + h], area))
    
    return boxes


def detect_by_ocr_regions(img_path):
    """PaddleOCR OCR: 检测文字区域，聚类成文本块"""
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(lang='ch')
    result = ocr.ocr(img_path)  # 3.x: ocr() 仍返回完整识别结果 (deprecated but works)
    
    # 3.x call() 返回: list[OCRResult] -> .json['res']['dt_polys'] = [[x1,y1]...] + rec_texts
    text_boxes = []
    if not result:
        return text_boxes
    
    res = result[0].json.get('res', {})
    dt_polys = res.get('dt_polys', [])
    # 尝试获取识别文本（可能在 rec_texts 或其他字段）
    texts = res.get('rec_texts', [])
    
    for i, poly in enumerate(dt_polys):
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        w, h = x2 - x1, y2 - y1
        text = texts[i] if i < len(texts) else ""
        conf = 0.9  # 3.x 默认置信度字段位置不确定，给默认值
        if w > 5 and h > 5:
            text_boxes.append((text, conf, [x1, y1, x2, y2], w * h))
    
    return text_boxes


def cluster_text_to_blocks(text_boxes, W, H):
    """将 OCR 文本框按空间邻近性聚类为块"""
    if not text_boxes:
        return []
    
    # 简单网格聚类: 按 1/4 页面分 grid，同一 grid 内的文本合并
    grid_w = W // 4
    grid_h = H // 4
    
    grids = {}
    for tb in text_boxes:
        text, conf, box, area = tb
        cx = (box[0] + box[2]) // 2
        cy = (box[1] + box[3]) // 2
        gx = int(cx // grid_w)
        gy = int(cy // grid_h)
        key = (gx, gy)
        if key not in grids:
            grids[key] = []
        grids[key].append(box)
    
    blocks = []
    for key, bxs in grids.items():
        all_x = [b[0] for b in bxs] + [b[2] for b in bxs]
        all_y = [b[1] for b in bxs] + [b[3] for b in bxs]
        x1, y1, x2, y2 = min(all_x), min(all_y), max(all_x), max(all_y)
        # 加 10% padding
        pw = (x2 - x1) * 0.1
        ph = (y2 - y1) * 0.1
        blocks.append(("ocr_cluster", 0.0,
                       [max(0, x1 - pw), max(0, y1 - ph),
                        x2 + pw, y2 + ph],
                       (x2 - x1 + 2 * pw) * (y2 - y1 + 2 * ph)))
    
    return blocks


def merge_candidates(contour_blocks, ocr_blocks, W, H):
    """合并轮廓候选和 OCR 聚类块: 取并集，去重"""
    all_blocks = []
    
    for cb in contour_blocks:
        all_blocks.append(cb)
    for ob in ocr_blocks:
        all_blocks.append(ob)
    
    # IoU 去重
    def iou(a, b):
        ax1, ay1, ax2, ay2 = a[2]
        bx1, by1, bx2, by2 = b[2]
        ix1 = max(ax1, bx1); iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2); iy2 = min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        union = a[3] + b[3] - inter
        return inter / union if union > 0 else 0
    
    kept = []
    for block in sorted(all_blocks, key=lambda b: -b[3]):  # 面积降序
        duplicate = False
        for k in kept:
            if iou(block, k) > 0.5:
                duplicate = True
                break
        if not duplicate:
            kept.append(block)
    
    return kept[:MAX_BLOCKS]


def apply_discard_rules(blocks, W, H):
    """通用舍弃规则"""
    if not blocks:
        return []
    
    page_area = W * H
    
    # 第1步: 丢弃超大轮廓(整页外框/背景)
    valid = []
    for b in blocks:
        src, score, box, area = b
        if area > page_area * MAX_PAGE_RATIO:
            valid.append([src, score, box, area, "DROP_PAGE"])
        else:
            valid.append([src, score, box, area, "PENDING"])
    
    # 第2步: 在有效块中做相对大小比较
    pending = [c for c in valid if c[4] == "PENDING"]
    if not pending:
        return valid
    
    max_area = max(c[3] for c in pending)
    
    for c in valid:
        if c[4] != "PENDING":
            continue
        if c[3] < page_area * MIN_AREA_RATIO:
            c[4] = "DROP_TINY"
        elif c[3] < max_area * MIN_MAIN_RATIO:
            c[4] = "DROP_SMALL"
        else:
            c[4] = "KEEP"
    
    return valid


def draw_annotation(base_img, classified, out_path):
    from PIL import ImageDraw, ImageFont
    ann = base_img.copy()
    d = ImageDraw.Draw(ann)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
    except Exception:
        font = ImageFont.load_default()
    
    for c in classified:
        src, score, box, area, dec = c
        x1, y1, x2, y2 = box
        if dec == "KEEP":
            color = (30, 180, 60); width = 4
            tag = f"KEEP {src}"
        else:
            color = (220, 40, 40); width = 2
            tag = f"{src} ({dec})"
        d.rectangle([x1, y1, x2, y2], outline=color, width=width)
        tw = d.textlength(tag, font=font)
        ty = max(0, y1 - 30)
        d.rectangle([x1, ty, x1 + tw + 8, ty + 30], fill=color)
        d.text((x1 + 4, ty + 2), tag, fill="white", font=font)
    
    ann.save(out_path)


def main():
    pdf_path = sys.argv[1]
    out_dir = sys.argv[2]
    dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    os.makedirs(out_dir, exist_ok=True)
    kept_dir = os.path.join(out_dir, "kept")
    os.makedirs(kept_dir, exist_ok=True)

    print(f"[1/5] 渲染 PDF dpi={dpi} ...")
    base = render_pdf(pdf_path, dpi)
    W, H = base.size
    page_path = os.path.join(out_dir, "page_full.png")
    base.save(page_path)
    print(f"      页面尺寸 {W}x{H}")

    img_np = np.array(base)

    print("[2/5] OpenCV 轮廓检测 ...")
    contour_blocks = detect_by_contours(img_np)
    print(f"      轮廓候选: {len(contour_blocks)} 块")

    print("[3/5] PaddleOCR OCR 区域检测 ...")
    text_boxes = detect_by_ocr_regions(page_path)
    ocr_blocks = cluster_text_to_blocks(text_boxes, W, H)
    print(f"      OCR 文本行: {len(text_boxes)} -> 聚类块: {len(ocr_blocks)}")

    print("[4/5] 合并候选 + 舍弃规则 ...")
    merged = merge_candidates(contour_blocks, ocr_blocks, W, H)
    classified = apply_discard_rules(merged, W, H)
    kept = [c for c in classified if c[4] == "KEEP"]
    
    print(f"      候选合并: {len(merged)} -> 保留: {len(kept)}")
    for c in classified:
        src, score, box, area, dec = c
        mark = "✅" if dec == "KEEP" else "❌"
        print(f"   {mark} {src:16s} box={box} area%={area/(W*H)*100:.2f} {dec}")

    print("[5/5] 切图 + 标注 ...")
    ann_path = os.path.join(out_dir, "page_annotated.png")
    draw_annotation(base, classified, ann_path)

    manifest = {"pdf": os.path.basename(pdf_path), "width": W, "height": H,
                "algorithm": "paddle-ocr-contour-v2", "dpi": dpi, "blocks": []}

    for k, c in enumerate(sorted(kept, key=lambda c: (c[2][1], c[2][0]))):
        src, score, box, area, dec = c
        x1, y1, x2, y2 = box
        crop = base.crop((x1, y1, x2, y2))
        f = f"blk_{k:02d}_{src}.png"
        crop.save(os.path.join(kept_dir, f))
        manifest["blocks"].append({"idx": k, "x": x1, "y": y1,
                                   "w": x2 - x1, "h": y2 - y1,
                                   "file": f, "type": "engineering",
                                   "src_method": src})

    with open(os.path.join(out_dir, "auto_blocks.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)
    print(f"      完成: {len(kept)} 块 -> {kept_dir}")
    print(f"      标注图: {ann_path}")


if __name__ == "__main__":
    main()
