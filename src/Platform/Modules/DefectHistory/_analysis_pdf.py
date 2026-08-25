# -*- coding: utf-8 -*-
"""生成 PDF 分析报告：A4 竖向，含图表+数据表+图例，可直接打印。"""
import io, sys, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams

# 中文字体（Windows 自带，显式 addfont 否则中文显示为方框）
_FONT_REGISTERED = False
def _register_zh_font():
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    candidates = [
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
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
            zh_name = prop.get_name()
            rcParams["font.sans-serif"] = [zh_name, "DejaVu Sans"]
            rcParams["axes.unicode_minus"] = False
        except Exception:
            pass
_register_zh_font()

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# 与 H5 Canvas 配色一致
MODEL_COLORS_RGBA = ["#4472C4", "#ED7D31", "#70AD47", "#FFC000", "#7030A0",
                      "#E84C3D", "#2E9BD9", "#C00000", "#00B0F0", "#7F7F7F"]


def _render_analysis_png(title, lines, models, data, page_w_px=1240, page_h_px=1754):
    """用 matplotlib 渲染一整页 A4 图表 → PNG bytes。
    page_w_px/page_h_px：A4 比例 1:√2 ≈ 1:1.414，150dpi 时 1240×1754。"""
    fig = plt.figure(figsize=(8.27, 11.69), dpi=150)  # A4
    # 关闭 tight_layout 自动布局：手动精确管理 axes + table 位置避免重叠
    fig.set_tight_layout(False)
    # 标题
    fig.suptitle(title, fontsize=15, fontweight="bold", y=0.97)
    # 柱状图区（上半部分）
    ax = fig.add_axes((0.12, 0.50, 0.83, 0.40))
    n = len(lines)
    color_of = {m: MODEL_COLORS_RGBA[i % len(MODEL_COLORS_RGBA)] for i, m in enumerate(models)}
    # 按拉线分组堆叠
    group_data = {}
    for d in data:
        group_data.setdefault(d["line"], {})[d["model"]] = d["total"]
    max_total = max((sum(g.values()) for g in group_data.values()), default=1)
    y_max = max(5, ((max_total // 5) + 1) * 5)
    bar_w = 0.55
    x = list(range(n))
    bottoms = [0] * n
    for m in models:
        vals = [group_data.get(line, {}).get(m, 0) for line in lines]
        ax.bar(x, vals, bar_w, bottom=bottoms, color=color_of[m], label=m, edgecolor="white", linewidth=0.5)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax.set_xticks(x)
    ax.set_xticklabels(lines, rotation=0, fontsize=9)
    ax.set_ylabel("单数", fontsize=10)
    ax.set_xlabel("拉线", fontsize=10)
    ax.set_ylim(0, y_max)
    ax.set_yticks(range(0, y_max + 1, max(1, y_max // 5)))
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    # 顶部数据标签
    for i, line in enumerate(lines):
        total = sum(group_data.get(line, {}).values())
        if total > 0:
            ax.text(i, total + y_max * 0.01, str(total), ha="center", va="bottom", fontsize=9, fontweight="bold")
    # 图例（型号顺序）
    handles = [plt.Rectangle((0, 0), 1, 1, color=color_of[m]) for m in models]
    ax.legend(handles, models, loc="upper center", bbox_to_anchor=(0.5, -0.10), ncol=min(4, len(models)),
              fontsize=8, frameon=False, title="型号图例", title_fontsize=8)
    plt.draw()
    # 数据表（独立 axes 位置，用 fig.add_axes 手动精确控制避免重叠）
    # 隐藏坐标轴，仅渲染表格
    ax_table = fig.add_axes((0.05, 0.02, 0.90, 0.46))
    ax_table.axis("off")
    table_lines = ["拉线", "型号", "IPQC", "QA", "合计"]
    cell_text = []
    for d in data:
        cell_text.append([d["line"], d["model"], str(d["ipqc"]), str(d["qa"]), str(d["total"])])
    if not cell_text:
        cell_text = [["(无数据)", "", "", "", ""]]
    tbl = ax_table.table(cellText=cell_text, colLabels=table_lines, cellLoc="center",
                         colWidths=[0.16, 0.42, 0.13, 0.13, 0.16], loc="upper center",
                         rowLoc="center")
    if tbl is not None:
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        for (r, c), cell in tbl.get_celld().items():
            cell.set_edgecolor("#cccccc")
            if r == 0:
                cell.set_facecolor("#ddebf7")
                cell.set_text_props(fontweight="bold")
            elif r % 2 == 0:
                cell.set_facecolor("#f8fafc")
    # "数据明细" 标题放在 table 上方（图与表之间的间隙）
    fig.text(0.5, 0.495, "数据明细（按拉线 × 型号）", ha="center", fontsize=11, fontweight="bold")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.5)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor("#cccccc")
        if r == 0:
            cell.set_facecolor("#ddebf7")
            cell.set_text_props(fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor("#f8fafc")
    # 底部文本图例（按拉线对应的型号与单数）
    legend_lines = []
    for line in lines:
        parts = [f"{d['model']}：{d['total']}单" for d in data if d["line"] == line]
        if parts:
            legend_lines.append(f"【{line}】" + " · ".join(parts))
    if legend_lines:
        legend_text = "拉线—型号单数：" + "    ".join(legend_lines)
        plt.figtext(0.05, 0.005, legend_text, ha="left", fontsize=7, color="#555555", wrap=True)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches=None, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _write_analysis_pdf(out, title, lines, models, data):
    """生成 PDF：A4 竖向单页（含图表 + 数据表 + 图例），无需编辑可直接打印。"""
    png_bytes = _render_analysis_png(title, lines, models, data)
    c = canvas.Canvas(out, pagesize=A4)
    page_w, page_h = A4
    margin = 10 * mm
    # 渲染 PNG 到页面（保持 A4 比例）
    img = ImageReader(io.BytesIO(png_bytes))
    iw, ih = img.getSize()
    avail_w = page_w - 2 * margin
    avail_h = page_h - 2 * margin
    ratio = min(avail_w / iw, avail_h / ih)
    draw_w = iw * ratio
    draw_h = ih * ratio
    x = (page_w - draw_w) / 2
    y = page_h - margin - draw_h
    c.drawImage(img, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask='auto')
    c.showPage()
    c.save()
    return out