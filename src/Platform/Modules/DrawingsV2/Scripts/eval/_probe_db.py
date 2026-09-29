# -*- coding: utf-8 -*-
"""临时探针：查看 drawingsv2.db 表结构与记录字段，用于离线标定取真实 regions。"""
import sqlite3, json, os, sys

DB = r"E:\workaaa\shengchanguanli\data\drawingsv2.db"
con = sqlite3.connect(DB)
con.execute("PRAGMA journal_mode=WAL")
cur = con.cursor()

print("== tables ==")
for (t,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print("  ", t)

print("\n== v2_compare_records columns ==")
try:
    for r in cur.execute("PRAGMA table_info(v2_compare_records)"):
        print("  ", r[1], r[2])
except Exception as e:
    print("ERR", e)

print("\n== latest 3 records (id, created_at, profile_id, session_id) ==")
try:
    for r in cur.execute("SELECT id, created_at, profile_id, session_id FROM v2_compare_records ORDER BY id DESC LIMIT 3"):
        print("  ", r)
except Exception as e:
    print("ERR", e)

print("\n== one row keys/values (truncated) ==")
try:
    cur.execute("SELECT * FROM v2_compare_records ORDER BY id DESC LIMIT 1")
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    for c, v in zip(cols, row):
        s = str(v)
        if len(s) > 300:
            s = s[:300] + " ...[len=%d]" % len(str(v))
        print("  %-24s %s" % (c, s))
except Exception as e:
    print("ERR", e)
con.close()
