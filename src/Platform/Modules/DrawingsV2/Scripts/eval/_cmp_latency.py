# -*- coding: utf-8 -*-
"""逐用例耗时变化分布：区分「真变慢」与「噪声/负载」，并估算剩余耗时构成。"""
import json, os, statistics as st, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
new = json.load(open(os.path.join(HERE, "baseline_report_1280.json"), encoding="utf-8"))
base = json.load(open(os.path.join(HERE, "baseline_report.json"), encoding="utf-8"))
bn = {c["id"]: c for c in new["results"]}
bb = {c["id"]: c for c in base["results"]}

deltas = sorted(((bn[k]["ms"] - bb[k]["ms"]), k, bb[k]["ms"], bn[k]["ms"])
                for k in bb if k in bn)
ds = [d[0] for d in deltas]
print(f"逐用例 Δms：min={min(ds):+.0f} P25={ds[len(ds)//4]:+.0f} 中位={st.median(ds):+.0f} "
      f"P75={ds[len(ds)*3//4]:+.0f} max={max(ds):+.0f}")
slow = [d for d in deltas if d[0] < -1000]
fast = [d for d in deltas if d[0] > 1000]
flat = [d for d in deltas if -1000 <= d[0] <= 1000]
print(f"  变快 >1s：{len(slow)} 例（合计省 {sum(-d[0] for d in slow)/1000:.0f}s）")
print(f"  变慢 >1s：{len(fast)} 例（合计增 {sum(d[0] for d in fast)/1000:.0f}s）")
print(f"  持平 ±1s：{len(flat)} 例")

print("\n变慢最多的 6 例（检查是否与本次改动无关，如 QR 主导）：")
for d in sorted(deltas, reverse=True)[:6]:
    print(f"   {d[1]}: {d[2]}ms → {d[3]}ms  ({d[0]:+.0f}ms)")

# 用 DB 的 verify_ms 看端到端是否也被同样影响（排除 HTTP 层噪声）
con = sqlite3.connect(r"E:\workaaa\shengchanguanli\data\drawingsv2.db")
con.execute("PRAGMA journal_mode=WAL")
cur = con.cursor()
old = [r[0] for r in cur.execute(
    "SELECT verify_ms FROM v2_compare_records WHERE created_at BETWEEN "
    "'2026-09-15 13:00:00' AND '2026-09-15 15:20:00' AND verify_ms>0")]
cur2 = con.cursor()
newv = [r[0] for r in cur2.execute(
    "SELECT verify_ms FROM v2_compare_records WHERE created_at > '2026-09-15 16:00:00' "
    "AND verify_ms>0")]
con.close()
if old and newv:
    print(f"\nDB verify_ms（纯感知层，不含 HTTP）：")
    print(f"   改动前样本 n={len(old)} 中位={st.median(old):.0f}ms 均值={sum(old)/len(old):.0f}ms")
    print(f"   改动后样本 n={len(newv)} 中位={st.median(newv):.0f}ms 均值={sum(newv)/len(newv):.0f}ms")
    print(f"   中位变化 {(1-st.median(newv)/st.median(old))*100:+.1f}%  "
          f"均值变化 {(1-(sum(newv)/len(newv))/(sum(old)/len(old)))*100:+.1f}%")
