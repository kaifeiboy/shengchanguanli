#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探查：哪些 profile 含多个 Icon / 多个 Qr；展示其 mark_key/type/required/text/NormBbox。"""
import json
import os
import sqlite3

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
DB = os.path.join(ROOT, "data", "drawingsv2.db")

con = sqlite3.connect(DB)
con.execute("PRAGMA journal_mode=WAL")
cur = con.cursor()

# 列存在性
cols = [r[1] for r in cur.execute("PRAGMA table_info(v2_drawing_marks)")]
print("v2_drawing_marks 列:", cols)

need = {"id", "profile_id", "mark_key", "type", "required", "text", "norm_bbox", "view"}
sel = ", ".join(c for c in ["id", "profile_id", "mark_key", "type", "required", "text", "norm_bbox", "view"] if c in cols)
cur.execute(f"SELECT {sel} FROM v2_drawing_marks ORDER BY profile_id, id")

from collections import defaultdict
by_prof = defaultdict(list)
for row in cur.fetchall():
    d = dict(zip(["id", "profile_id", "mark_key", "type", "required", "text", "norm_bbox", "view"], row))
    by_prof[d["profile_id"]].append(d)

print("\n== profile 类型分布 ==")
for pid, rows in sorted(by_prof.items()):
    tc = sum(1 for r in rows if r["type"] == "Text")
    ic = sum(1 for r in rows if r["type"] == "Icon")
    qr = sum(1 for r in rows if r["type"] == "Qr")
    gp = sum(1 for r in rows if r["type"] == "Group")
    flag = ""
    if ic >= 2 or qr >= 2:
        flag = "  <== 多图标/多QR"
    print(f"  profile {pid}: Text={tc} Icon={ic} Qr={qr} Group={gp}{flag}")

print("\n== 多 Icon / 多 Qr 的 profile 明细 ==")
shown = 0
for pid, rows in sorted(by_prof.items()):
    ic = [r for r in rows if r["type"] == "Icon"]
    qr = [r for r in rows if r["type"] == "Qr"]
    if len(ic) < 2 and len(qr) < 2:
        continue
    shown += 1
    print(f"\n--- profile {pid} (Icon={len(ic)}, Qr={len(qr)}) ---")
    for r in rows:
        if r["type"] not in ("Icon", "Qr"):
            continue
        nb = r["norm_bbox"]
        nb_s = (f"[{nb[0]:.3f},{nb[1]:.3f},{nb[2]:.3f},{nb[3]:.3f}]" if nb else "null")
        print(f"  {r['type']:5s} key={r['mark_key']!r} required={r['required']} "
              f"text={r['text']!r} view={r['view']} norm_bbox={nb_s}")
    if shown >= 8:
        break
con.close()
