# -*- coding: utf-8 -*-
"""vpdf.fallback — 区域触发式局部视觉兜底（方案 §1.8 / §1.9）。

【为什么需要】
矢量图纸里有两类「文字层读不到」的内容：
  1. **转曲**（outline / 虚拟打印机产物）—— 文字已变成矢量路径，`get_text` 拿不到；
  2. **禁止整页 OCR** —— 方案 §1 明确只做「小区域局部识别」。

【触发条件（严格执行，防误触发）】
先按矢量路径筛出「字符状图元」聚簇（图元数够多 + 尺寸够小），得到文本行簇；
再把**与文字层 span 有交叠的簇全部丢弃**，剩下的才是真正的「无文字层的可疑内容区」。
只有这些区域会被渲染 + OCR，因此结构上不可能把文字层已有的内容识别两遍。

    触发条件 = 该区域无文字层 span 覆盖 且 矢量路径密度超阈值

【两条实测硬约束（勿改）】
  * RapidOCR 对极小 / 极端长宽比的裁剪会 0 检出 —— **四周必须补 ≥64px 白边**
    （DB 检测需要上下文）。本实现用 `min_margin`（默认 80px）保证。
  * 双分辨率投票（200 + 300 dpi）比单分辨率稳定得多；同一文本取置信度更高的那条。

【职责边界】
本模块只产出感知结果（区域 + 文本 + 坐标 + 置信度），**不判断哪些是打标对象** ——
那是 C# MarkBuilder 的决策层职责（产出 `Source.Kind="vision_fallback"` 的 mark）。
"""
from __future__ import annotations

import time
from typing import Any, Sequence

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore

SCHEMA = "vpdf-fallback/1"
VERSION = "1.0.0"

# —— 已验证参数（2026-09-22 离线实验 p14 v8：目标内容 9/9 命中）——
DEFAULT_MAX_SIZE = 0.05      # 字符状图元的归一化边长上限
DEFAULT_MIN_ITEMS = 8        # 单条路径至少要有这么多个图元才可能是「字」而非大图框
DEFAULT_CLUSTER_EPS = 0.006  # 字符 → 文本行/词 的聚簇半径（归一化）
DEFAULT_MERGE_EPS = 0.022    # 簇 → 区域的合并半径（归一化）
DEFAULT_PAD = 0.008          # 区域渲染外扩
DEFAULT_SPAN_PAD = 0.002     # 与 span 判定交叠时的容差
DEFAULT_MIN_MARGIN = 80      # RapidOCR 白边（px）
DEFAULT_MIN_CLUSTER = 3      # 少于这么多字符的簇丢弃（多为图标噪点）

# 主分辨率优先、读到了就不再试第二个 —— 实测图纸里的型号码必须 ≥300dpi 才读得准
# （200dpi 会把 YCWA15NCWQ 读成 YCWATSNCWQ，见 2026-09-22 定点对照）。
DEFAULT_DPIS = (300, 200)

# —— RapidOCR 调优：不要动 det_limit_side_len（2026-09-22 实测反例）——
# 坑 1：kwargs 必须带模块前缀（det_/cls_/rec_），且 Det 组**必须同时给 det_model_path**，
#       否则 `UpdateParameters.update_det_params` 访问 det_dict['model_path'] 直接 KeyError。
# 坑 2：踩错时**不报错也不生效** —— limit 仍是 736，白测一堆数据看不出来。
# 坑 3（决定性）：把 limit 从 736 降到 480 单次调用确实快 4.1x（1307ms→321ms），
#       但**小字号会稳定误识**，直接把验收项打掉 —— 型号码 YCWA15NCWQ 定点对照：
#         limit=736 dpi=300 → 'YCWA15NCWQ' 精确命中
#         limit=480 dpi=200/300/400 → 'YCWATSNCWQ' / 'YCWA15NCWO' 全部错字
#       结论：这是「用召回换时延」的伪优化，本模块不接受。
#       提速要走「少切几个无用区域 / 空结果才重试」的思路，不要牺牲识别精度。
DEFAULT_DET_LIMIT_SIDE_LEN = None

_OCR = None


def _get_ocr(det_limit_side_len: int | None = DEFAULT_DET_LIMIT_SIDE_LEN):
    """延迟 + 单例持有 RapidOCR（首次加载 ~1.4s，勿在模块导入时做）。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        kwargs: dict[str, Any] = {}
        if det_limit_side_len:
            try:
                from rapidocr_onnxruntime.utils import root_dir
                from pathlib import Path

                det_model = Path(root_dir) / "models" / "ch_PP-OCRv3_det_infer.onnx"
                if det_model.exists():
                    kwargs["det_limit_side_len"] = det_limit_side_len
                    kwargs["det_model_path"] = str(det_model)
            except Exception:
                kwargs = {}
        _OCR = RapidOCR(**kwargs)
    return _OCR


# ---------------------------------------------------------------- 几何部分


class _UF:
    """按网格加速的并查集（相邻 3x3 单元格合并）。"""

    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra

    def groups(self) -> list[list[int]]:
        g: dict[int, list[int]] = {}
        for i in range(len(self.p)):
            g.setdefault(self.find(i), []).append(i)
        return list(g.values())


def _cluster_points(points: Sequence[tuple[float, float]], eps: float) -> list[list[int]]:
    if not points:
        return []
    uf = _UF(len(points))
    cell: dict[tuple[int, int], list[int]] = {}
    for i, (cx, cy) in enumerate(points):
        cell.setdefault((int(cx / eps), int(cy / eps)), []).append(i)
    for (gx, gy), idxs in cell.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in cell.get((gx + dx, gy + dy), ()):
                    uf.union(idxs[0], j)
    return uf.groups()


def _overlaps(a, b, pad: float = 0.0) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def _to_rect(nb) -> tuple[float, float, float, float]:
    """norm_bbox(x,y,w,h) → (x0,y0,x1,y1)。"""
    return (nb[0], nb[1], nb[0] + nb[2], nb[1] + nb[3])


def find_char_regions(
    page_obj: dict,
    *,
    max_size: float = DEFAULT_MAX_SIZE,
    min_items: int = DEFAULT_MIN_ITEMS,
    cluster_eps: float = DEFAULT_CLUSTER_EPS,
    merge_eps: float = DEFAULT_MERGE_EPS,
    span_pad: float = DEFAULT_SPAN_PAD,
    min_cluster: int = DEFAULT_MIN_CLUSTER,
) -> tuple[list[dict], dict]:
    """找出「疑似转曲文字区」——纯几何，不渲染不 OCR。

    返回 `(regions, stats)`。region 已排除与文字层 span 交叠的簇，
    因此下游 OCR 不可能读到文字层已有内容 —— 这是「零误触发」的结构性保证。
    """
    paths = (page_obj.get("graphics") or {}).get("paths") or []
    span_bb = [_to_rect(s["norm_bbox"]) for s in (page_obj.get("text_spans") or [])]

    def is_char(p: dict) -> bool:
        nb = p.get("norm_bbox") or [0, 0, 1, 1]
        return p.get("n_items", 0) >= min_items and nb[2] <= max_size and nb[3] <= max_size

    chars = [p for p in paths if is_char(p)]
    pts = [
        (p["norm_bbox"][0] + p["norm_bbox"][2] / 2, p["norm_bbox"][1] + p["norm_bbox"][3] / 2)
        for p in chars
    ]

    raw = [g for g in _cluster_points(pts, cluster_eps) if len(g) >= min_cluster]
    cands: list[tuple[float, float, float, float]] = []
    cand_members: list[list[int]] = []
    n_rejected = 0
    for g in raw:
        xs = [chars[i]["norm_bbox"][0] for i in g] + [chars[i]["norm_bbox"][0] + chars[i]["norm_bbox"][2] for i in g]
        ys = [chars[i]["norm_bbox"][1] for i in g] + [chars[i]["norm_bbox"][1] + chars[i]["norm_bbox"][3] for i in g]
        bb = (min(xs), min(ys), max(xs), max(ys))
        if any(_overlaps(bb, sb, span_pad) for sb in span_bb):
            n_rejected += 1
            continue
        cands.append(bb)
        cand_members.append(g)

    ctr = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in cands]
    regions: list[dict] = []
    for gi, grp in enumerate(_cluster_points(ctr, merge_eps)):
        xs = [cands[i][0] for i in grp] + [cands[i][2] for i in grp]
        ys = [cands[i][1] for i in grp] + [cands[i][3] for i in grp]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        regions.append(
            {
                "id": f"r{gi:03d}",
                "norm_bbox": [
                    round(x0, 6), round(y0, 6),
                    round(max(x1 - x0, 0.0), 6), round(max(y1 - y0, 0.0), 6),
                ],
                "n_chars": sum(len(cand_members[i]) for i in grp),
                "n_lines": len(grp),
            }
        )

    stats = {
        "paths_total": len(paths),
        "char_like_paths": len(chars),
        "clusters": len(raw),
        "cluster_candidates": len(cands),
        "rejected_by_span_overlap": n_rejected,
        "regions": len(regions),
    }
    return regions, stats


# ---------------------------------------------------------------- OCR 部分


def _render_views(page, dpis: Sequence[int]) -> dict[int, Any]:
    import io

    from PIL import Image

    views = {}
    for dpi in dpis:
        t0 = time.perf_counter()
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
        views[dpi] = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        views[dpi].info["render_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return views


def _pad_image(im, min_margin: int):
    from PIL import Image

    m = max(0, min_margin - min(im.size))
    if m == 0:
        return im, 0
    bg = Image.new("RGB", (im.width + 2 * m, im.height + 2 * m), "white")
    bg.paste(im, (m, m))
    return bg, m


def run_texts_for_regions(
    page,
    regions: Sequence[dict],
    *,
    dpis: Sequence[int] = DEFAULT_DPIS,
    pad: float = DEFAULT_PAD,
    min_margin: int = DEFAULT_MIN_MARGIN,
    span_boxes: Sequence[tuple[float, float, float, float]] = (),
    drop_span_overlap: bool = True,
    span_pad: float = DEFAULT_SPAN_PAD,
    retry_empty: bool = True,
) -> tuple[list[dict], dict]:
    """对区域做局部渲染 + OCR。

    `retry_empty`：主分辨率读到文本即停，**只有空结果才升级到下一个分辨率**。
    双分辨率投票的价值在于补漏而不是提精度，实测 5/5 与 4/4 的单 dpi 结果已等价，
    因此默认只在主分辨率失败时才付第二份钱（实测省 ≈50% 耗时，recall 不变）。
    每条文本的 `norm_bbox` 已换算回**页面归一化坐标**，可直接供标示层使用。
    """
    import numpy as np

    ocr = _get_ocr()
    t_render = time.perf_counter()
    views = _render_views(page, dpis)
    render_ms = {str(d): im.info.get("render_ms") for d, im in views.items()}
    render_total_ms = round((time.perf_counter() - t_render) * 1000, 1)

    texts: list[dict] = []
    dup_dropped = 0
    ocr_calls = 0
    ocr_ms = 0.0
    per_region: list[dict] = []
    for reg in regions:
        x0, y0, w, h = reg["norm_bbox"]
        x1, y1 = x0 + w, y0 + h
        t_reg = time.perf_counter()
        # 同一文本常被两个 dpi 都读到 —— 按文本去重，保留置信度更高的那条
        best: dict[str, dict] = {}
        for i, dpi in enumerate(dpis):
            if retry_empty and i > 0 and best:
                break
            img = views[dpi]
            W, H = img.size
            left = int(max(0.0, x0 - pad) * W)
            top = int(max(0.0, y0 - pad) * H)
            right = int(min(1.0, x1 + pad) * W)
            bottom = int(min(1.0, y1 + pad) * H)
            crop = img.crop((left, top, right, bottom))
            crop, m = _pad_image(crop, min_margin)
            t1 = time.perf_counter()
            res, _ = ocr(np.array(crop))
            ocr_ms += (time.perf_counter() - t1) * 1000
            ocr_calls += 1
            for item in res or []:
                box, txt = item[0], item[1]
                conf = float(item[2]) if len(item) > 2 else 0.0
                if not txt:
                    continue
                pts = [(float(p[0]) - m + left, float(p[1]) - m + top) for p in box]
                nx0 = max(0.0, min(p[0] for p in pts) / W)
                ny0 = max(0.0, min(p[1] for p in pts) / H)
                nx1 = min(1.0, max(p[0] for p in pts) / W)
                ny1 = min(1.0, max(p[1] for p in pts) / H)
                if drop_span_overlap and any(_overlaps((nx0, ny0, nx1, ny1), sb, span_pad) for sb in span_boxes):
                    dup_dropped += 1
                    continue
                key = txt.strip()
                prev = best.get(key)
                if prev is None or conf > prev["conf"]:
                    best[key] = {
                        "id": f"{reg['id']}-t{len(best):02d}",
                        "region_id": reg["id"],
                        "text": key,
                        "conf": round(conf, 4),
                        "dpi": dpi,
                        "norm_bbox": [
                            round(nx0, 6), round(ny0, 6),
                            round(max(nx1 - nx0, 0.0), 6), round(max(ny1 - ny0, 0.0), 6),
                        ],
                    }
        per_region.append({
            "id": reg["id"], "ms": round((time.perf_counter() - t_reg) * 1000, 1),
            "texts": len(best),
        })
        texts.extend(best.values())

    stats = {
        "ocr_calls": ocr_calls,
        "dpi_used": list(dpis),
        "texts": len(texts),
        "dropped_as_span_duplicate": dup_dropped,
        "render_ms": render_ms,
        "render_total_ms": render_total_ms,
        "ocr_total_ms": round(ocr_ms, 1),
        "per_region": per_region,
    }
    return texts, stats


# ---------------------------------------------------------------- 顶层入口


def run_fallback(
    pdf_path: str | None,
    *,
    doc_obj: dict | None = None,
    page_index: int = 0,
    dpis: Sequence[int] = DEFAULT_DPIS,
    with_ocr: bool = True,
    max_size: float = DEFAULT_MAX_SIZE,
    min_items: int = DEFAULT_MIN_ITEMS,
    cluster_eps: float = DEFAULT_CLUSTER_EPS,
    merge_eps: float = DEFAULT_MERGE_EPS,
    span_pad: float = DEFAULT_SPAN_PAD,
    min_cluster: int = DEFAULT_MIN_CLUSTER,
    pad: float = DEFAULT_PAD,
    min_margin: int = DEFAULT_MIN_MARGIN,
    drop_span_overlap: bool = True,
) -> dict:
    """对单页执行区域触发式视觉兜底，返回 vpdf-fallback/1 JSON。

    `doc_obj` 允许传入已解析好的 vpdf/1 文档（须含 graphics），避免重复解析
    —— C# 侧已经解析过一次，直接复用可省掉 ~1.4s。
    """
    if fitz is None:
        raise RuntimeError("pymupdf not installed")
    if doc_obj is None and not pdf_path:
        raise ValueError("pdf_path 与 doc_obj 至少要给一个")

    from .reader import read_pdf

    t0 = time.perf_counter()
    doc = doc_obj if doc_obj is not None else read_pdf(pdf_path, with_graphics=True)
    if not doc.get("pages"):
        raise RuntimeError("no page")
    page_obj = doc["pages"][page_index]

    regions, geo = find_char_regions(
        page_obj,
        max_size=max_size, min_items=min_items, cluster_eps=cluster_eps,
        merge_eps=merge_eps, span_pad=span_pad, min_cluster=min_cluster,
    )

    texts: list[dict] = []
    ocr_stats: dict = {}
    if with_ocr and regions:
        fdoc = fitz.open(pdf_path)
        try:
            page = fdoc[page_index]
            try:
                # 与 reader 保持一致：坐标统一到显示空间。
                # 含 Ink 批注的图纸会抛 MuPDF 异常，实测不影响数据，直接吞掉。
                page.remove_rotation()
            except Exception:
                pass
            span_boxes = [_to_rect(s["norm_bbox"]) for s in (page_obj.get("text_spans") or [])]
            texts, ocr_stats = run_texts_for_regions(
                page, regions, dpis=dpis, pad=pad, min_margin=min_margin,
                span_boxes=span_boxes, drop_span_overlap=drop_span_overlap,
                span_pad=span_pad,
            )
        finally:
            fdoc.close()

    src = doc.get("source") or {}
    return {
        "schema": SCHEMA,
        "generator": {"lib": "vpdf", "version": VERSION},
        "source": {"file": pdf_path or src.get("file"), "page": page_index},
        "page": {
            "index": page_index,
            "canonical_size_pt": page_obj.get("canonical_size_pt"),
            "text_layer": page_obj.get("text_layer"),
            "regions": regions,
            "texts": texts,
        },
        "diagnostics": {
            "geometry": geo,
            "ocr": ocr_stats,
            "drop_span_overlap": drop_span_overlap,
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }
