import sqlite3, sys
sys.path.insert(0, "src/Platform/Modules/DrawingsV2/Scripts/vpdf")
import logical_blocks as LB

con = sqlite3.connect("data/drawingsv2.db")
con.execute("PRAGMA journal_mode=WAL")
cur = con.cursor()

overlap = 0
for pid in (49, 50, 52, 56, 57, 61, 62, 63, 64, 65, 66, 67):
    rows = cur.execute(
        "SELECT b.block_index, e.kind, e.source, e.text FROM v2_block_elements e "
        "JOIN v2_logical_blocks b ON e.block_id=b.id WHERE b.profile_id=? "
        "ORDER BY b.block_index", (pid,)).fetchall()
    byblk = {}
    for bi, kind, src, txt in rows:
        byblk.setdefault(bi, {"tl": [], "ct": []})
        if src == "text_layer" and txt:
            byblk[bi]["tl"].append(txt)
        elif kind == "curve_text" and txt:
            byblk[bi]["ct"].append(txt)
    for bi, d in byblk.items():
        a_norm = {LB._norm_text(t) for t in d["tl"]}
        for t in d["ct"]:
            if LB._ocr_conflicts_text_layer(LB._norm_text(t), a_norm):
                overlap += 1
                print("OVERLAP pid=%d B%d curve=%r" % (pid, bi, t))
print("DB curve_text/text_layer 重叠条目数:", overlap)

need = {
    63: {1: ["YCWA16NCWQ", "400-620-6607"], 3: ["YCWA15NCWQ", "400-620-6607"]},
    66: {0: ["YCWA15NC", "400-620-6607"], 4: ["YCWA15NC"]},
    67: {2: ["YORK", "RS485"]},
}
for pid, mp in need.items():
    for bi, ns in mp.items():
        ct = " ".join(r[0] for r in cur.execute(
            "SELECT e.text FROM v2_block_elements e JOIN v2_logical_blocks b ON e.block_id=b.id "
            "WHERE b.profile_id=? AND b.block_index=? AND e.kind='curve_text'", (pid, bi)).fetchall())
        miss = [n for n in ns if n not in ct]
        print("pid=%d B%d 铭牌:%s 缺失%s" % (pid, bi, "OK" if not miss else "FAIL", miss))
