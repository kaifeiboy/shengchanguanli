# -*- coding: utf-8 -*-
"""坐标系归一化 —— v2 感知层的坐标基准。

【2026-09-11 修正：本模块首版设计基于错误前提，已推翻重写】

错误前提：
    首版假设「page.rect 的宽高就是文字 bbox 所在坐标系的宽高」，
    并据此把竖版页强制顺时针旋转 90° 归一到横向。

实测事实（4 份样本，E:\\生产打标效果图）：
    page.rotation      = 270
    page.mediabox      = (0, 0, 595, 842)     <- 原始 PDF 空间（竖版 A4）
    page.rect          = (0, 0, 842, 595)     <- 显示空间（横版）
    get_text("dict")   span bbox x: 11..580   y: 8.6..844.58   <- 仍在**原始空间**
    remove_rotation()  span bbox x: 8.6..844  y: 14.6..583.7   <- 显示空间
    line dir          (0,1) -> (1,0)          <- 文字由竖排变横排

结论（两个独立角度交叉验证一致）：
    1. PyMuPDF 所有 page.get_*() 返回的坐标位于**未经旋转的原始 PDF 空间**，
       与 page.rect（受 /Rotate 影响）不是同一坐标系；
    2. 调用 page.remove_rotation() 后坐标即落入显示空间，且 page.rect 不变。

因此正确做法是：**先 remove_rotation()，再以 page.rect 作为标准坐标系**，
而不是自行用宽高比较去猜旋转。

【标准坐标系（canonical）定义】
    canonical = 人眼实际看到的显示空间：
        - 调用 page.remove_rotation() 之后的 page.rect
        - 原点左上，x 向右，y 向下，单位 pt
        - 不做「强制横向」——2 份 10 寸屏图纸是**真实的竖版图纸**，
          强行旋转会把它们转错方向。
    页面朝向（orientation）作为元数据记录，供渲染/H5 按需处理。

【归一化坐标】
    norm_bbox = [x, y, w, h]，各分量 ∈ [0, 1]，相对 canonical 宽高。
    用于跨渲染比例做位置映射；原始 pt 坐标同时保留，二者互为校验。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

Rect = Tuple[float, float, float, float]  # (x0, y0, x1, y1)


@dataclass(frozen=True)
class PageFrame:
    """一页的标准坐标系描述。"""

    width: float
    height: float
    page_rotation: int  # PDF /Rotate 原始值：0/90/180/270

    @property
    def orientation(self) -> str:
        if self.width > self.height:
            return "landscape"
        if self.width < self.height:
            return "portrait"
        return "square"

    @property
    def aspect(self) -> float:
        if self.height <= 0:
            return 0.0
        return round(self.width / self.height, 6)


def orientation_of(width: float, height: float) -> str:
    return PageFrame(width, height, 0).orientation


def clamp_rect(rect: Rect, W: float, H: float, pad: float = 0.0) -> Rect:
    """把矩形规范为 (minx, miny, maxx, maxy) 并裁剪到页内。

    必要性：字体 ascent/descent 会让 bbox 溢出页面边界
    （实测横版页 x 最大 844.58 > 842）。不裁剪会导致 norm_bbox 越界。
    """
    x0, y0, x1, y1 = rect
    ax0, ax1 = (x0, x1) if x0 <= x1 else (x1, x0)
    ay0, ay1 = (y0, y1) if y0 <= y1 else (y1, y0)
    lo_x, hi_x = -pad, W + pad
    lo_y, hi_y = -pad, H + pad
    return (
        min(max(ax0, lo_x), hi_x),
        min(max(ay0, lo_y), hi_y),
        min(max(ax1, lo_x), hi_x),
        min(max(ay1, lo_y), hi_y),
    )


def norm_bbox(rect: Rect, W: float, H: float, pad: float = 0.0):
    """标准坐标 -> 归一化 [x, y, w, h]，各分量保证落在 [0, 1]。"""
    x0, y0, x1, y1 = clamp_rect(rect, W, H, pad)
    if W <= 0 or H <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    x, y = x0 / W, y0 / H
    w, h = (x1 - x0) / W, (y1 - y0) / H
    return [
        round(min(max(x, 0.0), 1.0), 6),
        round(min(max(y, 0.0), 1.0), 6),
        round(min(max(w, 0.0), 1.0), 6),
        round(min(max(h, 0.0), 1.0), 6),
    ]


def denorm_rect(nb, W: float, H: float) -> Rect:
    """归一化 [x, y, w, h] -> 标准坐标 (x0, y0, x1, y1)。渲染/切块用。"""
    x, y, w, h = nb
    return (x * W, y * H, (x + w) * W, (y + h) * H)


def angle_from_dir(dx: float, dy: float) -> float:
    """由方向向量推算文字旋转角（度），规范到 [0, 360)。

    注：必须在 remove_rotation() 之后调用，否则会得到全页面统一的假角度
    （原始空间下横排文字的 dir 恒为 (0,1)）。
    """
    if dx == 0 and dy == 0:
        return 0.0
    a = math.degrees(math.atan2(dy, dx))
    return round(a % 360.0, 2)


def normalize_angle(angle: float) -> float:
    return round(float(angle) % 360.0, 2)


if __name__ == "__main__":
    ok = 0
    total = 0

    def check(name, got, expect):
        global ok, total
        total += 1
        good = got == expect
        ok += 1 if good else 0
        print(("PASS  " if good else "FAIL  ") + name, "got=", got, "expect=", expect)

    check("orientation landscape", orientation_of(842, 595), "landscape")
    check("orientation portrait", orientation_of(595, 842), "portrait")
    check("orientation square", orientation_of(600, 600), "square")

    # 溢出裁剪：x 最大 844.58 超过 842
    r = clamp_rect((8.6, 14.61, 844.58, 583.74), 842.0, 595.0)
    check("clamp x1", r[2], 842.0)

    # 顺序颠倒的矩形
    r2 = clamp_rect((30.0, 20.0, 10.0, 5.0), 842.0, 595.0)
    check("clamp reorder", r2, (10.0, 5.0, 30.0, 20.0))

    # 归一化不越界
    nb = norm_bbox((8.6, 14.61, 844.58, 583.74), 842.0, 595.0)
    check("norm in range", all(0.0 <= v <= 1.0 for v in nb), True)
    check("norm x", nb[0], round(8.6 / 842.0, 6))
    nb_full = norm_bbox((0, 0, 842, 595), 842.0, 595.0)
    check("norm full", nb_full, [0.0, 0.0, 1.0, 1.0])

    # 反变换
    check("denorm roundtrip", denorm_rect(nb_full, 842.0, 595.0), (0.0, 0.0, 842.0, 595.0))

    # 角度：remove_rotation 后横排文字 dir=(1,0) -> 0
    check("angle horizontal", angle_from_dir(1.0, 0.0), 0.0)
    check("angle down", angle_from_dir(0.0, 1.0), 90.0)
    check("angle up", angle_from_dir(0.0, -1.0), 270.0)
    check("angle left", angle_from_dir(-1.0, 0.0), 180.0)

    print()
    print("%d/%d passed" % (ok, total))
