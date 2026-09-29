# -*- coding: utf-8 -*-
"""图纸侧逻辑图块（产品单一部位）提取 —— Phase 1 核心。

设计依据：docs/打标首件对比V2_图纸侧部位提取方案_定稿.md
核心命题：图纸每个打标部位是独立的；图块 = 文本 span + 图像对象（QR/logo）
按空间间距聚类出的区域（逻辑概念，非 V1 物理切块）。不依赖矢量引线几何
（真实 21 份：A 栅格化 17 / B 原生矢量 2 / C 文字转曲 2，矢量引线均不可提）。

流程：
  1. 取 canonical 坐标系（page.remove_rotation() 后的 page.rect，见 normalize.py）。
  2. 取文本 span（A/B 类来自文本层；C 类来自渲染200DPI+RapidOCR）+ 图像对象 rect。
  3. 并查集按间距 thr_pt 聚类 → 候选块。
  4. 噪声三步过滤：标题栏满高窄列 / 技术要求整句块 / 会签栏部门名。
  5. 块内候选 = 真实打标内容（型号/服务热线/端子标识/QR...），去噪后输出。
  6. 块 anchor = 块内 QR/图像中心（优先），否则块中心。

本模块只读 PDF，不写回源文件。调用方负责落库（见 V2Store）。
"""
from __future__ import annotations

import re
import fitz
import numpy as np

from . import normalize as N

# 图名/代号（标题栏特有）：含「打标图纸/效果图/Model」+ 代号（如 JT-010-F-9075）
TITLE_NAME_RE = re.compile(r"(打标图纸|效果图|Model|MODEL)")
CODE_RE = re.compile(r"^[A-Za-z]{1,3}[-\s]?\d{2,}[-\sA-Za-z0-9]*$")
# 日期（会签/标题栏）：23.04.18 / 2025.7.21
DATE_RE = re.compile(r"\d{2,4}[.\-]\d{1,2}([.\-]\d{1,2})?")


# ---------------- 噪声判定（图纸特有） ----------------

# 标题栏字段名（GB 标准标题栏，会变但模式固定：窄列 + 满高 + 页缘）
TITLEBLOCK_WORD_HINTS = (
    "设计", "校对", "审核", "批准", "工艺", "标准化", "日期", "单位", "比例",
    "材料", "文件名", "图号", "图样", "版本", "阶段", "标记", "处数", "更改单号",
    "文件编号", "物料编码", "重量", "共", "第", "页",
)
# 会签栏部门名
SIGNBLOCK_DEPTS = (
    "技术部", "电子部", "品质工程部", "品质部", "结构部", "软件部", "硬件部",
    "项目部", "生产部", "采购部", "发放部门", "会签",
)
# 技术要求块头
TECH_REQ_HEADERS = ("技术要求", "说", "明", "注：", "注:")


def _is_dim_span(text: str) -> bool:
    """标线数字：纯数字 / 小数 / 含 ± 公差 / 单位缩写。"""
    import re
    t = text.strip()
    if not t:
        return False
    return bool(re.fullmatch(r"[0-9][0-9.xX±±\-\+/ ]*", t)) or bool(
        re.search(r"[0-9](\.[0-9]+)?\s*(±|±|mm|°|%|cm|kg)", t)
    )


def _looks_marking(text: str) -> bool:
    """疑似真实打标内容：含字母/中文产品词/服务热线/型号，而非纯说明。"""
    import re
    t = text.strip()
    if not t:
        return False
    if _is_dim_span(t):
        return True
    # 含字母数字混合（型号/MAC/端子）、或服务热线、或中文产品词
    if re.search(r"[A-Za-z]", t) and re.search(r"[0-9]", t):
        return True
    if "服务热线" in t or "热线" in t or "HTTP" in t.upper() or "WWW" in t.upper():
        return True
    if re.search(r"[一-鿿]", t) and len(t) >= 2 and not _is_pure_explanation(t):
        return True
    return False


def _is_pure_explanation(text: str) -> bool:
    """整句中文说明（技术要求/工艺注），非打标内容。"""
    import re
    t = text.strip()
    # 以动词/连词开头的说明句
    if re.match(r"^(上盖|最终|表面|不得|允许|按|当|若|如|见|图|无|所有|产品|字体)", t):
        return True
    if len(t) >= 12 and not re.search(r"[A-Za-z]{2,}", t):
        return True
    return False


def _block_is_titlebar(block_bbox_norm, page_w, page_h, items_texts) -> bool:
    """标题栏：① 满高窄列（占页高>80%、宽<25%页宽）贴页缘；或 ② 底部条带含图名/代号。"""
    x0, y0, x1, y1 = block_bbox_norm
    h_ratio = (y1 - y0)
    w_ratio = (x1 - x0)
    # 规则①：满高窄列贴页缘
    if h_ratio > 0.80 and w_ratio < 0.25 and ((x0 < 0.06) or (x1 > 0.94)):
        return True
    # 退路：块内大量标题栏字段名
    if sum(1 for t in items_texts if t in TITLEBLOCK_WORD_HINTS) >= 4:
        return True
    # 规则②：底部条带（页底 28% 内）含图名/代号 → 转曲图纸的标题栏常在底部
    if y0 > 0.72:
        has_name = any(TITLE_NAME_RE.search(t) for t in items_texts)
        has_code = any(CODE_RE.match(t.strip()) for t in items_texts)
        hint_hits = sum(1 for t in items_texts if t in TITLEBLOCK_WORD_HINTS)
        if has_name or has_code or hint_hits >= 2:
            return True
    return False


def _block_is_signblock(items_texts) -> bool:
    joined = " ".join(items_texts)
    # OCR 常把日期切成 "23. 04. 18"（带空格），去空格再判
    joined_ns = joined.replace(" ", "")
    if any(d in joined for d in SIGNBLOCK_DEPTS):
        return True
    # 会签栏：含日期 +（标准化 / 部门）+ 人名（2~3 字中文）
    if DATE_RE.search(joined_ns):
        if any(h in joined for h in ("标准化", "会签", "审核", "批准", "校对", "设计")):
            return True
        if re.search(r"[一-鿿]{2,3}", joined_ns):
            return True
    return False


def _block_is_techreq(items_texts) -> bool:
    joined = " ".join(items_texts)
    if any(h in joined for h in TECH_REQ_HEADERS):
        # 含技术要求头，且多整句中文、少打标 token
        marking = sum(1 for t in items_texts if _looks_marking(t))
        if marking <= 1:
            return True
    return False


# ---------------- 聚类 ----------------

def _gap(a, b):
    """两矩形 (x0,y0,x1,y1) 的轴对齐间隙；重叠则为 0。"""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return dx, dy


