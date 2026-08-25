#!/usr/bin/env python
"""
图形匹配辅助：为部位识别提供「图形信号」。
用法（供 .NET DrawingService 调用）：
  1. 预渲染图纸页面 + 裁剪各部位区域：
     python graphic_match.py prepare <pdf_path> <drawing_id> <output_dir> ["部位1","部位2",...]
     -> 输出 JSON: { "crops": {"部位名": "crop_png_path"}, ... }

  2. 上传照片 vs 各部位裁剪图的感知哈希(aHash)比较：
     python graphic_match.py match <photo_path> <crop_paths_json>
     -> 输出 JSON: { "photo_hash": "...", "scores": { "部位名": 0~1 } }
"""
import sys, json, os, struct, zlib

def render_pages(pdf_path, out_dir, did):
    """渲染 PDF 页面为低分辨率 PNG，返回 {pageNum: filePath}。"""
    import fitz
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    pages = {}
    for i in range(doc.page_count):
        path = os.path.join(out_dir, f"{did}_p{i+1}.png")
        if not os.path.exists(path):
            pix = doc[i].get_pixmap(dpi=90)   # 低 DPI 足够做哈希
            pix.save(path)
        pages[i + 1] = path
    doc.close()
    return pages


def find_part_rect(page, part_name):
    """在 PyMuPDF 页面上搜索部位名称文字，返回其 Rect 或 None。"""
    # search_for 返回 Rect 列表（精确文本匹配）
    rects = page.search_for(part_name)
    if rects:
        return rects[0]
    # 宽容：取前2字搜索
    if len(part_name) >= 2:
        short = part_name[:2]
        rects = page.search_for(short)
        if rects:
            return rects[0]
    return None


def crop_around(rect, page_w, page_h, expand_ratio=2.5):
    """围绕 rect 扩展一个矩形区域，不超出页面边界。"""
    if rect is None:
        return None
    w = rect.width
    h = rect.height
    ew = min(w * expand_ratio, page_w * 0.7)
    eh = min(h * expand_ratio, page_h * 0.7)
    cx = (rect.x0 + rect.x1) / 2
    cy = (rect.y0 + rect.y1) / 2
    x0 = max(0, cx - ew / 2)
    y0 = max(0, cy - eh / 2)
    x1 = min(page_w, cx + ew / 2)
    y1 = min(page_h, cy + eh / 2)
    import fitz
    return fitz.Rect(x0, y0, x1, y1)


def prepare_crops(pdf_path, did, out_dir, part_names):
    """准备：渲染页面 → 为每个部位定位标签→裁剪区域图。
    返回 dict: { part_name: crop_png_path }。
    """
    import fitz
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    crops = {}

    for pi in range(doc.page_count):
        page = doc[pi]
        pw, ph = page.rect.width, page.rect.height

        for pname in part_names:
            if pname in crops:
                continue  # 已在前面页面找到
            crop_path = os.path.join(out_dir, f"{did}_{pname}.png")
            # 如果已有缓存则跳过裁剪
            if os.path.exists(crop_path):
                crops[pname] = crop_path
                continue

            rect = find_part_rect(page, pname)
            crop_rect = crop_around(rect, pw, ph)
            if crop_rect is None:
                # 回退：整页作为该部位的图形代表
                full_path = os.path.join(out_dir, f"{did}_full_p{pi+1}.png")
                if not os.path.exists(full_path):
                    page.get_pixmap(dpi=70).save(full_path)
                crops[pname] = full_path
                continue

            pix = page.get_pixmap(clip=crop_rect, dpi=110)
            pix.save(crop_path)
            crops[pname] = crop_path

    doc.close()

    # 未找到任何标签的部位，用第一页全图兜底
    remaining = [n for n in part_names if n not in crops]
    if remaining and len(part_names) > 0:
        doc = fitz.open(pdf_path)
        if doc.page_count > 0:
            fallback = os.path.join(out_dir, f"{did}_fallback.png")
            if not os.path.exists(fallback):
                doc[0].get_pixmap(dpi=70).save(fallback)
            for n in remaining:
                crops[n] = fallback
        doc.close()

    return crops


def compute_average_hash(image_path, size=8):
    """计算图片的感知平均哈希 (aHash)：缩放到 size×size 灰度 → 阈值化 → 64 位二进制串。"""
    try:
        from PIL import Image
        img = Image.open(image_path).convert("L").resize((size, size), Image.LANCZOS)
        # 最小尺寸保护：原图太小则返回 None
        if img.width < 4 or img.height < 4:
            return None
        pixels = list(img.get_flattened_data())  # Pillow >=10
        avg = sum(pixels) / len(pixels)
        bits = ''.join('1' if p > avg else '0' for p in pixels)
        return bits
    except Exception:
        return None


def hamming_similarity(h1, h2):
    """两个等长哈希的相似度（1-Hamming/len）。"""
    if not h1 or not h2 or len(h1) != len(h2): return 0.0
    diff = sum(c1 != c2 for c1, c2 in zip(h1, h2))
    return 1.0 - diff / len(h1)


def cmd_prepare(args):
    """prepare <pdf> <did> <outdir> ['p1','p2',...]"""
    if len(args) < 4:
        print(json.dumps({"error": "usage: prepare <pdf> <id> <dir> [\"parts\"]"}))
        sys.exit(1)
    pdf_path, did_str, out_dir = args[0], args[1], args[2]
    part_names = json.loads(args[3]) if len(args) > 3 else []
    crops = prepare_crops(pdf_path, did_str, out_dir, part_names)
    print(json.dumps({"ok": True, "crops": crops}, ensure_ascii=False))


def cmd_match(args):
    """match <photo> <crops_json>  (crops_json = {'name':'path',...})"""
    if len(args) < 2:
        print(json.dumps({"error": "usage: match <photo> <crops_json>"}))
        sys.exit(1)
    photo_path = args[0]
    crops = json.loads(args[1])
    photo_hash = compute_average_hash(photo_path)
    scores = {}
    for name, cpath in crops.items():
        if not os.path.exists(cpath):
            scores[name] = 0.0
            continue
        chash = compute_average_hash(cpath)
        scores[name] = round(hamming_similarity(photo_hash, chash), 3) if chash else 0.0
    print(json.dumps({
        "ok": True,
        "photo_hash": photo_hash,
        "scores": scores
    }, ensure_ascii=False))


def cmd_hash(args):
    """hash <image_path>  -> 输出 aHash 二进制串（64位）"""
    if len(args) < 1:
        print(json.dumps({"error": "usage: hash <image_path>"}))
        sys.exit(1)
    h = compute_average_hash(args[0])
    if h is None:
        h = ""
    print(h)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1].lower()
    rest = sys.argv[2:]
    if cmd == "prepare":
        cmd_prepare(rest)
    elif cmd == "match":
        cmd_match(rest)
    elif cmd == "hash":
        cmd_hash(rest)
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"})); sys.exit(1)
