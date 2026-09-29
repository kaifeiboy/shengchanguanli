# -*- coding: utf-8 -*-
"""has_a 修复后：对当前库中所有持有逻辑图块(或存在比对记录)的 profile 重新触发 P2 提取
（POST /api/drawingsv2/profiles/{id}/blocks -> ExtractLogicalBlocksAsync -> ReplaceLogicalBlocks 覆盖写）。
代码已即时生效，本脚本使线上 DB 受益。结果写 _refresh_p2.json。
"""
import json, time, urllib.request, sqlite3

DB = "data/drawingsv2.db"
con = sqlite3.connect(DB); con.execute("PRAGMA journal_mode=WAL"); cur = con.cursor()
ids_blocks = {r[0] for r in cur.execute("SELECT DISTINCT profile_id FROM v2_logical_blocks")}
ids_recs = {r[0] for r in cur.execute("SELECT DISTINCT profile_id FROM v2_compare_records")}
pids = sorted(ids_blocks | ids_recs)
print("re-extract profiles:", pids)

BASE = "http://localhost:5000/api/drawingsv2/profiles/{id}/blocks"
out = []
for pid in pids:
    body = json.dumps({}).encode("utf-8")
    url = BASE.format(id=pid)
    t0 = time.time()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=600) as resp:
            txt = resp.read().decode("utf-8")
        res = json.loads(txt)
        dt = round(time.time() - t0, 1)
        nblk = res.get("nBlocks") or res.get("n_blocks")
        nhm = res.get("nHasMarking") or res.get("n_has_marking")
        out.append({"pid": pid, "ok": True, "seconds": dt, "nBlocks": nblk, "nHasMarking": nhm})
        print("pid=%d OK %.1fs blocks=%s has_marking=%s" % (pid, dt, nblk, nhm))
    except Exception as e:
        dt = round(time.time() - t0, 1)
        out.append({"pid": pid, "ok": False, "seconds": dt, "error": str(e)[:200]})
        print("pid=%d FAIL %.1fs %s" % (pid, dt, e))

with open("_refresh_p2.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE", len(out))
