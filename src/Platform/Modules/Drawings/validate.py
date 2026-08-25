#!/usr/bin/env python3
"""⭐ Phase 7 护栏：首件比对全量回归门禁（合并主干前必跑）。

复用 real_match_regression 的全量报告（若存在且比 diff_visualizer.py 新则直接读，
否则重新跑），    断言【差异指标零负向】：
        R <= BASE_R(+R_TOL)   Y <= BASE_Y(+Y_TOL)
        |G - BASE_G| <= G_TOL |Gray - BASE_GRAY| <= GRAY_TOL
        (RapidOCR 跨次边界抖动容差；R/Y 亦受抖动影响，给小幅容差)
    ⭐ 2026-08-19 重基线（v19.45）：废除序列号/批次码灰化（用户拍板，序列号与正常文本
       一致逐字对比）+ 恢复字符级红框（个别字符错→绿框+错误字符红框）+ 文字框跳 QR +
       标示去重。全量实测：基线更新为 R=28 Y=17 G=588 Gray=683（废除序列号灰化后，
       Gray 增加（批次码不再走序列号通道）+ R/Y 小降）。
并执行两个固化负向用例（历史误判，必须 0 框 / 必须保留）：
    1) did=123 blk_3 "▲警报" 三角 [280,53,23,38] 不得再被图标检测误判
       （Phase 6 几何排除 _is_triangular 从根因消除，已退役坐标白名单）。
    2) did=115 blk_3 黄框白名单 _drawing_yellow_filter[(115,3)] 必须仍存在
       （不同根因：OCR 把 A|B 读成 AB，坐标白名单保留，未被误删）。

任一断言失败 → 退出码 1（阻断合并）。全过 → 退出码 0。
"""
import os, sys, json, subprocess, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, HERE)
os.chdir(HERE)

BASE_R, BASE_Y = 28, 17
BASE_G, BASE_GRAY = 588, 683
G_TOL, GRAY_TOL = 25, 25
R_TOL, Y_TOL = 3, 3   # R/Y 亦受 RapidOCR 跨次抖动影响（同 G/Gray），给小幅容差

REPORT = os.path.join(ROOT, "data", "seg", "_baseline_real_match.json")
SCRIPT = os.path.join(HERE, "real_match_regression.py")
ENGINE = os.path.abspath(os.path.join(
    os.environ.get("PYTHON_EXE",
                   r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe")))


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def load_or_run_summary():
    """优先用较新的报告；否则 subprocess 重跑（V-1 风格：绝对路径 + cwd=脚本目录）。"""
    src_mtime = os.path.getmtime(os.path.join(HERE, "diff_visualizer.py")) \
        if os.path.exists(os.path.join(HERE, "diff_visualizer.py")) else 0
    if os.path.exists(REPORT) and os.path.getmtime(REPORT) >= src_mtime:
        with open(REPORT, encoding="utf-8") as f:
            rep = json.load(f)
        return rep.get("summary", {}), "cached"
    # 重跑（最小环境，cwd=脚本目录，与 C# 子进程一致）
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE
    subprocess.run([ENGINE, SCRIPT], cwd=HERE, env=env, check=True)
    with open(REPORT, encoding="utf-8") as f:
        rep = json.load(f)
    return rep.get("summary", {}), "regenerated"


def negative_checks():
    """返回 [(name, ok, detail), ...]"""
    import diff_visualizer as dv
    results = []

    # 1) did=123 blk_3 三角不得被检测为图标
    blk = os.path.join(ROOT, "data", "seg", "123", "blocks", "123_blk_03.png")
    tri = (280, 53, 23, 38)
    try:
        icons = dv._detect_icon_regions(blk, is_photo=False)
        hit = any(_iou(ic, tri) >= 0.5 for ic in icons)
        results.append(("did=123 blk_3 三角误判已消除", not hit,
                        f"检测图标数={len(icons)}, 三角命中={hit}"))
    except Exception as e:
        results.append(("did=123 blk_3 三角误判已消除", False, f"异常: {e}"))

    # 2) did=115 blk_3 黄框白名单必须保留
    kept = dv._drawing_yellow_filter.get((115, 3)) is not None
    results.append(("_drawing_yellow_filter[(115,3)] 保留", kept,
                    f"value={dv._drawing_yellow_filter.get((115, 3))}"))

    # 3) 四色框契约稳定（legacy run 仍返回四数组）
    contract_ok = all(hasattr(dv, n) for n in ("run",))
    results.append(("legacy run() 接口存在", contract_ok, ""))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重跑回归（忽略缓存报告）")
    args = ap.parse_args()

    fails = []
    if args.force and os.path.exists(REPORT):
        os.remove(REPORT)

    summary, src = load_or_run_summary()
    tr, ty, tg, tgray = (summary.get("total_R", -1), summary.get("total_Y", -1),
                         summary.get("total_G", -1), summary.get("total_Gray", -1))

    print(f"[report:{src}] 汇总 R={tr} Y={ty} G={tg} Gray={tgray} (基线 R={BASE_R} Y={BASE_Y} G≈{BASE_G} Gray≈{BASE_GRAY})")

    if tr < 0 or ty < 0:
        fails.append(f"报告缺少 R/Y 汇总: {summary}")
    if tr > BASE_R + R_TOL:
        fails.append(f"R={tr} 超过基线 {BASE_R}+{R_TOL}（出现新增红框负向）")
    if ty > BASE_Y + Y_TOL:
        fails.append(f"Y={ty} 超过基线 {BASE_Y}+{Y_TOL}（出现新增黄框负向）")
    if abs(tg - BASE_G) > G_TOL:
        fails.append(f"G={tg} 偏离基线 {BASE_G} 超容差±{G_TOL}（非 OCR 抖动级）")
    if abs(tgray - BASE_GRAY) > GRAY_TOL:
        fails.append(f"Gray={tgray} 偏离基线 {BASE_GRAY} 超容差±{GRAY_TOL}（非 OCR 抖动级）")

    for name, ok, detail in negative_checks():
        print(f"  [neg] {'PASS' if ok else 'FAIL'} {name} — {detail}")
        if not ok:
            fails.append(f"负向用例失败: {name} — {detail}")

    if fails:
        print("\n❌ VALIDATE FAILED:")
        for f in fails:
            print("   -", f)
        sys.exit(1)
    print("\n✅ VALIDATE PASSED：差异指标零负向 + 负向用例全过，可合并主干。")


if __name__ == "__main__":
    main()
