# -*- coding: utf-8 -*-
"""has_a 修复 + 重新落库后：对高样本量 profile 的近期比对记录重新跑 /compare，
抽取 blockMatch 的 Top 块名与四色计数，确认修复后匹配未崩溃（ sanity 回归）。
仅做 post-refresh 抽检；不追求与历史 verdicts 逐条对照（记录存的是 v1 verdicts，
与 blockMatch 口径不同）。写 _regress_match.json。
"""
import json, sqlite3, urllib.request, urllib.error

DB = "data/drawingsv2.db"
con = sqlite3.connect(DB); con.execute("PRAGMA journal_mode=WAL"); cur = con.cursor()

SAMPLE = {"63": 12, "66": 10, "61": 8, "62": 8, "64": 8, "52": 8}
BASE = "http://localhost:5000/api/drawingsv2/compare"

def call_compare(pid, photo):
    body = json.dumps({"drawing": str(pid), "photo": photo}).encode("utf-8")
    req = urllib.request.Request(BASE, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d" % e.code
    except Exception as e:
        return None, str(e)[:160]

out = {}
for pid, n in SAMPLE.items():
    rows = cur.execute("""SELECT photo_path, drawing_key FROM v2_compare_records
        WHERE profile_id=? ORDER BY id DESC LIMIT ?""", (int(pid), n)).fetchall()
    recs = []
    for photo, dk in rows:
        if not photo:
            continue
        res, err = call_compare(pid, photo)
        if err:
            recs.append({"photo": photo, "error": err}); continue
        bm = (res or {}).get("blockMatch") or {}
        top = bm.get("top") or {}
        recs.append({
            "photo": photo.split("/")[-1] if photo else None,
            "enabled": bm.get("enabled"),
            "top_name": (top.get("name") or "")[:48],
            "score": top.get("score"),
            "nGreen": top.get("nGreen"), "nRed": top.get("nRed"),
            "nYellow": top.get("nYellow"), "nGray": top.get("nGray"),
            "nHit": top.get("nHit"),
            "reason": bm.get("reason"),
        })
    out[pid] = recs
    print("pid=%s n=%d" % (pid, len(recs)))

with open("_regress_match.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
# 汇总每 profile 的 Top 名分布与四色均值
print("\n=== 汇总 ===")
for pid, recs in out.items():
    ok = [r for r in recs if r.get("enabled")]
    names = {}
    for r in ok:
        names[r["top_name"]] = names.get(r["top_name"], 0) + 1
    print("pid=%s 有效=%d Top名分布=%s" % (pid, len(ok), names))
