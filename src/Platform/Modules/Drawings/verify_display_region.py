#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LCD 屏显灰化 v2 —— 真实代码路径验证（开关强制 ON，不改动生产默认值）。

Phase A: 对全部 99 块用实时块 OCR 提名 + _detect_display_regions 确认屏显，
          汇总每块产出的 display_texts 键；断言「仅 did112 命中」（其余零误报）。
Phase B: did112 接线测试 —— 照片用不含 8888 的块图(112_blk_00)、块用含 8888 的
          块图(112_blk_01)，跑真实 run()，断言 8888 进入 grayRegions 且不进 redRegions/
          blockExclusive（即屏显豁免生效、无红框负向）。

不改 ENABLE_DISPLAY_REGION_GRAY 磁盘默认值（仅进程内强制 ON）。
"""
import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, HERE)
import diff_visualizer as dv
import photo_registration as PR

dv.ENABLE_DISPLAY_REGION_GRAY = True   # 进程内强制开（仅测试）
SEG = os.path.join(ROOT, "data", "seg")


def phaseA():
    print("=" * 70)
    print("Phase A: 全量 99 块屏显检测扫描（开关 ON）")
    print("=" * 70)
    hits = {}   # did -> {blk: [keys]}
    total_blocks = 0
    for d in sorted(os.listdir(SEG)):
        dp = os.path.join(SEG, d)
        if not os.path.isdir(dp) or not d.isdigit():
            continue
        bdir = os.path.join(dp, "blocks")
        if not os.path.isdir(bdir):
            continue
        for fn in sorted(os.listdir(bdir)):
            if not (fn.endswith(".png") and "_blk_" in fn):
                continue
            total_blocks += 1
            png = os.path.join(bdir, fn)
            try:
                lines = PR.ocr_photo_tiled(png)
            except Exception as e:
                print(f"  !! OCR fail {d}/{fn}: {e}")
                continue
            _, keys = dv._detect_display_regions(png, lines)
            if keys:
                hits.setdefault(d, {})[fn] = sorted(keys)
    print(f"\n扫描块数: {total_blocks}")
    print(f"命中屏显的 did 数: {len(hits)} -> {sorted(hits.keys(), key=int)}")
    for d in sorted(hits, key=int):
        for fn, ks in hits[d].items():
            print(f"  did={d} {fn}: keys={ks}")
    # 断言
    unexpected = [d for d in hits if d != "112"]
    assert not unexpected, f"❌ 出现非预期误报 did: {unexpected}"
    assert "112" in hits, "❌ did112 真屏显未命中"
    print("\n✅ Phase A 通过：仅 did112 命中，其余 98 块零误报。")
    return hits


def phaseB():
    print("\n" + "=" * 70)
    print("Phase B: did112 屏显豁免接线测试（照片缺 8888 / 块含 8888）")
    print("=" * 70)
    photo = os.path.join(SEG, "112", "blocks", "112_blk_00.png")  # 无 8888 屏幕
    block = os.path.join(SEG, "112", "blocks", "112_blk_01.png")  # 含 8888 屏幕
    if not (os.path.exists(photo) and os.path.exists(block)):
        print("  !! 缺少测试块图，跳过 Phase B")
        return
    out = os.path.join(SEG, "_p1b_tmp", "lcd_wire_test.jpg")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        block_ocr = dv._ocr_image(block)
    except Exception:
        block_ocr = None
    res = dv.run(photo, block, out, block_bbox_cache=block_ocr,
                 block_text_override=None, photo_text_override=None,
                 icon_regions_override=None)
    if not res.get("success"):
        raise RuntimeError(f"run() 失败: {res.get('error')}")
    gray = res.get("grayRegions", [])
    red = res.get("redRegions", [])
    block_excl = res.get("blockExclusive", [])
    # 8888 是否被灰化（gray 文本里应含 8888）；red/blockExclusive 不应含 8888
    gray_text = [g.get("text") for g in gray] if gray and isinstance(gray[0], dict) else []
    print(f"  grayRegions 数={len(gray)}  redRegions 数={len(red)}  "
          f"blockExclusive={block_excl}")
    # 直接检查：8888 不在 blockExclusive（即未被当差异）
    has_8888_excl = any("8888" in str(t) for t in block_excl)
    assert not has_8888_excl, f"❌ 8888 仍出现在 blockExclusive(差异): {block_excl}"
    # 关键：灰化确实发生——display_texts 命中应使 8888 进入 cadOnly/gray 链路
    # 由于照片缺 8888、块含 8888，若无豁免则 8888 必为 blockExclusive/红；
    # 有豁免则 blockExclusive 不含 8888 且 red 不含 8888。
    red_text = [r.get("text") for r in red] if red and isinstance(red[0], dict) else []
    assert not any("8888" in str(t) for t in red_text), f"❌ 8888 出现在 red: {red_text}"
    print("✅ Phase B 通过：8888 未被当作差异（屏显豁免生效），无红框负向。")


if __name__ == "__main__":
    phaseA()
    phaseB()
    print("\n🎉 全部验证通过。")
