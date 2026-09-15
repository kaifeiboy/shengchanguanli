# -*- coding: utf-8 -*-
"""vpdf —— 矢量 PDF 原生解析独立库（激光打标首件对比系统 v2 · perception 层）。

设计原则（对应方案 §1 与 v2 架构「感知 / 决策分离」）：
  1. **只做感知**：输出中性 JSON（文字 span / 图像 / 矢量图形 + 坐标 + 方向 + 置信标记），
     绝不包含「哪些是打标对象」「合格与否」之类的业务判定。
  2. **与业务解耦**：不依赖 drawings 任何旧代码，可独立运行、独立测试、独立版本化。
  3. **可追溯**：输出携带 schema 版本、生成器版本、PyMuPDF 版本、文件 SHA256 与耗时。

CLI：
    python -m vpdf.cli parse  <file.pdf> [-o out.json] [--with-graphics]
    python -m vpdf.cli sweep  <dir> -o <outdir>
    python -m vpdf.cli selfcheck
"""
from . import normalize
from .normalize import PageFrame, clamp_rect, denorm_rect, norm_bbox, orientation_of
from .reader import (
    MIN_WORDS_FOR_TEXT_LAYER,
    SCHEMA,
    VERSION,
    read_page,
    read_pdf,
    sha256_file,
)

__all__ = [
    "SCHEMA",
    "VERSION",
    "MIN_WORDS_FOR_TEXT_LAYER",
    "read_pdf",
    "read_page",
    "sha256_file",
    "normalize",
    "PageFrame",
    "orientation_of",
    "clamp_rect",
    "norm_bbox",
    "denorm_rect",
]
