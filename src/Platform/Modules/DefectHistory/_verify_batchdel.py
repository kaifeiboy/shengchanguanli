#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证批量删除修复：升序（旧 bug）vs 降序（新方案）在真实数据上的差异。
用两份独立临时 root 副本（与生产一致），分别模拟两种删除顺序，diff 前后数据给出可靠证据。

用法: python -E _verify_batchdel.py
"""
import os
import sys
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import defect_history as dh

SRC = r"E:\生产不良履历\不良履历汇总.xlsx"
BASE = r"E:\workaaa\shengchanguanli\temp\_vbd"
os.makedirs(BASE, exist_ok=True)


def setup(tag):
    T = os.path.join(BASE, tag)
    if os.path.exists(T):
        shutil.rmtree(T)
    os.makedirs(T)
    shutil.copy2(SRC, os.path.join(T, "不良履历汇总.xlsx"))
    return T


def snapshot(root):
    """全量数据快照: [(date,line,model,hasImage)]，用于前后 diff"""
    q = dh.cmd_query({"root": root})
    return [(r["date"], r["line"], r["model"], r["hasImage"]) for r in q["rows"]]


def delete_asc(root, rows):
    """旧 bug 逻辑：升序逐个删（H5 修复前 Array.from(dhSelected) 顺序）"""
    ok = 0
    for row in sorted(rows):          # 升序
        try:
            dh.cmd_delete_row({"root": root, "row": row})
            ok += 1
        except Exception as e:
            print("    [旧逻辑] 删 row=%d 失败: %s" % (row, str(e)[:60]))
    return ok


def delete_desc(root, rows):
    """新修复逻辑：降序逐个删（H5 修复后 sort(b-a)）"""
    for row in sorted(rows, reverse=True):   # 降序
        dh.cmd_delete_row({"root": root, "row": row})


def main():
    # 目标：8 月 3 条连续行（相邻 data_idx，模拟用户勾选相邻行，最易触发升序位移错位）
    probe = setup("probe")
    q8 = dh.cmd_query({"root": probe, "month": "2026-08"})
    targets = [x["row"] for x in q8["rows"]]
    print("目标删除（8 月 %d 条连续行，data_idx）: %s" % (len(targets), targets))
    shutil.rmtree(probe)

    # ---- 场景 A：旧逻辑（升序）----
    TA = setup("A_asc")
    beforeA = snapshot(TA)
    okA = delete_asc(TA, targets)
    afterA = snapshot(TA)
    delA = [x for x in beforeA if x not in afterA]
    imgA_before = sum(1 for x in beforeA if x[3])
    imgA_after = sum(1 for x in afterA if x[3])
    print()
    print("=== 场景A：旧逻辑（升序删除）===")
    print("接口成功 %d 次（目标 %d 条）" % (okA, len(targets)))
    print("实际数据变化：被删 %d 条" % len(delA))
    print("被删明细:")
    for x in delA:
        print("   ", x)
    print("有图数据: %d → %d（损失 %d）" % (imgA_before, imgA_after, imgA_before - imgA_after))
    target_keysA = {(x["date"], x["line"], x["model"]) for x in q8["rows"]}
    delA_keys = {(x[0], x[1], x[2]) for x in delA}
    print("是否精确命中目标: %s | 有图零损失: %s" % (delA_keys == target_keysA, imgA_before == imgA_after))
    shutil.rmtree(TA)

    # ---- 场景 B：新方案（降序）----
    TB = setup("B_desc")
    beforeB = snapshot(TB)
    delete_desc(TB, targets)
    afterB = snapshot(TB)
    delB = [x for x in beforeB if x not in afterB]
    imgB_before = sum(1 for x in beforeB if x[3])
    imgB_after = sum(1 for x in afterB if x[3])
    print()
    print("=== 场景B：新方案（降序删除）===")
    print("实际被删 %d 条（目标 %d 条）" % (len(delB), len(targets)))
    print("被删明细:")
    for x in delB:
        print("   ", x)
    print("有图数据: %d → %d（损失 %d）" % (imgB_before, imgB_after, imgB_before - imgB_after))
    # 精确性断言：①删除集合 == 目标集合；②【非目标】数据零变化（除目标外无任何误删/漏删）
    from collections import Counter
    target_keys = {(x["date"], x["line"], x["model"]) for x in q8["rows"]}
    delB_keys = {(x[0], x[1], x[2]) for x in delB}
    # 非目标数据快照对比（排除目标 3 条）
    c_before = Counter((x[0], x[1], x[2]) for x in beforeB if (x[0], x[1], x[2]) not in target_keys)
    c_after = Counter((x[0], x[1], x[2]) for x in afterB if (x[0], x[1], x[2]) not in target_keys)
    non_target_intact = (c_before == c_after)
    img_before_nontgt = sum(1 for x in beforeB if (x[0], x[1], x[2]) not in target_keys and x[3])
    img_after_nontgt = sum(1 for x in afterB if (x[0], x[1], x[2]) not in target_keys and x[3])
    exact = (delB_keys == target_keys) and non_target_intact and (img_before_nontgt == img_after_nontgt)
    print()
    print("降序精确性: 删除集合==目标集合: %s | 非目标数据零变化: %s | 非目标有图零损失: %s → 结果 %s" % (
        delB_keys == target_keys, non_target_intact, img_before_nontgt == img_after_nontgt,
        "✅ 杜绝误删" if exact else "❌ 仍有问题"))
    shutil.rmtree(TB)


if __name__ == "__main__":
    main()
