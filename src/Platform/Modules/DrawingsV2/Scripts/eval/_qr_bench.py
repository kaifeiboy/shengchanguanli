#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QR 解码优化前后对照基准（双角度验证·角度①正确性 + 角度②延迟）。
对指定 profile 的每张照片打 /api/drawingsv2/compare，仅统计 QR 类 mark 的判定分布与端到端耗时。
用法：python _qr_bench.py <out_json> [profile_ids...]
"""
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = "e:/workaaa/shengchanguanli"
DB = os.path.join(ROOT, "data", "drawingsv2.db")
TESTSET = os.path.join(HERE, "testset.json")
EP = "http://127.0.0.1:5000"

con = sqlite3.connect(DB)
con.execute("PRAGMA journal_mode=WAL")
qrmap = {}
for pid, mk, mt in con.execute(
    "SELECT profile_id,mark_key,mark_type FROM v2_drawing_marks WHERE is_active=1"
):
    if mt == "Qr":
        qrmap[(pid, mk)] = True
con.close()

ts = json.load(open(TESTSET, encoding="utf-8"))
targets = set(int(x) for x in sys.argv[2:]) if len(sys.argv) > 2 else {49, 50, 51}
cases = [c for c in ts["cases"] if c["profileId"] in targets]


def call(pid, photo):
    path = os.path.join(ROOT, photo) if not os.path.isabs(photo) else photo
    body = json.dumps({"drawing": str(pid), "photo": os.path.abspath(path).replace("\\", "/"),
                       "observe": True}).encode()
    req = urllib.request.Request(EP + "/api/drawingsv2/compare", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return d, (time.time() - t0) * 1000


tally = {}
ms = []
per = []
for c in cases:
    pid = c["profileId"]
    rel = c["photoRel"]
    try:
        d, m = call(pid, rel)
    except Exception as e:
        per.append({"id": c["id"], "error": "%s: %s" % (type(e).__name__, e)})
        continue
    ms.append(m)
    qr = []
    for v in d.get("verdicts", []):
        if qrmap.get((pid, v.get("markKey"))):
            tally[v.get("state")] = tally.get(v.get("state"), 0) + 1
            qr.append((v.get("markKey", "").split("#")[-1], v.get("state")))
    per.append({"id": c["id"], "ms": round(m), "qr": qr})

out = {
    "profiles": sorted(targets),
    "caseCount": len(cases),
    "qrStateTally": tally,
    "latencyMs": {
        "p50": round(statistics.median(ms)) if ms else None,
        "p95": round(sorted(ms)[max(0, len(ms) * 95 // 100 - 1)]) if ms else None,
        "avg": round(sum(ms) / len(ms)) if ms else None,
        "n": len(ms),
    },
    "per": per,
}
json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("QR state tally:", tally)
print("latency:", out["latencyMs"])
print("written:", sys.argv[1])
