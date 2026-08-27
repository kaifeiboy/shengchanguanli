"""照片→块 定位模块（OCR 驱动文本行归属，主路径）。

设计要点（来自 Phase 1 spike 结论）：
- SIFT/几何配准在产品只占画面小区域 + 杂乱背景（键盘/手）的照片上**本质不可靠**
  （inlier=1.0 但全局映射到背景），故定位主路径改为 **OCR 文本行内容归属**：
  复用 diff_visualizer 的 `_texts_match_strict` / `_norm_text`，把每张照片的 OCR 文本行
  归属到「其文本匹配上该块 CAD 期望文本」的块。块只与归属自己的行比对 → 跨块污染被
  构造性消除（根治反复出现的「修 A 坏 B」）。
- 块在照片上的 ROI = 其归属文本行的 bbox 并集（+边距）。用于对照片直接标示画块外框。
- SIFT 仅保留为实验性辅助（`register_sift`），当前主路径不使用；纯图形/无文本块定位
  失败时标记 `low_conf`，由图标检测/人工复核兜底。

本模块只 import 复用 diff_visualizer 的纯函数，不修改它；不影响其它调用方。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from diff_visualizer import _ocr_image, _texts_match_strict, _norm_text, _bbox_rect  # noqa: E402


def block_expected_lines(block):
    """块 CAD 期望文本 → 逐行列表（按 \\n 拆分，去空）。"""
    raw = block.get("RawText") or ""
    return [ln.strip() for ln in raw.split("\n") if ln.strip()]


# ⭐ v19.41 型号感知匹配：从块 RawText / 照片行提取产品型号（支持 YCWA\d+NCWQ 系列 + QHRLA/QHRLB 编码）。
#   同 PDF 多型号图纸（15NCWQ + 16NCWQ 等）按型号归属照片行——避免跨型号误报"缺标"。
_MODEL_RE = re.compile(r"(?:YCWA\d+NCWQ|QHRLA|QHRLB)", re.IGNORECASE)


def extract_model(text):
    """从单行/单段文本提取产品型号（None 表示非型号行/未识别）。"""
    if not text:
        return None
    m = _MODEL_RE.search(text)
    return m.group(0).upper() if m else None


def block_model(block):
    """从块 RawText 推导所属型号（取首个匹配行）。"""
    raw = block.get("RawText") or ""
    for ln in raw.split("\n"):
        m = extract_model(ln)
        if m:
            return m
    return None


def assign_photo_lines_to_blocks(photo_lines, blocks_db, block_image_text_map=None):
    """把每张照片的 OCR 文本行归属到匹配其文本的块。

    ⭐ v19.41 型号感知过滤（块级）：
      · 块有型号（RawText 含 YCWA\d+NCWQ / QHRLA / QHRLB）→ 仅当"块型号"与
        "照片主导型号"一致时参与归属/比对；型号不同则整块 skip（避免跨型号
        公共行"服务热线/NFC"重复归属造成假绿/假红）。
      · 块/照片任一方无型号 → 退化为原全块归属（不丢能力）。

    ⭐ v19.55 方案 Z（用户拍板 2026-08-26，根治病灶）：
      当 DB RawText 行（如 did=116 blk_4 早期 OCR 错读 "QHRWS"）与 photo OCR 行
      （"QHRW5"）匹配失败时，兜底用「块图实际 OCR 文本」（由调用方
      PR.ocr_photo_tiled(blk_path) 提取并以 block_image_text_map[bidx]=List[str] 传入）
      做归属。块图渲染为真值（CAD 直出），与 photo 实物 OCR 比对 strict 匹配 →
      该 photo 行正确归属对应块 → step4 比对获得完整 b_photo_lines →
      "QHRWS" 经 _texts_char_diff 4/5 同 1 错位 80% 阈值进 green_block + 字符级红，
      不再误报整行红框 + red_block_info 映射 fallback 塌缩同坐标。
      block_image_text_map=None 时严格保持 v19.54 及更早行为（向下兼容）。

    返回:
      assignment: {bidx: [photo_line, ...]}  —— 每块的归属行
      line_owners: {photo_line_index: [bidx, ...]}  —— 每行归属的块（可多归属）
    """
    block_exp = {b["BIdx"]: block_expected_lines(b) for b in blocks_db}
    block_md = {b["BIdx"]: block_model(b) for b in blocks_db}
    # 照片主导型号：取首个提取到型号的 OCR 行
    photo_dom_model = None
    for pl in photo_lines:
        m = extract_model(pl.get("text", ""))
        if m:
            photo_dom_model = m
            break
    assignment = {b["BIdx"]: [] for b in blocks_db}
    line_owners = {}
    for i, pl in enumerate(photo_lines):
        owners = []
        pl_model = extract_model(pl["text"])
        # ── Pass 1：DB RawText 严格匹配（v19.41 型号感知过滤不变）──
        for bidx, exps in block_exp.items():
            if any(_texts_match_strict(pl["text"], e) for e in exps):
                # ⭐ v19.41 块级型号过滤：型号不同的块整块 skip
                if photo_dom_model and block_md.get(bidx) and block_md[bidx] != photo_dom_model:
                    continue
                # 型号行只归属同型号块（即使块无型号也走这条）
                if pl_model and block_md.get(bidx) and pl_model != block_md[bidx]:
                    continue
                owners.append(bidx)
        # ⭐ v19.55 方案 Z Pass 2：DB RawText 无归属时，兜底用块图实际 OCR 文本
        #    （_texts_match_strict 严格匹配 + 同样的型号感知过滤；不动 pass1 已归属结果）
        if not owners and block_image_text_map:
            for bidx, blk_ocr_texts in block_image_text_map.items():
                if bidx not in block_exp:
                    continue
                if photo_dom_model and block_md.get(bidx) and block_md[bidx] != photo_dom_model:
                    continue
                if pl_model and block_md.get(bidx) and pl_model != block_md[bidx]:
                    continue
                if any(_texts_match_strict(pl["text"], bt) for bt in (blk_ocr_texts or []) if bt):
                    owners.append(bidx)
        line_owners[i] = owners
        for bidx in owners:
            assignment[bidx].append(pl)
    return assignment, line_owners


def localize_blocks(photo_lines, blocks_db, margin=0.06, block_image_text_map=None):
    """由归属行推导每块在照片上的 ROI。

    block_image_text_map (Dict[bidx, List[str]]): v19.55 方案 Z —— DB RawText 不匹配
      时用块图实际 OCR 文本做兜底归属。None 表示不启用（保持旧行为，向下兼容）。

    返回:
      loc: {bidx: {"roi": (x,y,w,h)|None, "lines": [...], "conf": "high"|"low"}}
      assignment, line_owners
    """
    assignment, line_owners = assign_photo_lines_to_blocks(
        photo_lines, blocks_db, block_image_text_map=block_image_text_map
    )
    out = {}
    for b in blocks_db:
        bidx = b["BIdx"]
        ls = assignment[bidx]
        if not ls:
            out[bidx] = {"roi": None, "lines": [], "conf": "low"}
            continue
        xs = [p["bbox"][k][0] for p in ls for k in range(4)]
        ys = [p["bbox"][k][1] for p in ls for k in range(4)]
        x0, y0 = min(xs), min(ys)
        x1, y1 = max(xs), max(ys)
        w, h = x1 - x0, y1 - y0
        mx, my = int(w * margin), int(h * margin)
        out[bidx] = {
            "roi": (max(0, int(x0) - mx), max(0, int(y0) - my), int(w) + 2 * mx, int(h) + 2 * my),
            "lines": ls,
            "conf": "high",
        }
    return out, assignment, line_owners


def localize_photo(photo_path, blocks_db, margin=0.06):
    """对一张照片：OCR + 文本行归属 + 块 ROI 定位。"""
    photo_lines = ocr_photo_tiled(photo_path)
    loc, assignment, line_owners = localize_blocks(photo_lines, blocks_db, margin)
    return loc, photo_lines, assignment, line_owners


def _dedupe_lines(lines, iou_thresh=0.5):
    """合并跨 tile 重复识别的同一文本行（同归一化文本 + bbox IoU 高）。"""
    from diff_visualizer import _norm_text as _nt

    kept = []
    for ln in lines:
        n = _nt(ln["text"])
        if not n:
            continue
        dup = False
        for k in kept:
            if _nt(k["text"]) == n:
                # IoU of bboxes
                a = ln["bbox"]; b = k["bbox"]
                ax = [p[0] for p in a]; ay = [p[1] for p in a]
                bx = [p[0] for p in b]; by = [p[1] for p in b]
                ax1, ay1, ax2, ay2 = min(ax), min(ay), max(ax), max(ay)
                bx1, by1, bx2, by2 = min(bx), min(by), max(bx), max(by)
                ix1, iy1 = max(ax1, bx1), max(ay1, by1)
                ix2, iy2 = min(ax2, bx2), min(ay2, by2)
                iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
                inter = iw * ih
                if inter == 0:
                    continue
                area = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
                if inter / area >= iou_thresh:
                    # keep higher-conf
                    try:
                        if float(ln.get("conf", 0)) > float(k.get("conf", 0)):
                            k["conf"] = ln["conf"]
                    except Exception:
                        pass
                    dup = True
                    break
        if not dup:
            kept.append(ln)
    return kept


def ocr_photo_tiled(photo_path, grid=(1, 1), overlap=0.25, max_dim_per_tile=800):
    """整图分块网格 OCR（解决整图小字漏检）。

    默认 grid=(1,1)：实测单块推理已能在 did=115 三张真实照片上零负向(逐块状态/图标
    与 (3,4) 基线完全一致)，且耗时从分块 4 推理(~6-8s)降到 1 推理(~2-4s)。块级差异
    判定走独立的 localize_blocks+_compare_block，与 grid 无关，故调小 grid 不引入
    红/黄/绿判定回归。如需更精细的照片级文本行框，可临时调大 grid。

    把照片切成 grid=(行,列) 带重叠的 tile，每 tile 缩到 max_dim_per_tile 内 OCR
    （文字相对变大，触发检测器），再把 bbox 反算回照片原坐标并跨 tile 去重。
    返回 lines: [{text, bbox:[[x,y]×4], conf}]（照片原坐标系）。
    """
    import numpy as np
    from PIL import Image
    from diff_visualizer import _get_rapidocr

    pil = Image.open(photo_path).convert("RGB")
    W, H = pil.size
    gw, gh = grid
    stepx = W / gw
    stepy = H / gh
    ox = stepx * overlap
    oy = stepy * overlap
    engine = _get_rapidocr()
    all_lines = []
    for r in range(gh):
        for c in range(gw):
            x0 = max(0, int(c * stepx - ox))
            x1 = min(W, int((c + 1) * stepx + ox))
            y0 = max(0, int(r * stepy - oy))
            y1 = min(H, int((r + 1) * stepy + oy))
            if x1 <= x0 or y1 <= y0:
                continue
            tile = pil.crop((x0, y0, x1, y1))
            tw, th = tile.size
            si = 1.0
            if max(tw, th) > max_dim_per_tile:
                s = max_dim_per_tile / max(tw, th)
                si = 1.0 / s
                tile = tile.resize((int(tw * s), int(th * s)), Image.LANCZOS)
            img = np.asarray(tile)
            result, _ = engine(img)
            if not result:
                continue
            for item in result:
                if len(item) < 3:
                    continue
                box, txt, conf = item[0], item[1], item[2]
                corners = [[float(p[0]) * si + x0, float(p[1]) * si + y0] for p in box]
                all_lines.append({"text": txt, "bbox": corners, "conf": conf, "tile": (r, c)})
    return _dedupe_lines(all_lines)


# ── 实验性 SIFT 辅助（当前主路径不使用；产品占小区域+杂乱背景时不可靠）──
def register_sift(photo_path, fullpage_path):
    """复用 geo_score 的 SIFT 求 照片→整页 单应矩阵（实验性）。

    返回 (H, inlier_ratio, photo_size, page_size) 或 (None, 0, ..., ...)。
    仅在 OCR 主路径对某块完全失效、且需几何兜底时参考使用。
    """
    try:
        import numpy as np
        import cv2
        import geo_score as G

        p_gray, p_scale, pnat = _load_gray_scaled(photo_path)
        f_gray, f_scale, fnat = _load_gray_scaled(fullpage_path)
        alg, isfloat = G.make_matcher()
        matcher = G.build_index(isfloat)
        kp_p, desc_p = G.extract(p_gray, alg, isfloat)
        kp_f, desc_f = G.extract(f_gray, alg, isfloat)
        if desc_p is None or desc_f is None or desc_p.size == 0 or desc_f.size == 0:
            return None, 0.0, pnat, fnat
        matches = matcher.knnMatch(desc_f, desc_p, k=2)
        good = []
        for m in matches:
            if len(m) == 2 and m[0].distance < 0.75 * m[1].distance:
                good.append(m[0])
        if len(good) < 4:
            return None, 0.0, pnat, fnat
        pts_f = np.float32([kp_f[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        pts_p = np.float32([kp_p[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(pts_f, pts_p, cv2.RANSAC, 5.0)
        if H is None:
            return None, 0.0, pnat, fnat
        ir = float(mask.sum()) / len(good)
        return H, ir, pnat, fnat
    except Exception:
        return None, 0.0, (0, 0), (0, 0)


def _load_gray_scaled(path, max_dim=1280):
    """与 geo_score 一致的灰度缩放加载，返回 (gray, scale, native_wh)。"""
    import numpy as np
    from PIL import Image

    pil = Image.open(path).convert("L")
    w, h = pil.size
    native_wh = (w, h)
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        pil = pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    else:
        scale = 1.0
    return np.asarray(pil), scale, native_wh
