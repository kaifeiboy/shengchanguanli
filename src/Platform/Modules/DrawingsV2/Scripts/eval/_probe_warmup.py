# -*- coding: utf-8 -*-
"""验证假设：端点耗时的巨幅波动是不是「每请求冷启动（Python 进程 + OCR 模型加载）」。

方法：同一用例连续请求 N 次，看耗时是否单调下降后收敛（冷启动特征），
以及 DB 里记录的 verify_ms（感知层内部计时）与端点 ms 的差值是否近似常数。
"""
import json, os, sqlite3, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = r"E:\workaaa\shengchanguanli"
EP = "http://127.0.0.1:5000/api/drawingsv2/compare"
ts = json.load(open(os.path.join(HERE, "testset.json"), encoding="utf-8"))
cases = ts["cases"] if isinstance(ts, dict) else ts
by_id = {c["id"]: c for c in cases}

TARGETS = ["p63-3ca0bfd4", "p61-0253298c", "p64-ae4973b4"]
N = 3


def call(profile_id, photo_abs):
    body = json.dumps({"drawing": str(profile_id),
                       "photo": photo_abs.replace("\\", "/"),
                       "observe": True}).encode("utf-8")
    req = urllib.request.Request(EP, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return d, (time.time() - t0) * 1000


con = sqlite3.connect(os.path.join(ROOT, "data", "drawingsv2.db"))
con.execute("PRAGMA journal_mode=WAL")
cur = con.cursor()

for tid in TARGETS:
    c = by_id.get(tid)
    if not c:
        print("未找到用例", tid)
        continue
    photo = c["photoRel"] if os.path.isabs(c["photoRel"]) else os.path.join(ROOT, c["photoRel"])
    print(f"\n=== {tid} profile={c['profileId']} photo={os.path.basename(photo)} ===")
    mss = []
    for i in range(N):
        before = cur.execute("SELECT MAX(id) FROM v2_compare_records").fetchone()[0]
        d, ms = call(c["profileId"], os.path.abspath(photo))
        row = cur.execute(
            "SELECT id, verify_ms FROM v2_compare_records WHERE id>? ORDER BY id DESC LIMIT 1",
            (before,)).fetchone()
        mss.append(ms)
        vms = row[1] if row else None
        print(f"  第{i+1}次: 端点 {ms:8.0f}ms  感知层 verify_ms={vms}  "
              f"差(端点-感知)={(ms - vms):.0f}ms" if vms is not None else
              f"  第{i+1}次: 端点 {ms:8.0f}ms")
    if len(mss) >= 2:
        print(f"  → 第1次 vs 后续最小: {mss[0]:.0f}ms → {min(mss[1:]):.0f}ms "
              f"（{(1-min(mss[1:])/mss[0])*100:+.0f}%）")
con.close()
