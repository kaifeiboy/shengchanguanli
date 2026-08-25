# -*- coding: utf-8 -*-
"""生成 docx 分析报告：A4 竖向，标题 + 柱状图（图片）+ 数据明细（原生可编辑表格）+ 底部图例。
数据表用 python-docx 原生表格 → 用户可直接编辑；A4 竖向可直接打印。"""
import io, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams
from docx import Document
from docx.shared import Pt, Cm, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

_FONT_REGISTERED = False


def _register_zh_font():
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    candidates = [r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"]
    for f in candidates:
        if os.path.exists(f):
            try:
                font_manager.fontManager.addfont(f)
                _FONT_REGISTERED = True
                break
            except Exception:
                continue
    if _FONT_REGISTERED:
        try:
            prop = font_manager.FontProperties(fname=candidates[0])
            rcParams["font.sans-serif"] = [prop.get_name(), "DejaVu Sans"]
            rcParams["axes.unicode_minus"] = False
        except Exception:
            pass


_register_zh_font()

MODEL_COLORS_RGBA = ["#4472C4", "#ED7D31", "#70AD47", "#FFC000", "#7030A0",
                     "#E84C3D", "#2E9BD9", "#C00000", "#00B0F0", "#7F7F7F"]


def _wrap_label(name, max_units=8):
    """按估算宽度折行（中文≈2 单位、英文≈1 单位），返回多行文本（\n 分隔），保证完整显示不截断。"""
    def w(s):
        return sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    parts = []
    cur = ""
    for ch in name:
        if cur and w(cur + ch) > max_units:
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return "\n".join(parts) if parts else ""


def _render_chart_png(title, lines, models, data):
    """只渲染柱状图（不含标题、不含数据表）→ PNG bytes。
    版式：堆叠柱（X=拉线，柱内按型号分段着色）；每个拉线的型号说明（色块+型号名，完整显示、
    超宽自动折行）放置在该拉线正下方（不再使用集中式图例，去除其它多余说明文本）。
    标题由 docx 段落承载（文档仅保留一个大标题），图表内不再重复。"""
    fig = plt.figure(figsize=(8.27, 6.6), dpi=150)
    fig.set_tight_layout(False)
    # 主 axes（柱状图）+ 下方透明 axes（柱下：拉线名 + 型号色块说明）
    ax = fig.add_axes((0.10, 0.38, 0.85, 0.56))
    n = len(lines)
    color_of = {m: MODEL_COLORS_RGBA[i % len(MODEL_COLORS_RGBA)] for i, m in enumerate(models)}
    group_data = {}
    for d in data:
        group_data.setdefault(d["line"], {})[d["model"]] = d["total"]
    max_total = max((sum(g.values()) for g in group_data.values()), default=1)
    y_max = max(5, ((max_total // 5) + 1) * 5)
    x = list(range(n))
    bottoms = [0] * n
    for m in models:
        vals = [group_data.get(line, {}).get(m, 0) for line in lines]
        ax.bar(x, vals, 0.55, bottom=bottoms, color=color_of[m], edgecolor="white", linewidth=0.5)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax.set_xticks([])
    ax.set_ylabel("单数", fontsize=10)
    ax.set_ylim(0, y_max)
    ax.set_yticks(range(0, y_max + 1, max(1, y_max // 5)))
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    # 柱顶总单数标签
    for i, line in enumerate(lines):
        total = sum(group_data.get(line, {}).values())
        if total > 0:
            ax.text(i, total + y_max * 0.01, str(total), ha="center", va="bottom", fontsize=9, fontweight="bold")
    # 下方透明 axes：每个拉线正下方 = 拉线名 + 该拉线型号（色块+名称，完整显示可折行）
    # 先算每拉线标注总行数（拉线名折行 + 各型号折行），动态定 ylim
    per_line = []
    for line in lines:
        items = [m for m in models if group_data.get(line, {}).get(m, 0) > 0]
        rows = len(_wrap_label(line, 12).split("\n"))
        for m in items:
            rows += len(_wrap_label(m, 10).split("\n"))
        per_line.append(max(1, rows))
    max_rows = max(per_line)
    ax2 = fig.add_axes((0.10, 0.02, 0.85, 0.34))
    ax2.set_xlim(-0.5, n - 0.5)
    ax2.set_ylim(-0.3, max_rows + 0.6)
    ax2.axis("off")
    for i, line in enumerate(lines):
        items = [m for m in models if group_data.get(line, {}).get(m, 0) > 0]
        # 拉线名（顶部，可折行）
        lname = _wrap_label(line, 12)
        ax2.text(i, max_rows + 0.35, lname, ha="center", va="center", fontsize=9,
                 fontweight="bold", color="#334155", linespacing=1.2)
        # 型号 色块 + 名称（每行独立定位，超宽折行，缩小色块与文本间距）
        y = max_rows - 0.3
        for m in items:
            wrapped = _wrap_label(m, 10)
            subs = wrapped.split("\n")
            ax2.add_patch(plt.Rectangle((i - 0.30, y - 0.15), 0.20, 0.30,
                                        facecolor=color_of[m], edgecolor="none"))
            for li, ln in enumerate(subs):
                ax2.text(i - 0.04, y - li * 0.34, ln, ha="left", va="center",
                         fontsize=7.5, color="#475569")
            y -= len(subs) * 0.58
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches=None, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _set_cell_bg(cell, hexcolor):
    """设置单元格底纹（浅蓝表头）"""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hexcolor)
    tcPr.append(shd)


def _write_analysis_docx(out, title, lines, models, data):
    """生成 docx：A4 竖向，标题 + 柱状图 + 可编辑数据表 + 底部图例。"""
    doc = Document()
    # A4 竖向 + 页边距
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    # 默认字体（中文）
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # 标题（28 号加粗居中，文档唯一大标题）
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(title)
    run.font.size = Pt(28)
    run.font.bold = True

    # 柱状图
    png = _render_chart_png(title, lines, models, data)
    pic_p = doc.add_paragraph()
    pic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = pic_p.add_run()
    run.add_picture(io.BytesIO(png), width=Cm(16.5))

    # 数据明细标题
    p = doc.add_paragraph()
    p.space_before = Pt(10)
    run = p.add_run("数据明细（按拉线 × 型号）")
    run.font.bold = True
    run.font.size = Pt(12)

    # 可编辑数据表
    table_lines = ["拉线", "型号", "IPQC", "QA", "合计"]
    cell_text = [[d["line"], d["model"], str(d["ipqc"]), str(d["qa"]), str(d["total"])] for d in data]
    if not cell_text:
        cell_text = [["(无数据)", "", "", "", ""]]
    tbl = doc.add_table(rows=1 + len(cell_text), cols=5)
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    # 表头
    for c, h in enumerate(table_lines):
        cell = tbl.cell(0, c)
        cell.text = h
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in cell.paragraphs[0].runs:
            run.font.bold = True
        _set_cell_bg(cell, "DDEBF7")
    # 数据
    for ri, row in enumerate(cell_text, start=1):
        for ci, v in enumerate(row):
            cell = tbl.cell(ri, ci)
            cell.text = str(v)
            cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
            if ri % 2 == 0:
                _set_cell_bg(cell, "F2F7FC")
    # 列宽
    widths = [Cm(2.6), Cm(5.6), Cm(2.4), Cm(2.4), Cm(2.4)]
    for ri in range(len(tbl.rows)):
        for ci, w in enumerate(widths):
            tbl.cell(ri, ci).width = w

    os.makedirs(os.path.dirname(out), exist_ok=True) if os.path.dirname(out) else None
    doc.save(out)
    return out
