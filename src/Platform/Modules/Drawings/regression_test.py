#!/usr/bin/env python3
"""回归测试基线 — 在改动 diff_visualizer.py 前后运行，确保核心功能不退化。

用法:
    python regression_test.py           # 运行全部测试
    python regression_test.py --verbose # 详细输出
    
测试场景:
  1. 编译检查: import diff_visualizer 不报语法错误
  2. 基本 diff: 两个简单文本串的字符级 diff 正确
  3. photo_text_override: override 模式不抛异常
  4. 空白过滤: 空格不产生 false positive
  5. 块缓存兼容: 新旧缓存格式下不抛异常
  6. 图标区域兼容: 非文字区域不被标为差异
"""

import sys
import os
import json
import traceback

# ── 配置 ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)  # 保证 relative data/ 路径可用

PASS = 0
FAIL = 0
ERRORS = []


def test(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        if "--verbose" in sys.argv:
            print(f"  ✅ {name}")
    except Exception as e:
        FAIL += 1
        err = f"  ❌ {name}: {e}"
        print(err)
        traceback.print_exc()
        ERRORS.append(err)


# ════════════════════════════════════════════
# Test 1: 编译检查
# ════════════════════════════════════════════
def test_import():
    import diff_visualizer
    assert hasattr(diff_visualizer, "run"), "diff_visualizer 缺少 run()"
    assert callable(diff_visualizer.run), "run() 不可调用"
    # 检查所有关键函数存在
    for fn_name in ["run", "_ocr_image", "_fast_text_detection", "_map_override_to_ocr"]:
        assert hasattr(diff_visualizer, fn_name), f"缺少 {fn_name}()"


# ════════════════════════════════════════════
# Test 2: 基本 diff — 两个文本串的字符级 diff
# ════════════════════════════════════════════
def test_basic_diff():
    from diff_visualizer import run
    # 用空图（最小 1x1 白图）做占位
    from PIL import Image
    tmp_dir = os.path.join(PROJECT_ROOT, "..", "..", "..", "data", "seg", "_test")
    os.makedirs(tmp_dir, exist_ok=True)
    dummy_png = os.path.join(tmp_dir, "dummy.png")
    Image.new("RGB", (100, 100), (255, 255, 255)).save(dummy_png)
    out_png = os.path.join(tmp_dir, "out.png")
    try:
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=None,
                block_text_override="LOW VOLTAGE\n禁止强电",
                photo_text_override="LOW VOLTAGE\n禁止强电")
        assert isinstance(r, dict), f"run() 应返回 dict, 实际={type(r)}"
        assert r.get("success") == True, f"success 应为 True, 实际={r.get('success')}"
        # 文本相同 => 0 red
        assert len(r.get("redRegions", [])) == 0, f"相同文本不应有 red region"
        assert len(r.get("greenRegions", [])) > 0, f"应有 green region"
    finally:
        try: os.remove(dummy_png)
        except: pass
        try: os.remove(out_png)
        except: pass


# ════════════════════════════════════════════
# Test 3: photo_text_override 模式不抛异常
# ════════════════════════════════════════════
def test_override_mode():
    from diff_visualizer import run
    from PIL import Image
    tmp_dir = os.path.join(PROJECT_ROOT, "..", "..", "..", "data", "seg", "_test")
    os.makedirs(tmp_dir, exist_ok=True)
    dummy_png = os.path.join(tmp_dir, "dummy2.png")
    Image.new("RGB", (100, 100), (255, 255, 255)).save(dummy_png)
    out_png = os.path.join(tmp_dir, "out2.png")
    try:
        # 模拟有差异的场景
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=None,
                block_text_override="ABC\nDEF\nGHI",
                photo_text_override="ABC\nGXX")
        assert isinstance(r, dict), f"run() 应返回 dict"
        # 文本不同，应该有差异
        assert len(r.get("blockExclusive", [])) > 0 or len(r.get("photoExclusive", [])) > 0 or len(r.get("redRegions", [])) > 0, "文本不同应有差异"
    finally:
        try: os.remove(dummy_png)
        except: pass
        try: os.remove(out_png)
        except: pass


# ════════════════════════════════════════════
# Test 4: 空白/标点过滤 — 空格不产生 false positive
# ════════════════════════════════════════════
def test_whitespace_filter():
    from diff_visualizer import run
    from PIL import Image
    tmp_dir = os.path.join(PROJECT_ROOT, "..", "..", "..", "data", "seg", "_test")
    os.makedirs(tmp_dir, exist_ok=True)
    dummy_png = os.path.join(tmp_dir, "dummy3.png")
    Image.new("RGB", (100, 100), (255, 255, 255)).save(dummy_png)
    out_png = os.path.join(tmp_dir, "out3.png")
    try:
        # block="LOW VOLTAGE"(含空格), photo="LOW"/"VOLTAGE"(拆成两行)
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=None,
                block_text_override="LOW VOLTAGE",
                photo_text_override="LOW\nVOLTAGE")
        assert isinstance(r, dict), f"run() 应返回 dict"
        missing = r.get("missingChars", [])
        for m in missing:
            missing_chars = m.get("missing", "")
            # 缺失字符不能全是空白
            assert not all(c.isspace() or c in ',.;:、，。；：' for c in missing_chars), \
                f"空白差异不应报告为 missingChars: {missing_chars!r}"
    finally:
        try: os.remove(dummy_png)
        except: pass
        try: os.remove(out_png)
        except: pass


# ════════════════════════════════════════════
# Test 5: 块缓存兼容 — 新旧格式下不抛异常
# ════════════════════════════════════════════
def test_block_cache_compat():
    from diff_visualizer import run
    from PIL import Image
    tmp_dir = os.path.join(PROJECT_ROOT, "..", "..", "..", "data", "seg", "_test")
    os.makedirs(tmp_dir, exist_ok=True)
    dummy_png = os.path.join(tmp_dir, "dummy4.png")
    Image.new("RGB", (100, 100), (255, 255, 255)).save(dummy_png)
    out_png = os.path.join(tmp_dir, "out4.png")
    
    # 测试 1: list[dict] 缓存（新格式）
    new_cache = [{"text": "ABC", "bbox": [[0,0],[10,0],[10,10],[0,10]], "conf": 0.9}]
    try:
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=new_cache,
                block_text_override="ABC",
                photo_text_override="ABC")
        assert isinstance(r, dict), "list[dict] 缓存不应抛异常"
    except Exception as e:
        raise AssertionError(f"list[dict] 缓存失败: {e}")
    
    # 测试 2: dict 缓存（旧格式——兼容路径，应正常降级到 OCR）
    old_cache = {"success": True, "redRegions": []}
    try:
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=old_cache,
                block_text_override="ABC",
                photo_text_override="ABC")
        assert isinstance(r, dict), "dict 缓存不应抛异常"
    except Exception as e:
        raise AssertionError(f"dict 缓存失败: {e}")
    
    try: os.remove(dummy_png)
    except: pass
    try: os.remove(out_png)
    except: pass


# ════════════════════════════════════════════
# Test 6: 图标区域兼容 — 非文字区域不影响文本 diff
# ════════════════════════════════════════════
def test_icon_compat():
    """保证将来加了图标检测后，纯文本 diff 功能不退化"""
    from diff_visualizer import run
    from PIL import Image
    tmp_dir = os.path.join(PROJECT_ROOT, "..", "..", "..", "data", "seg", "_test")
    os.makedirs(tmp_dir, exist_ok=True)
    # 创建带"图标区域"的假块图（含一个非文字的小矩形）
    dummy_png = os.path.join(tmp_dir, "dummy5.png")
    img = Image.new("RGB", (200, 100), (255, 255, 255))
    # 加一个矩形（模拟图标）
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 30, 30], fill=(100, 100, 100))
    img.save(dummy_png)
    out_png = os.path.join(tmp_dir, "out5.png")
    try:
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=None,
                block_text_override="ABC",
                photo_text_override="ABC")
        assert isinstance(r, dict), "图标区域兼容测试失败"
    finally:
        try: os.remove(dummy_png)
        except: pass
        try: os.remove(out_png)
        except: pass


# ════════════════════════════════════════════
# Test 7: 图标框 override（A2）— 传入图标框跳过检测且正确回传
# ════════════════════════════════════════════
def test_icon_override():
    """A2：传入持久化图标框时跳过 _detect_icon_regions（免模型加载），
    且 iconRegions 正确回传（独立于 grayRegions，紫色绘制）。"""
    from diff_visualizer import run
    from PIL import Image
    tmp_dir = os.path.join(PROJECT_ROOT, "..", "..", "..", "data", "seg", "_test")
    os.makedirs(tmp_dir, exist_ok=True)
    dummy_png = os.path.join(tmp_dir, "dummy7.png")
    Image.new("RGB", (200, 100), (255, 255, 255)).save(dummy_png)
    out_png = os.path.join(tmp_dir, "out7.png")
    try:
        fake_icons = [[10, 10, 20, 20], [50, 50, 15, 15]]
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=None,
                block_text_override="ABC",
                photo_text_override="ABC",
                icon_regions_override=fake_icons)
        assert isinstance(r, dict), "icon override 不应抛异常"
        assert r.get("success") is True, f"success 应为 True, 实际={r.get('success')}"
        ir = r.get("iconRegions", [])
        assert isinstance(ir, list), "iconRegions 应为 list"
        # ⭐ 图标现在独立在 iconRegions（不再并入 grayRegions）
        flat_icons = [list(map(int, g[:4])) for g in ir]
        for ic in fake_icons:
            ic4 = list(map(int, ic[:4]))
            assert ic4 in flat_icons, f"图标框 {ic4} 应出现在 iconRegions，实际 iconRegions={flat_icons}"
        # grayRegions 不应包含图标（已分离）
        flat_gray = [list(map(int, g[:4])) for g in r.get("grayRegions", [])]
        for ic in fake_icons:
            ic4 = list(map(int, ic[:4]))
            assert ic4 not in flat_gray, f"图标框 {ic4} 不应再出现在 grayRegions（已独立为紫色）"
    finally:
        try: os.remove(dummy_png)
        except: pass
        try: os.remove(out_png)
        except: pass


# ════════════════════════════════════════════
# 运行
# ════════════════════════════════════════════
# ════════════════════════════════════════════
# Test 8: 图标框 override 为空列表 []（已检测无图标）— 必须跳过检测，不可误判为未提供
# ════════════════════════════════════════════
def test_icon_override_empty():
    """A2 边界：持久化图标框为 []（表示"已检测、无图标"）时，run() 应据此跳过
    _detect_icon_regions（免重复加载 RapidOCR 模型），而非误判未提供去重检测。"""
    from diff_visualizer import run
    from PIL import Image
    tmp_dir = os.path.join(PROJECT_ROOT, "..", "..", "..", "data", "seg", "_test")
    os.makedirs(tmp_dir, exist_ok=True)
    dummy_png = os.path.join(tmp_dir, "dummy8.png")
    Image.new("RGB", (200, 100), (255, 255, 255)).save(dummy_png)
    out_png = os.path.join(tmp_dir, "out8.png")
    try:
        r = run(dummy_png, dummy_png, out_png,
                block_bbox_cache=None,
                block_text_override="ABC",
                photo_text_override="ABC",
                icon_regions_override=[])
        assert isinstance(r, dict), "empty icon override 不应抛异常"
        assert r.get("success") is True, f"success 应为 True, 实际={r.get('success')}"
        assert r.get("iconRegions") == [], f"iconRegions 应为 [], 实际={r.get('iconRegions')}"
    finally:
        try: os.remove(dummy_png)
        except: pass
        try: os.remove(out_png)
        except: pass


if __name__ == "__main__":
    print(f"回归测试基线 — {len(sys.argv) > 1 and sys.argv[1] or ''}")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  工作目录: {PROJECT_ROOT}\n")

    test("编译检查", test_import)
    test("基本 diff（相同文本=0 red）", test_basic_diff)
    test("photo_text_override 模式", test_override_mode)
    test("空白过滤（空格不产生 false positive）", test_whitespace_filter)
    test("块缓存兼容（新旧格式）", test_block_cache_compat)
    test("图标区域兼容", test_icon_compat)
    test("图标框 override（A2 持久化）", test_icon_override)
    test("图标框 override 为空[]（跳过检测）", test_icon_override_empty)

    print(f"\n{'─' * 40}")
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    if ERRORS:
        for e in ERRORS:
            print(e)
    sys.exit(0 if FAIL == 0 else 1)
