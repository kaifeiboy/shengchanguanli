import sys, io, json, os
sys.path.insert(0, "src/Platform/Modules/DrawingsV2/Scripts/vpdf")
import logical_blocks as LB

PDFS = {
    63: "E:/生产打标效果图/02-新约克86线控器效果图-25.7.15-Model.pdf",
    66: "E:/生产打标效果图/新约克86线控器(YCWA15NCBQ)效果图25.7.18-Model.pdf",
}

# 直接从库取 pdf 路径（profile 65 等）
import sqlite3
con = sqlite3.connect("data/drawingsv2.db"); con.execute("PRAGMA journal_mode=WAL")
cur = con.cursor()
db_pdf = {r[0]: r[1] for r in cur.execute("SELECT id, pdf_path FROM v2_drawing_profiles")}

violations = []   # 违反规则：curve_text 与 text_layer 原文重叠
total_dup_dropped = 0
nameplate_ok = {}

for pid in (63, 66, 67, 65):
    path = PDFS.get(pid) or db_pdf.get(pid)
    if not path or not os.path.exists(path):
        print("SKIP profile", pid, "no pdf")
        continue
    res = LB.extract_logical_blocks(path, 0, ocr_on=True)
    print("\n===== profile %d : %d blocks =====" % (pid, len(res["blocks"])))
    for blk in res["blocks"]:
        els = blk["elements"]
        tl = [e["text"] for e in els if e.get("source") == "text_layer" and e.get("text")]
        ct = [(e["text"], e.get("source")) for e in els if e.get("kind") == "curve_text" and e.get("text")]
        tl_norm = {LB._norm_text(t) for t in tl}
        # 规则校验：curve_text 不得与 text_layer 原文重叠
        for t, src in ct:
            if LB._ocr_conflicts_text_layer(LB._norm_text(t), tl_norm):
                violations.append((pid, blk["block_index"], t, src))
        # 报告 nameplate 块
        name = blk.get("name", "")
        if any(k in name for k in ("作业", "Nameplate", "铭牌")) or blk["block_index"] in (1, 3, 0, 4, 2):
            print("  B%d name=%r has_marking=%s | text_layer=%s | curve_text=%s" % (
                blk["block_index"], name, blk["has_marking"], tl[:4], [t for t, _ in ct][:6]))

# 校验 5 个已知恢复块是否仍含铭牌内容
checks = {
    63: {1: ["YCWA16NCWQ", "400-620-6607", "NFC便捷控制"],
         3: ["YCWA15NCWQ", "400-620-6607", "NFC便捷控制"]},
    66: {0: ["YCWA15NC", "400-620-6607", "NFC便捷控制"],
         4: ["YCWA15NC", "400-620-66"]},
    67: {2: ["YORK", "RS485"]},
}
print("\n===== 恢复内容校验 =====")
for pid, blkmap in checks.items():
    path = PDFS.get(pid) or db_pdf.get(pid)
    res = LB.extract_logical_blocks(path, 0, ocr_on=True)
    byidx = {b["block_index"]: b for b in res["blocks"]}
    for bi, needles in blkmap.items():
        els = byidx[bi]["elements"]
        ct_all = " ".join(e["text"] for e in els if e.get("kind") == "curve_text")
        missing = [n for n in needles if n not in ct_all]
        print("  profile %d B%d: %s (缺失 %s)" % (pid, bi, "OK" if not missing else "FAIL", missing))
        nameplate_ok[(pid, bi)] = not missing

print("\n===== 规则违反统计 =====")
print("curve_text 与 text_layer 原文重叠的条目数:", len(violations))
for v in violations:
    print("  VIOLATION", v)

print("\n===== 结论 =====")
print("规则满足(无重叠):", len(violations) == 0)
print("5 恢复块全部含铭牌内容:", all(nameplate_ok.values()))
