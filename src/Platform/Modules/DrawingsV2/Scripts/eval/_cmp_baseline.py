# -*- coding: utf-8 -*-
"""判定级对比：新报告 vs 基线报告，逐用例比对 counts 与逐 mark 状态。

用法：python _cmp_baseline.py <新报告.json> [基线报告.json]
判据：
  1) 每个用例的 counts 完全一致
  2) 每个 mark 的 state 完全一致（比 counts 更严：counts 相同也可能是内部互换）
  3) 耗时 P50/P95 与总耗时下降
"""
import json
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEW = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "baseline_report_1280.json")
BASE = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "baseline_report.json")

new = json.load(open(NEW, encoding="utf-8"))
base = json.load(open(BASE, encoding="utf-8"))
bn = {c["id"]: c for c in new["results"]}
bb = {c["id"]: c for c in base["results"]}
common = [k for k in bb if k in bn]
print(f"基线用例 {len(bb)}，新报告 {len(bn)}，可比 {len(common)}")

cnt_diff, mark_diff, view_diff = [], [], []
for k in sorted(common):
    a, b = bb[k], bn[k]
    if a.get("counts") != b.get("counts"):
        cnt_diff.append((k, a.get("counts"), b.get("counts")))
    ma = {v["markKey"]: v["state"] for v in a.get("verdicts", [])}
    mb = {v["markKey"]: v["state"] for v in b.get("verdicts", [])}
    d = {key: (ma.get(key), mb.get(key)) for key in set(ma) | set(mb)
         if ma.get(key) != mb.get(key)}
    if d:
        mark_diff.append((k, d))
    if (a.get("selectedView") or a.get("inferredView")) != (b.get("selectedView") or b.get("inferredView")):
        view_diff.append((k, a.get("inferredView") or a.get("selectedView"),
                          b.get("inferredView") or b.get("selectedView")))

print(f"\n【判定】counts 不同：{len(cnt_diff)}/{len(common)}")
for k, x, y in cnt_diff:
    print(f"   {k}\n      基线 {x}\n      新   {y}")
print(f"【判定】逐 mark 状态不同：{len(mark_diff)}/{len(common)}")
for k, d in mark_diff:
    for mk, (s1, s2) in d.items():
        print(f"   {k} {mk[-24:]}: {s1} -> {s2}")
print(f"【判定】视图不同：{len(view_diff)}/{len(common)}")
for v in view_diff:
    print("   ", v)


def lat(d):
    ms = [c["ms"] for c in d["results"] if c.get("ok")]
    return ms


mb, mn = lat(base), lat(new)
print(f"\n【耗时】基线 P50={st.median(mb):.0f}ms P95={sorted(mb)[int(len(mb)*.95)-1]:.0f}ms "
      f"总={sum(mb)/1000:.0f}s")
print(f"【耗时】新   P50={st.median(mn):.0f}ms P95={sorted(mn)[int(len(mn)*.95)-1]:.0f}ms "
      f"总={sum(mn)/1000:.0f}s  "
      f"（P50 {(1-st.median(mn)/st.median(mb))*100:+.1f}%，总 {(1-sum(mn)/sum(mb))*100:+.1f}%）")
print(f"【耗时】最慢 5 例（基线 → 新）：")
pair = {c["id"]: c["ms"] for c in base["results"] if c.get("ok")}
slow = sorted(pair.items(), key=lambda x: -x[1])[:5]
for k, v in slow:
    nv = bn.get(k, {}).get("ms")
    print(f"   {k}: {v}ms → {nv}ms")

print(f"\n【A 类指标】基线 vs 新")
for key in ("photoUsableRate", "viewInferRate", "anchorApplyRate", "decisiveRate",
            "notDetectedRate"):
    print(f"   {key:18} {base['metrics'].get(key)} → {new['metrics'].get(key)}")
print(f"   verdictDistribution:")
bd, nd = base["metrics"].get("verdictDistribution", {}), new["metrics"].get("verdictDistribution", {})
for k in sorted(set(bd) | set(nd)):
    flag = "" if bd.get(k, 0) == nd.get(k, 0) else "   <<< 变化"
    print(f"      {k:16} {bd.get(k,0):4} → {nd.get(k,0):4}{flag}")

ok = (not cnt_diff) and (not mark_diff) and (not view_diff)
print(f"\n结论：判定{'完全一致 ✅' if ok else '有变化 ❌'}")
