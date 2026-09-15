# -*- coding: utf-8 -*-
"""vpdf 命令行入口。

    python -m vpdf.cli parse  <file.pdf> [-o out.json] [--with-graphics] [--max-graphics-items N]
    python -m vpdf.cli sweep  <dir> -o <outdir> [--with-graphics]
    python -m vpdf.cli selfcheck
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from . import normalize
from .reader import MIN_WORDS_FOR_TEXT_LAYER, SCHEMA, VERSION, read_pdf


def _dump(obj, out_path: str | None, indent: int = 2) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    return text


def cmd_parse(a: argparse.Namespace) -> int:
    obj = read_pdf(
        a.pdf,
        with_graphics=a.with_graphics,
        max_graphics_items=a.max_graphics_items,
    )
    text = _dump(obj, a.out, a.indent)

    # --json-only：stdout 只吐纯 JSON，供 C# 桥接层直接消费。
    # 默认是给人看的（逐页摘要行 + "-> out"），两者不能混用。
    if getattr(a, "json_only", False):
        print(text)
        return 0

    for p in obj["pages"]:
        tl = p["text_layer"]
        print(
            f"{obj['source']['file_name']}: page{p['index']} "
            f"words={tl['word_count']} spans={tl['span_count']} "
            f"layer={'vector' if tl['present'] else 'NONE(fallback)'} "
            f"{p['diagnostics']['parse_ms']}ms"
        )
    if a.out:
        print(f"-> {a.out}")
    return 0


def cmd_sweep(a: argparse.Namespace) -> int:
    pattern = os.path.join(a.dir, "**", "*.pdf")
    files = sorted(set(glob.glob(pattern, recursive=True)))
    if not files:
        print(f"no pdf under {a.dir}")
        return 1

    os.makedirs(a.out, exist_ok=True)
    rows = []
    ok = 0
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(a.out, name + ".vpdf.json")
        try:
            obj = read_pdf(path, with_graphics=a.with_graphics)
            _dump(obj, out_path, a.indent)
            p0 = obj["pages"][0] if obj["pages"] else None
            if p0 is None:
                raise RuntimeError("no page")
            rows.append(
                {
                    "file": obj["source"]["file_name"],
                    "sha256": obj["source"]["sha256"][:12],
                    "pages": obj["source"]["page_count"],
                    "canonical": p0["canonical_size_pt"],
                    "orient": p0["orientation"],
                    "page_rotation": p0["page_rotation"],
                    "words": p0["text_layer"]["word_count"],
                    "spans": p0["text_layer"]["span_count"],
                    "images": len(p0["images"]),
                    "text_layer": p0["text_layer"]["present"],
                    "fallback": p0["diagnostics"]["vision_fallback_required"],
                    "parse_ms": p0["diagnostics"]["parse_ms"],
                    "out": os.path.basename(out_path),
                }
            )
            ok += 1
        except Exception as e:  # 单份失败不阻断全量
            rows.append({"file": os.path.basename(path), "error": f"{type(e).__name__}: {e}"})

    vec = sum(1 for r in rows if r.get("text_layer"))
    n = len(rows)
    total_ms = sum(r.get("parse_ms", 0) or 0 for r in rows)
    summary = {
        "schema": SCHEMA,
        "vpdf_version": VERSION,
        "dir": a.dir,
        "total": n,
        "parsed_ok": ok,
        "vector_layer": vec,
        "needs_vision_fallback": n - vec,
        "vector_rate": round(100.0 * vec / n, 1) if n else 0.0,
        "avg_parse_ms": round(total_ms / max(ok, 1), 1),
        "rows": rows,
    }
    _dump(summary, os.path.join(a.out, "_sweep_summary.json"), a.indent)

    print(f"{'file':<52}{'pg':>3}{'words':>7}{'span':>6}{'img':>5}{'rot':>5}{'ms':>8}  orient/layer")
    for r in rows:
        if "error" in r:
            print(f"{r['file'][:52]:<52}  ERROR {r['error']}")
            continue
        print(
            f"{r['file'][:52]:<52}{r['pages']:>3}{r['words']:>7}{r['spans']:>6}"
            f"{r['images']:>5}{r['page_rotation']:>5}{r['parse_ms']:>8}  "
            f"{r['orient']}/{'VECTOR' if r['text_layer'] else 'FALLBACK'}"
        )
    print()
    print(
        f"TOTAL={n} ok={ok} VECTOR={vec} FALLBACK={n - vec} "
        f"rate={summary['vector_rate']}%  avg={summary['avg_parse_ms']}ms/页"
    )
    print(f"-> {os.path.join(a.out, '_sweep_summary.json')}")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    """坐标合法性验收闸：扫描 sweep 产物目录，断言所有坐标合法。

    这是 M1 的回归门禁 —— 2026-09-11 曾因「拿 page.rect 宽高去猜旋转」
    导致 norm_bbox 越界（y0=1.22）。此后任何改动都必须过这一关。
    """
    files = sorted(
        f for f in os.listdir(a.dir) if f.endswith(".vpdf.json") and not f.startswith("_")
    )
    if not files:
        print(f"no *.vpdf.json under {a.dir}")
        return 1

    n_span = n_img = 0
    violations: list[str] = []
    for name in files:
        with open(os.path.join(a.dir, name), "r", encoding="utf-8") as f:
            doc = json.load(f)
        for pg in doc["pages"]:
            W, H = pg.get("canonical_size_pt") or (0, 0)
            for s in pg.get("text_spans", []):
                n_span += 1
                nb, b = s["norm_bbox"], s["bbox"]
                if not all(0.0 <= v <= 1.0 for v in nb):
                    violations.append("%s %s norm range %s" % (name, s["id"], nb))
                if nb[0] + nb[2] > 1.000001 or nb[1] + nb[3] > 1.000001:
                    violations.append("%s %s norm sum %s" % (name, s["id"], nb))
                if b[0] < -0.01 or b[1] < -0.01 or b[2] > W + 0.01 or b[3] > H + 0.01:
                    violations.append("%s %s bbox out of page %s / %s" % (name, s["id"], b, [W, H]))
                if b[2] < b[0] or b[3] < b[1]:
                    violations.append("%s %s bbox unordered %s" % (name, s["id"], b))
            for im in pg.get("images", []):
                n_img += 1
                if not all(0.0 <= v <= 1.0 for v in im["norm_bbox"]):
                    violations.append("%s %s img norm %s" % (name, im["id"], im["norm_bbox"]))

    print("files=%d  spans=%d  images=%d  violations=%d" % (len(files), n_span, n_img, len(violations)))
    for v in violations[:20]:
        print("  VIOLATION " + v)
    if violations:
        return 1
    print("OK: all coordinates valid")
    return 0


def cmd_selfcheck(_: argparse.Namespace) -> int:
    """坐标系归一化自检（显示空间标准坐标系，不做强制横向旋转）。

    断言依据见 normalize 模块头部：PyMuPDF 的 get_* 坐标位于未旋转的原始空间，
    必须先 remove_rotation() 再取 page.rect 作为标准坐标系。
    """
    checks = [
        ("orientation landscape", normalize.orientation_of(842, 595) == "landscape"),
        ("orientation portrait", normalize.orientation_of(595, 842) == "portrait"),
        ("orientation square", normalize.orientation_of(600, 600) == "square"),
        # 竖版真实存在（2 份 10 寸屏图纸），不得被强行旋转
        ("portrait stays portrait", normalize.norm_bbox((0, 0, 595, 842), 595.0, 842.0) == [0.0, 0.0, 1.0, 1.0]),
        ("landscape norm full", normalize.norm_bbox((0, 0, 842, 595), 842.0, 595.0) == [0.0, 0.0, 1.0, 1.0]),
        # 字体 ascent 溢出必须被裁剪（实测 x1=844.58 > 842）
        ("clamp overflow", normalize.clamp_rect((8.6, 14.61, 844.58, 583.74), 842.0, 595.0)[2] == 842.0),
        ("clamp reorder", normalize.clamp_rect((30.0, 20.0, 10.0, 5.0), 842.0, 595.0) == (10.0, 5.0, 30.0, 20.0)),
        ("norm never out of range", all(0.0 <= v <= 1.0 for v in normalize.norm_bbox((8.6, 14.61, 844.58, 583.74), 842.0, 595.0))),
        ("denorm roundtrip", normalize.denorm_rect([0.0, 0.0, 1.0, 1.0], 842.0, 595.0) == (0.0, 0.0, 842.0, 595.0)),
        ("angle horizontal", normalize.angle_from_dir(1, 0) == 0.0),
        ("angle down", normalize.angle_from_dir(0, 1) == 90.0),
        ("angle up = 270", normalize.angle_from_dir(0, -1) == 270.0),
        ("angle left = 180", normalize.angle_from_dir(-1, 0) == 180.0),
    ]

    bad = 0
    for name, okc in checks:
        print(("PASS  " if okc else "FAIL  ") + name)
        if not okc:
            bad += 1
    print(f"\n{len(checks) - bad}/{len(checks)} passed")
    # C# 桥接层（V2Python.ExtractJson）从 stdout 切 JSON —— 必须以 JSON 行收尾，
    # 否则 health 端点会拿到纯文本并报「未输出 JSON」（M4 实测遗留问题）
    import json as _json
    print(_json.dumps({
        "schema": "vpdf-selfcheck/1",
        "ok": bad == 0,
        "passed": len(checks) - bad,
        "total": len(checks),
    }, ensure_ascii=False))
    return 0 if bad == 0 else 1


def cmd_render(a: argparse.Namespace) -> int:
    """渲染 PDF 指定页为 PNG，stdout 输出二进制字节。

    供 H5 可视化图纸回显使用。DPI 默认 150（屏幕可读，文件 ~200KB）。
    """
    import sys as _sys
    try:
        import fitz
    except ImportError:
        print("pymupdf not installed", file=_sys.stderr)
        return 1

    if not os.path.isfile(a.pdf):
        print(f"pdf not found: {a.pdf}", file=_sys.stderr)
        return 1

    doc = fitz.open(a.pdf)
    try:
        pno = a.page
        if pno < 0 or pno >= doc.page_count:
            print(f"page out of range: {pno}/{doc.page_count}", file=_sys.stderr)
            return 1
        page = doc[pno]
        # 渲染为 PNG 字节，直接写入 stdout 二进制
        pix = page.get_pixmap(dpi=a.dpi)
        png_bytes = pix.tobytes("png")
        _sys.stdout.buffer.write(png_bytes)
        return 0
    finally:
        doc.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vpdf", description="矢量 PDF 原生解析（v2 perception 层）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("parse", help="解析单份 PDF")
    p1.add_argument("pdf")
    p1.add_argument("-o", "--out", default=None)
    p1.add_argument("--with-graphics", action="store_true", help="启用矢量图形提取（慢，+1.4s/页）")
    p1.add_argument("--max-graphics-items", type=int, default=None)
    p1.add_argument("--indent", type=int, default=2)
    p1.add_argument("--json-only", action="store_true",
                    help="stdout 只输出纯 JSON（供 C# 桥接层消费），不打印人工摘要行")
    p1.set_defaults(func=cmd_parse)

    p2 = sub.add_parser("sweep", help="批量解析目录下所有 PDF")
    p2.add_argument("dir")
    p2.add_argument("-o", "--out", required=True)
    p2.add_argument("--with-graphics", action="store_true")
    p2.add_argument("--indent", type=int, default=2)
    p2.set_defaults(func=cmd_sweep)

    p3 = sub.add_parser("verify", help="坐标合法性验收闸（扫 sweep 产物目录）")
    p3.add_argument("dir")
    p3.set_defaults(func=cmd_verify)

    p4 = sub.add_parser("selfcheck", help="归一化数学自检")
    p4.set_defaults(func=cmd_selfcheck)

    p5 = sub.add_parser("render", help="渲染 PDF 指定页为 PNG（stdout 二进制）")
    p5.add_argument("pdf")
    p5.add_argument("--page", type=int, default=0)
    p5.add_argument("--dpi", type=int, default=150)
    p5.set_defaults(func=cmd_render)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
