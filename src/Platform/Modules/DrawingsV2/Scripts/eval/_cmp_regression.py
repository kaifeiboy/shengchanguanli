import json, os, sys

HERE = os.path.dirname(__file__)
new = json.load(open(os.path.join(HERE, "_demo_full.json"), encoding="utf-8"))
base = json.load(open(os.path.join(HERE, "baseline_report_1280.json"), encoding="utf-8"))

new_res = {r["id"]: r for r in new["results"]}
base_res = {r["id"]: r for r in (base.get("results") or base.get("cases") or [])}

# baseline_report_1280.json 的结构是 {results:[{id,profileId,...,verdicts}]}? 还是 {cases:...}?
# 兼容两种
def get_cases(d):
    for k in ("results", "cases"):
        if k in d and isinstance(d[k], list):
            return d[k]
    return []

diff_count = 0
diff_cases = []
for cid in new_res:
    nr = new_res[cid]
    br = base_res.get(cid)
    if br is None:
        diff_cases.append((cid, "missing_in_base"))
        diff_count += 1
        continue
    nc = nr.get("counts") or {}
    bc = br.get("counts") or {}
    if nc != bc:
        diff_cases.append((cid, "counts", nc, bc))
        diff_count += 1

print("新报告用例数:", len(new_res), " 基线用例数:", len(base_res))
print("counts 差异用例数:", diff_count)
for d in diff_cases[:20]:
    print("  DIFF", d)

# 汇总 verdicts 状态分布对比
def dist(res_map):
    from collections import Counter
    c = Counter()
    for r in res_map.values():
        for v in (r.get("verdicts") or []):
            c[v.get("state")] += 1
    return c

nd = dist(new_res); bd = dist(base_res)
print("新 verdict 分布:", dict(nd))
print("基 verdict 分布:", dict(bd))
print("分布差异:", dict(nd - bd) if (nd - bd) else "无")

# 逐 mark 状态对比（用 markKey+state 集合）
def markstate_map(res_map):
    m = {}
    for r in res_map.values():
        for v in (r.get("verdicts") or []):
            m[(r["id"], v.get("markKey"))] = v.get("state")
    return m

nm = markstate_map(new_res); bm = markstate_map(base_res)
ms_diff = 0
for k in set(nm) | set(bm):
    if nm.get(k) != bm.get(k):
        ms_diff += 1
print("逐 mark 状态差异数:", ms_diff)

# 视图对比
def view_map(res_map):
    m = {}
    for r in res_map.values():
        m[r["id"]] = r.get("inferredView") or r.get("selectedView")
    return m
# baseline 可能没存 view；仅当都存在时比
nv = {r["id"]: (r.get("inferredView") or r.get("selectedView")) for r in new_res.values()}
print("汇总 OK; 差异总数(用例级 counts):", diff_count, " 逐mark:", ms_diff)
