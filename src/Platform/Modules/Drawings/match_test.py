# -*- coding: utf-8 -*-
"""真跑匹配：对指定 did/blk 用 full.png 作照片侧，验证 QR 噪声是否被灰化、是否产生假红/黄框。"""
import os, sqlite3, json, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import diff_visualizer as dv

DB = r"e:\workaaa\shengchanguanli\data\app.db"
SEG = r"e:\workaaa\shengchanguanli\data\seg"
OUT = r"e:\workaaa\shengchanguanli\data\seg\_fleet_test"
os.makedirs(OUT, exist_ok=True)

con = sqlite3.connect(DB); cur = con.cursor()

def run_one(did, bidx):
    cur.execute("SELECT RawText,FileRel FROM drawing_blocks WHERE DrawingId=? AND BIdx=?", (did, bidx))
    row = cur.fetchone()
    raw = row[0] or ""
    full = os.path.join(SEG, str(did), f"{did}_full.png")
    blk = os.path.join(SEG, str(did), "blocks", f"{did}_blk_{bidx:02d}.png")
    out = os.path.join(OUT, f"{did}_{bidx:02d}_out.png")
    if not os.path.isfile(full) or not os.path.isfile(blk):
        print(f"  [SKIP] did={did} blk={bidx} 文件缺失 full={os.path.isfile(full)} blk={os.path.isfile(blk)}")
        return
    res = dv.run(full, blk, out, block_text_override=raw)
    red = len(res.get("redRegions", []))
    yellow = len(res.get("yellowRegions", []))
    gray = len(res.get("grayRegions", []))
    green = len(res.get("greenRegions", []))
    print(f"\n=== did={did} blk={bidx} ===")
    print(f"  red={red} yellow={yellow} gray={gray} green={green}")
    if res.get("blockExclusive"):
        print(f"  blockExclusive(假红框候选): {res['blockExclusive']}")
    if res.get("photoExclusive"):
        print(f"  photoExclusive(假黄框候选): {res['photoExclusive']}")
    if res.get("silentSkipped"):
        print(f"  silentSkipped: {res['silentSkipped'][:5]}")
    # 检查 NNFC / S'Oft 是否出现在差异里
    for tok in (res.get("blockExclusive", []) + res.get("photoExclusive", [])):
        if "NNFC" in str(tok) or "S'Oft" in str(tok) or "soft" in str(tok).lower():
            print(f"  ⚠️ QR 噪声落入差异框: {tok!r}")

for did, bidx in [(116, 0), (115, 0), (132, 0), (108, 3)]:
    run_one(did, bidx)

con.close()
print("\nDONE")
