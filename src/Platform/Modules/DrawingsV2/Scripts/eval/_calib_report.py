# -*- coding: utf-8 -*-
"""读取 textsize_calib.json 做统计：分档耗时、文本集差异、conf 差异、大/小 crop 分层。"""
import json, os, statistics as st

P = r"E:\workaaa\shengchanguanli\data\_exp\textsize_calib.json"
recs = json.load(open(P, encoding="utf-8"))
TH = ["1024", "1280", "1600"]
print(f"region 数 = {len(recs)}")
base0 = [r["res"]["0"]["ms"] for r in recs]
print(f"基线(0) 逐 region ms: min={min(base0):.0f} P50={st.median(base0):.0f} max={max(base0):.0f} 总={sum(base0)/1000:.1f}s")
print(f"  （首个 region 基线 {base0[0]:.0f}ms —— 若显著高于中位数说明含模型预热）")

big = [r for r in recs if r["canvas"] > 1024]
small = [r for r in recs if r["canvas"] <= 1024]
print(f"\n分层：大 canvas(>1024) {len(big)} 个，小 canvas {len(small)} 个")
for name, grp in (("大", big), ("小", small)):
    if not grp:
        continue
    b = sum(r["res"]["0"]["ms"] for r in grp)
    line = f"  {name}canvas 基线总 {b/1000:6.1f}s 均值 {b/len(grp):6.0f}ms |"
    for t in TH:
        v = sum(r["res"][t]["ms"] for r in grp)
        line += f" {t}: {v/1000:5.1f}s({(1-v/max(b,1e-9))*100:+.0f}%)"
    print(line)

print("\n=== 各阈值 vs 基线 ===")
for t in TH:
    tot = sum(r["res"][t]["ms"] for r in recs)
    same_text, conf_only, text_diff = 0, 0, []
    maxd = 0.0
    for r in recs:
        b = r["res"]["0"]["sig"]
        n = r["res"][t]["sig"]
        bt = [x[0] for x in b]
        nt = [x[0] for x in n]
        if bt == nt:
            same_text += 1
            db = {x[0]: x[1] for x in b}
            dn = {x[0]: x[1] for x in n}
            d = max((abs(db[k] - dn.get(k, 0))) for k in db) if db else 0.0
            maxd = max(maxd, d)
            if d > 0.001:
                conf_only += 1
        else:
            text_diff.append(r)
    print(f"\n阈值 {t}: 总 {tot/1000:.1f}s（省 {(1-tot/sum(base0))*100:.1f}%） 均值 {tot/len(recs):.0f}ms")
    print(f"  文本集完全一致 {same_text}/{len(recs)}；其中 conf 有变化 {conf_only}（最大 Δconf={maxd:.3f}）")
    print(f"  文本集不同 {len(text_diff)}：")
    for r in text_diff:
        print(f"    {r['photo'][:12]} r{r['region']} 目标={r['text']!r}")
        print(f"       0   -> {[x[0] for x in r['res']['0']['sig']]}")
        print(f"       {t} -> {[x[0] for x in r['res'][t]['sig']]}")
        db = {x[0]: x[1] for x in r['res']['0']['sig']}
        dn = {x[0]: x[1] for x in r['res'][t]['sig']}
        common = set(db) & set(dn)
        if common:
            print(f"       共有项 Δconf: {[ (k, round(dn[k]-db[k],3)) for k in common ]}")
