#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
segment_blocks.py - 图纸切图（auto-paddlev3 自动识别 + 舍弃规则）
=========================================================

【现行切图规则（2026-07-27 定型，auto-paddlev3）】
  用 PP-DocLayoutV3 版面检测（ONNX 推理，绕开本机 CPU 无 AVX2）自动识别图纸上的
  部件视图图块，再叠加「只舍弃、不合并」规则滤掉表格/文字/标题及低置信碎块，
  得到干净的匹配候选图。本规则已【完全替换】旧的「红框/标记」人工圈定规则。

实现要点：
  1) 渲染高 DPI 彩图（默认 300，保证清晰度；C# 生产调用传 200）。
  2) 调 pp_doclayout_onnx.detect()（经 segment_blocks_auto.detect_auto_regions）
     取版面 image/figure 块；detect() 内部 4 步纯舍弃：
        候选构建 → 过度覆盖舍弃(丢大框留子视图) → 逐块舍弃(分数/几何/QR/尺寸) → 子检测冗余舍弃。
     全程不合并相邻独立视图（anti-merge，仅丢弃不合规块）。
  3) 每个区域从渲染彩图裁切（高 DPI），写出小图块 PNG + manifest JSON。
  4) 若模型不可用 / 无有效块：产出 blocks=[] 的空 manifest，不回退红框。
     （红框规则已彻底移除；生产环境模型已验证覆盖全部在用图纸。）

子命令：
  render  <pdf> <did> <out_dir> [dpi]   仅渲染完整页为 {did}_full.png
  segment <pdf> <did> <out_dir> [dpi]   自动识别图块 + 切小图块 + 写 manifest

输出（与 C# EnsureSegmentedCore 解析契约保持一致）：
  <out_dir>/{did}_full.png                     完整页原图
  <out_dir>/blocks/{did}_blk_{NN}.png         每个小图块（高 DPI）
  <out_dir>/blocks/{did}_blocks.json          小图块清单（bbox + 相对路径 + idx）

依赖：PyMuPDF(fitz) + Pillow + numpy + onnxruntime。不调用 Tesseract / Paddle。
"""
import sys
import os
import json

import numpy as np
from PIL import Image
import fitz  # PyMuPDF

DPI = 300  # 默认清晰度


def norm_path(p):
    """把 Git-Bash 风格路径 /e/work/... 归一成 Windows 盘符 e:/work/...，
    避免 Python 子进程把前导 /e/ 当成 POSIX 根目录 -> FileNotFoundError。"""
    if not p:
        return p
    if len(p) > 2 and p[0:1] == "/" and p[2:3] == "/":
        drive = p[1:2]
        if drive.isalpha():
            return drive + ":/" + p[3:]
    return p


def render_page(pdf_path, out_png, dpi=DPI):
    """把 PDF 首页渲染为彩色 PNG（不改动原始 PDF）。返回 (PIL.Image, w, h)。"""
    doc = fitz.open(pdf_path)
    try:
        page = doc[0]
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img.save(out_png)
        return img, pix.width, pix.height
    finally:
        doc.close()


def _dedup_subdetections(regions, contain_thresh=0.70):
    """去重子检测 + 反向包含（防大框吞并独立视图）。

    两阶段：
    阶段1（正向）：若较小块 > contain_thresh 面积被包含在较大块内，暂标记为子检测。
    阶段2（反向）：若某大框内部包含 ≥2 个被丢弃的小框、且这些小框的并集面积
             占大框面积 > FRAG_COVERAGE_RATIO → 大框是模型的过度覆盖伪检，
             丢弃大框、恢复小框（如 VK01 #1 的「侧视图+正面板」大框）。

    与合并逻辑的关键区别：
    - 不合并相邻独立产品视图（如 VK01 的正面图与侧面图保持独立）
    - 仅移除模型对同一视图产生的重复/子区域检测（sub-detection noise）
    适用场景：auto-paddlev3 路径（模型已含 NMS，但低分边界块仍有子检测残留）。
    """
    FRAG_COVERAGE_RATIO = 0.65   # 小框并集占大框面积>此值→大框为伪检
    FRAG_MIN_INNER = 2           # 大框内至少几个小框才触发反向

    if len(regions) <= 1:
        return list(regions)

    # 按面积降序排列（大块优先处理）
    tagged = [(r, (r[2] - r[0]) * (r[3] - r[1])) for r in regions]
    tagged.sort(key=lambda x: x[1], reverse=True)

    keep = []       # [(box, area), ...] 确认保留
    discarded = []  # [(box, area), ...] 被大框吃掉的小框

    for box, area in tagged:
        is_sub = False
        for kept_box, kept_area in keep:
            ix0 = max(box[0], kept_box[0])
            iy0 = max(box[1], kept_box[1])
            ix1 = min(box[2], kept_box[2])
            iy1 = min(box[3], kept_box[3])
            iw = max(0, ix1 - ix0)
            ih = max(0, iy1 - iy0)
            inter = iw * ih
            # 较小块的 >contain_thresh 面积落在已保留的大块内 → 暂判为子检测
            if area > 0 and inter >= contain_thresh * area:
                is_sub = True
                break
        if is_sub:
            discarded.append((box, area))
        else:
            keep.append((box, area))

    # ── 阶段2：反向包含检测 ──
    # 对每个保留的大框，检查它是否"吞掉了"≥2 个本应独立的小框
    revived = []   # 从 discarded 中恢复的小框
    drop_big = set()  # 要丢弃的大框索引

    for ki, (big_box, big_area) in enumerate(keep):
        inner = []  # 被 big_box 吃掉的子框
        for dbox, darea in discarded:
            ix0 = max(dbox[0], big_box[0])
            iy0 = max(dbox[1], big_box[1])
            ix1 = min(dbox[2], big_box[2])
            iy1 = min(dbox[3], big_box[3])
            iw = max(0, ix1 - ix0)
            ih = max(0, iy1 - iy0)
            inter = iw * ih
            if darea > 0 and inter >= contain_thresh * darea:
                inner.append((dbox, darea))

        if len(inner) < FRAG_MIN_INNER:
            continue

        # 计算内部小框的并集面积（简化：用累加交大框面积，允许重叠）
        union_in_big = 0
        for ibox, _ in inner:
            ix0 = max(ibox[0], big_box[0])
            iy0 = max(ibox[1], big_box[1])
            ix1 = min(ibox[2], big_box[2])
            iy1 = min(ibox[3], big_box[3])
            union_in_big += max(0, ix1 - ix0) * max(0, iy1 - iy0)

        if big_area > 0 and union_in_big >= FRAG_COVERAGE_RATIO * big_area:
            # 大框是过度覆盖伪检 → 标记丢弃，恢复内部小框
            drop_big.add(ki)
            revived.extend(inner)

    # 组装最终结果
    result = [box for i, (box, _) in enumerate(keep) if i not in drop_big]
    result.extend([box for box, _ in revived])
    return result


def segment(pdf_path, did, out_dir, dpi=DPI):
    """auto-paddlev3 自动识别切图（红框规则已移除，无红框回退）。"""
    os.makedirs(out_dir, exist_ok=True)
    blocks_dir = os.path.join(out_dir, "blocks")
    os.makedirs(blocks_dir, exist_ok=True)

    full_png = os.path.join(out_dir, f"{did}_full.png")
    color, W, H = render_page(pdf_path, full_png, dpi)
    gray = np.asarray(color.convert("L"))

    # ── auto-paddlev3：PP-DocLayoutV3 ONNX + 舍弃规则 ──
    #    detect_auto_regions 内部即 pp.detect()，已完成「只舍弃不合并」。
    #    无有效块（模型不可用/图纸无视图）时 regions=None → 空 manifest，不回退红框。
    regions = None
    mode = "auto-none"
    try:
        import segment_blocks_auto as sba
        regions = sba.detect_auto_regions(pdf_path, color, gray, W, H, dpi)
    except Exception as e:
        sys.stderr.write(f"[segment] auto rule error: {e}\n")

    if regions:
        mode = "auto-paddlev3"
    else:
        regions = []
        sys.stderr.write("[segment] auto-paddlev3 返回空，本图纸无有效图块\n")

    algorithm = "v7-auto-paddlev3"

    # ── 从原彩图裁切（高 DPI，已渲染即此分辨率）──
    pending = []
    for r in regions:
        x0, y0, x1, y1 = [int(v) for v in r]
        pending.append({
            "box": (x0, y0, x1, y1),
            "x": x0, "y": y0,
            "w": x1 - x0, "h": y1 - y0,
        })

    # ── 排序：从上到下、从左到右 ──
    pending.sort(key=lambda b: (b["y"], b["x"]))

    blocks = []
    for k, pb in enumerate(pending):
        crop = color.crop(pb["box"])
        # 注：红框去除逻辑(_remove_red_frame)已随红框规则一并移除；
        #     现行 auto 管线识别的是图纸部件视图，不含人工红框线。
        bname = f"{did}_blk_{k:02d}.png"
        crop.save(os.path.join(blocks_dir, bname))
        blocks.append({
            "idx": k,
            "x": pb["x"], "y": pb["y"],
            "w": pb["w"], "h": pb["h"],
            "file": f"blocks/{bname}",
            # 自动识别命中的区域一律视为应保留（参与匹配）
            "type": "engineering",
        })

    manifest = {
        "drawingId": int(did),
        "source": os.path.basename(full_png),
        "width": int(W),
        "height": int(H),
        "blocks": blocks,
        "algorithm": algorithm,
        "mode": mode,
    }
    json_path = os.path.join(blocks_dir, f"{did}_blocks.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return manifest


def main():
    if len(sys.argv) < 4:
        print(json.dumps({
            "ok": False,
            "error": ("usage: segment_blocks.py <render|segment> "
                      "<pdf> <did> <out_dir> [dpi]")
        }))
        sys.exit(2)

    cmd = sys.argv[1]
    pdf = norm_path(sys.argv[2])
    did = sys.argv[3]
    out_dir = norm_path(sys.argv[4])
    dpi = int(sys.argv[5]) if len(sys.argv) > 5 else DPI

    # ── 自建硬超时看门狗（纪律修复：杜绝被外部强杀毒化 Paddle 会话）──
    # 正常切图数秒完成；若本进程（卡在 ONNX 推理等）超过 100s 仍未退出，
    # 由 worker 自身强制结束，使调用方（C#）永远无需 p.Kill()。
    import threading
    _hard_exit = threading.Timer(100.0, lambda: os._exit(1))
    _hard_exit.daemon = True
    _hard_exit.start()
    try:
        if cmd == "render":
            render_page(pdf, os.path.join(out_dir, f"{did}_full.png"), dpi)
            print(json.dumps({"ok": True, "full": f"{did}_full.png"}))
        elif cmd == "segment":
            manifest = segment(pdf, did, out_dir, dpi)
            print(json.dumps(manifest))
        else:
            print(json.dumps({"ok": False, "error": "unknown cmd: " + cmd}))
            sys.exit(2)
    finally:
        _hard_exit.cancel()


if __name__ == "__main__":
    main()
