# -*- coding: utf-8 -*-
"""has_a 门禁修复后回归评估：对所有 profile 跑新提取器，逐块比对 curve_text 元素数
与改动前 DB 的差异，定位恢复块与不利影响（新增噪声块），并记录提取耗时。

输出：_eval_has_a_fix.json 与控制台摘要。
"""
import os, sys, io, json, time, sqlite3
sys.path.insert(0, "src/Platform/Modules/DrawingsV2/Scripts/vpdf")
import logical_blocks as LB

DB = "data/drawingsv2.db"
con = sqlite3.connect(DB); con.execute("PRAGMA journal_mode=WAL"); cur = con.cursor()
profiles = cur.execute("SELECT id, drawing_key, pdf_path FROM v2_drawing_profiles ORDER BY id").fetchall()
# DB 现状：逐块 curve_text 数 / has_marking / n_participate
db_cur = {}
for pid, pg, bi, hm, nc, np_ in cur.execute('''\
    SELECT b.profile_id, b.page_index, b.block_index, b.has_marking,
      SUM(CASE WHEN e.kind='curve_text' THEN 1 ELSE 0 END),
      SUM(e.participate)
    FROM v2_logical_blocks b LEFT JOIN v2_block_elements e ON e.block_id=b.id
    GROUP BY b.id'''):
    db_cur[(pid, pg, bi)] = (nc or 0, hm, np_ or 0)

KNOWN5 = [(63,0,1),(63,0,3),(66,0,0),(66,0,4),(67,0,2)]  # 审计确认的 5 个真漏提块

out = {"profiles": [], "recovered": [], "adverse_new": [], "total_seconds": 0.0}
t_all = time.time()
for pid, key, pdf in profiles:
    if not os.path.exists(pdf):
        print("SKIP missing pdf", pid, key); continue
    t0 = time.time()
    try:
        res = LB.extract_logical_blocks(pdf, ocr_on=True)
    except Exception as e:
        print("ERR", pid, key, e); continue
    dt = round(time.time() - t0, 1)
    out["total_seconds"] += dt
    pg = res.get("page_index", 0)
    pblocks = []
    for b in res["blocks"]:
        bi = b["block_index"]
        ctexts = [e["text"] for e in b["elements"] if e["kind"] == "curve_text"]
        nc = len(ctexts)
        hm = b["has_marking"]
        np_ = b["n_participate"]
        pblocks.append({"pg": pg, "bi": bi, "has_marking": hm, "n_curve": nc,
                        "n_part": np_, "n_elem": b["n_elements"], "n_img": b["n_image"],
                        "curve_texts": ctexts})
        dbv = db_cur.get((pid, pg, bi))
        if dbv is None:
            out["adverse_new"].append({"pid": pid, "key": key, "bi": bi,
                                       "note": "DB 无此块(新块?)", "n_curve": nc})
            continue
        db_nc, db_hm, db_np = dbv
        if nc > db_nc:
            rec = {"pid": pid, "key": key[:30], "bi": bi, "db_nc": db_nc, "new_nc": nc,
                   "added": ctexts}
            if (pid, pg, bi) in KNOWN5:
                out["recovered"].append(rec)
            else:
                out["adverse_new"].append(rec)
        elif nc < db_nc:
            out["adverse_new"].append({"pid": pid, "key": key[:30], "bi": bi,
                                       "note": "curve_text 减少(异常)", "db_nc": db_nc, "new_nc": nc})
    out["profiles"].append({"pid": pid, "key": key[:36], "seconds": dt, "n_blocks": len(pblocks),
                            "n_has_marking": res["n_has_marking"]})
    print("pid=%d %-36s %.1fs blocks=%d has_marking=%d" % (pid, key[:36], dt, len(pblocks), res["n_has_marking"]))

out["total_seconds"] = round(out["total_seconds"], 1)
with open("_eval_has_a_fix.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

print("\n=== 恢复块（KNOWN5 内，curve_text 由 0 变 >0）===")
for r in out["recovered"]:
    print("  pid=%d B%d  +%d: %s" % (r["pid"], r["bi"], r["new_nc"], r["added"]))
print("\n=== 不利影响：新增 curve_text 的块（非 KNOWN5）===")
if not out["adverse_new"]:
    print("  （无）")
else:
    for r in out["adverse_new"]:
        print("  ", r)
print("\n总耗时 %.1fs，profile 数 %d" % (out["total_seconds"], len(out["profiles"])))
