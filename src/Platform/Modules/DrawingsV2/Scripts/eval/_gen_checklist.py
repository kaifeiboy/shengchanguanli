"""A｜生成按用例分组的真值标注清单（Markdown），方便人工逐照片填 present/missing/offscreen。

输入：truth_template.csv（242 行，含 systemState 提示，truth 为空）
输出：truth_checklist.md（42 用例分组，每个 mark 一行，含提示 + 空 truth 位）
"""
import os, csv, json

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "truth_template.csv")
OUT = os.path.join(HERE, "truth_checklist.md")

# 读取 testset 以拿到每用例的 photo 相对路径（方便人工找图）
ts = json.load(open(os.path.join(HERE, "testset.json"), encoding="utf-8"))
cases = ts["cases"] if isinstance(ts, dict) else ts
photo_of = {c["id"]: c.get("photoRel", "") for c in cases}

rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
by_case = {}
for r in rows:
    by_case.setdefault(r["caseId"], []).append(r)

lines = ["# V2 真值标注清单（按用例分组）", "",
         "> 用法：对每张照片，判断该 mark 在**实物**上是否确有此标示。",
         "> - `present` = 实物有此标（与系统判定无关，凭眼判断）",
         "> - `missing` = 实物确实没有此标（系统判 Matched 即为**假绿**）",
         "> - `offscreen` = 该标在照片范围外/被遮挡看不到（不计召回）",
         "> 填完把每行的 truth 抄回 `truth_template.csv` 的 truth 列，",
         "> 再执行：`python run_baseline.py --import-truth truth_template.csv`",
         "", f"共 {len(by_case)} 个用例 / {len(rows)} 个 mark 待标注。", ""]

for cid, rs in by_case.items():
    lines.append(f"## {cid}  （照片：{photo_of.get(cid, '?')}）")
    lines.append("")
    lines.append("| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |")
    lines.append("|---|---|---|---|---|")
    for r in rs:
        lines.append(f"| {r['markKey']} | {r['text']} | {r['view']} | {r['systemState']} |  |")
    lines.append("")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print(f"已生成 {OUT}：{len(by_case)} 用例 / {len(rows)} mark")
