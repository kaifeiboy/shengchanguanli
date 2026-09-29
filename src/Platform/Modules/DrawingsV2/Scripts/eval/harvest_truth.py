#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把人工标注结果（truth_answers.json，由 truth_workbook.html 导出）回填进 testset.json。

- 写回每个用例的 truth.view（视图正确率用）
- 写回每个 mark 的 truth.marks[markKey] = present/missing/offscreen
- 做合法性校验：三态取值、view 非空、markKey 命中
- 打印覆盖率，便于判断 B 类指标是否已足够出数

用法：
  python harvest_truth.py truth_answers.json
回填后跑：python run_baseline.py          # 全量，出 B 类指标
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTSET = os.path.join(HERE, "testset.json")
VALID = ("present", "missing", "offscreen")


def main():
    if len(sys.argv) < 2:
        print("用法：python harvest_truth.py truth_answers.json", file=sys.stderr)
        return 2
    ans_path = sys.argv[1]
    ans = json.load(open(ans_path, encoding="utf-8"))

    ts = json.load(open(TESTSET, encoding="utf-8"))
    by_id = {c["id"]: c for c in ts["cases"]}

    total_marks = sum(len((c.get("truth") or {}).get("marks") or {}) for c in ts["cases"])
    # 统计模板期望的 mark 数（用 testset 现有 marks 字典可能为空，改数 truth_template 行数不可靠；用导出时 242）
    expected = 242

    filled_cases = 0
    filled_marks = 0
    view_set = 0
    warns = []

    for c in ans.get("cases", []):
        cid = c.get("caseId")
        tc = by_id.get(cid)
        if tc is None:
            warns.append("用例不存在（跳过）：%s" % cid)
            continue
        truth = tc.get("truth") or {}
        tc["truth"] = truth
        view = (c.get("view") or "").strip()
        if view:
            truth["view"] = view
            view_set += 1
        else:
            warns.append("视图未填（viewAccuracy 将缺此例）：%s" % cid)
        marks = truth.setdefault("marks", {})
        for m in c.get("marks", []):
            mk = m.get("markKey")
            tv = (m.get("truth") or "").strip().lower()
            if tv not in VALID:
                warns.append("非法真值（跳过）：%s / %s = %r" % (cid, mk, m.get("truth")))
                continue
            marks[mk] = tv
            filled_marks += 1
        filled_cases += 1

    with open(TESTSET, "w", encoding="utf-8") as f:
        json.dump(ts, f, ensure_ascii=False, indent=2)

    print("== 回填完成 ==")
    print("  用例填完：%d / %d" % (filled_cases, len(ts["cases"])))
    print("  标记填完：%d / %d（%.1f%%）" % (filled_marks, expected, 100.0 * filled_marks / expected))
    print("  视图填完：%d / %d" % (view_set, len(ts["cases"])))
    if warns:
        print("\n-- 警告（%d 条）--" % len(warns))
        for w in warns[:40]:
            print("  " + w)
    print("\n下一步：python run_baseline.py   # 全量重跑出 B 类指标（viewAccuracy/recall/falseRedRate/falseGreenRate）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
