# -*- coding: utf-8 -*-
"""vpdf.reader — 矢量 PDF 原生对象读取（perception 层）。

【职责边界】
只回答「页面上有什么、在哪、多大、朝哪个方向」，
**不回答「哪些是打标对象」** —— 那是 C# MarkBuilder 的决策层职责。

【为什么不 OCR】
方案 §1：矢量 PDF 的文字应从 PDF 原生字符对象直接提取，不使用整页 OCR。
实测 21 份图纸中 19 份具备原生文字层（80~167 words/页）；
仅 2 份为虚拟打印机转曲件（words=0），需走视觉兜底，由本模块给出标记。

【坐标系】
先 page.remove_rotation() 把坐标统一到显示空间，再以 page.rect 作为标准坐标系。
详见 normalize 模块头部 2026-09-11 修正说明 —— 切勿回到「按宽高比较猜旋转」的老路。

【性能】
get_drawings() 实测占整页解析耗时的 82%（单页 8,876~22,482 个矢量图元），
因此默认**不调用**，仅在显式请求（引线检测等）时才启用。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Iterable, Sequence

import fitz

from . import normalize

SCHEMA = "vpdf/1"
VERSION = "1.1.0"

# 判定「是否存在原生文字层」的最小词数阈值。
# 实测依据：19 份矢量图纸 80~167 words/页；2 份转曲件为 0。取 20 作安全分界。
MIN_WORDS_FOR_TEXT_LAYER = 20


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _to_frame(page) -> normalize.PageFrame:
    """把 page 的坐标系统一到显示空间，并返回标准坐标系描述。"""
    raw_rot = int(getattr(page, "rotation", 0) or 0)
    if raw_rot:
        try:
            page.remove_rotation()
        except Exception:
            pass
    r = page.rect
    return normalize.PageFrame(
        width=float(r.width), height=float(r.height), page_rotation=raw_rot
    )


def _text_spans(page, frame: normalize.PageFrame) -> list[dict]:
    """从原生文字层提取 span（同字体/字号/颜色的连续文本），带坐标与方向。"""
    spans: list[dict] = []
    try:
        data = page.get_text("dict") or {}
    except Exception:
        return spans

    W, H = frame.width, frame.height
    idx = 0
    for b_i, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:  # 0=文本块, 1=图像块
            continue
        for line in block.get("lines", []):
            dx, dy = (line.get("dir") or (1.0, 0.0))[:2]
            angle = normalize.angle_from_dir(float(dx), float(dy))
            for sp in line.get("spans", []):
                text = (sp.get("text") or "").strip()
                if not text:
                    continue
                rect = normalize.clamp_rect(tuple(sp.get("bbox") or (0, 0, 0, 0)), W, H)
                spans.append(
                    {
                        "id": f"t{idx:04d}",
                        "text": text,
                        "bbox": [round(v, 3) for v in rect],
                        "norm_bbox": normalize.norm_bbox(rect, W, H),
                        "font": sp.get("font"),
                        "size": round(float(sp.get("size") or 0.0), 2),
                        "dir": [round(float(dx), 4), round(float(dy), 4)],
                        "angle": angle,
                        "color": sp.get("color"),
                        "flags": sp.get("flags"),
                        "block_index": b_i,
                    }
                )
                idx += 1
    return spans


def _images(page, frame: normalize.PageFrame) -> list[dict]:
    """提取页面上的图像对象（二维码 / 图标候选）及其**落位矩形**。

    注意：不能用 page.get_images() —— 它只返回 xref 列表，不含放置位置；
    必须用 get_image_info(xrefs=True) 才能拿到 bbox。
    """
    imgs: list[dict] = []
    try:
        info_list = page.get_image_info(xrefs=True) or []
    except Exception:
        return imgs

    W, H = frame.width, frame.height
    for i, info in enumerate(info_list):
        rect = normalize.clamp_rect(tuple(info.get("bbox") or (0, 0, 0, 0)), W, H)
        imgs.append(
            {
                "id": f"i{i:03d}",
                "xref": info.get("xref"),
                "bbox": [round(v, 3) for v in rect],
                "norm_bbox": normalize.norm_bbox(rect, W, H),
                "width": info.get("width"),
                "height": info.get("height"),
                "colorspace_n": info.get("colorspace"),
                "bpc": info.get("bpc"),
                "has_alpha": bool(info.get("alpha")),
            }
        )
    return imgs


def _graphics(page, frame: normalize.PageFrame, max_items: int | None) -> dict:
    """矢量图形（路径）提取 —— 慢路径，仅按需调用。"""
    t0 = time.perf_counter()
    try:
        drawings = page.get_drawings() or []
    except Exception:
        drawings = []

    W, H = frame.width, frame.height
    paths: list[dict] = []
    total_items = 0
    for i, d in enumerate(drawings):
        rect = normalize.clamp_rect(tuple(d.get("rect") or (0, 0, 0, 0)), W, H)
        items = d.get("items") or []
        total_items += len(items)
        paths.append(
            {
                "id": f"g{i:04d}",
                "bbox": [round(v, 3) for v in rect],
                "norm_bbox": normalize.norm_bbox(rect, W, H),
                "type": d.get("type"),
                "n_items": len(items),
                "color": d.get("color"),
                "fill": d.get("fill"),
                "width": d.get("width"),
                "closed": bool(d.get("closePath")),
            }
        )
        if max_items is not None and total_items >= max_items:
            break

    return {
        "count": len(paths),
        "total_items": total_items,
        "truncated": max_items is not None and total_items >= max_items,
        "parse_ms": round((time.perf_counter() - t0) * 1000, 1),
        "paths": paths,
    }


def _silence_mupdf() -> None:
    """尽量静音 MuPDF 的 stderr 噪声。

    【实测结论（2026-09-11，PyMuPDF 1.28.0）】
    2 份图纸（02-PC-P1HJQ / 02-PC-P1HVQ）含 Ink 批注，remove_rotation() 时打印
        cannot set rect: code=4: Ink annotations have no Rect property
    该消息由 MuPDF C 层直写文件描述符 2，Python 层无法拦截：
      - contextlib.redirect_stderr 捕获长度为 0（说明不走 sys.stderr）
      - fitz.TOOLS.mupdf_display_errors(False) 对这一条无效
    数据影响：无。本库只读文字 / 图像 / 矢量图形，不读批注；
    且 verify 全量校验 1853 个 span 坐标零违规，证明 remove_rotation 未中断。

    【集成要求（v2 runner 必须遵守）】
    JSON 走 stdout，不受影响；但 stderr 仍会有极少量噪声（约 60 字节），
    C# 侧必须异步排空 StandardError，不得只 ReadToEnd() stdout。
    """
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass


def read_page(
    page,
    *,
    with_graphics: bool = False,
    max_graphics_items: int | None = None,
) -> dict:
    """读取单页，返回中性页面对象（坐标已归一到显示空间标准坐标系）。"""
    t0 = time.perf_counter()

    frame = _to_frame(page)
    W, H = frame.width, frame.height

    words = page.get_text("words") or []
    spans = _text_spans(page, frame)
    images = _images(page, frame)

    # 文字块数（用于与 span 数交叉校验）
    try:
        blocks = [b for b in (page.get_text("dict") or {}).get("blocks", []) if b.get("type") == 0]
    except Exception:
        blocks = []

    has_text_layer = len(words) >= MIN_WORDS_FOR_TEXT_LAYER

    out: dict[str, Any] = {
        "index": int(page.number),
        # 标准坐标系 = 显示空间尺寸
        "canonical_size_pt": [round(W, 2), round(H, 2)],
        "orientation": frame.orientation,
        "aspect": frame.aspect,
        # 可追溯：PDF 原始 /Rotate 值
        "page_rotation": frame.page_rotation,
        "text_layer": {
            "present": has_text_layer,
            "word_count": len(words),
            "text_block_count": len(blocks),
            "span_count": len(spans),
        },
        "text_spans": spans,
        "images": images,
        "graphics": None,
        "diagnostics": {
            "source": "pdf_vector" if has_text_layer else "needs_vision_fallback",
            "vision_fallback_required": not has_text_layer,
            "parse_ms": 0,
        },
    }

    if with_graphics:
        out["graphics"] = _graphics(page, frame, max_graphics_items)

    out["diagnostics"]["parse_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def read_pdf(
    path: str,
    *,
    with_graphics: bool = False,
    max_graphics_items: int | None = None,
    pages: Sequence[int] | None = None,
) -> dict:
    """读取 PDF，返回中性文档对象（vpdf/1 schema）。"""
    t0 = time.perf_counter()
    _silence_mupdf()

    try:
        file_hash = sha256_file(path)
    except Exception:
        file_hash = ""

    doc = fitz.open(path)
    try:
        meta = doc.metadata or {}
        targets: Iterable[int] = pages if pages is not None else range(doc.page_count)
        out_pages = []
        for pno in targets:
            if pno < 0 or pno >= doc.page_count:
                continue
            out_pages.append(
                read_page(
                    doc[pno],
                    with_graphics=with_graphics,
                    max_graphics_items=max_graphics_items,
                )
            )

        return {
            "schema": SCHEMA,
            "generator": {
                "lib": "vpdf",
                "version": VERSION,
                "pymupdf": getattr(fitz, "__version__", "?"),
            },
            "source": {
                "file": path,
                "file_name": path.replace("\\", "/").rsplit("/", 1)[-1],
                "sha256": file_hash,
                "page_count": doc.page_count,
                "format": meta.get("format"),
                "creator": meta.get("creator"),
                "producer": meta.get("producer"),
                "title": meta.get("title"),
            },
            "pages": out_pages,
            "diagnostics": {
                "with_graphics": with_graphics,
                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        }
    finally:
        doc.close()
