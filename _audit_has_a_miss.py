# -*- coding: utf-8 -*-
"""一次性审计：has_a 门禁（logical_blocks.py L492-493）导致块级 OCR 被跳过的块中，
哪些块实际存在未提取的转曲打标内容。
对每个嫌疑块重跑与提取器同参数的 OCR（300/400dpi 双分辨率），把 OCR 有效文本
与块内已有元素文本对照，产出新增漏提清单。只读，不改库。
"""
import io, json, os, re, sqlite3, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "src", "Platform", "Modules", "DrawingsV2", "Scripts", "vpdf"))
import fitz
from logical_blocks import ocr_block, classify_text, _is_valid_marking, normalize

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "drawingsv2.db")

con = sqlite3.connect(DB); con.execute("PRAGMA journal_mode=WAL")
cur = con.cursor()
rows = cur.execute('''
SELECT p.id, p.drawing_key, p.pdf_path, b.id, b.page_index, b.block_index, b.name, b.bbox_json
FROM v2_logical_blocks b JOIN v2_drawing_profiles p ON b.profile_id=p.id
ORDER BY p.id, b.page_index, b.block_index''').fetchall()

# 嫌疑块：有 text_layer 参与 text 且无 outline_ocr 来源元素
suspects = []
for pid, key, pdf, bid, pg, bi, name, bbox in rows:
    els = cur.execute("SELECT kind, text, source, participate FROM v2_block_elements WHERE block_id=?", (bid,)).fetchall()
    has_a = any(k == 'text' and p_ == 1 and s == 'text_layer' for k, t, s, p_ in els)
    has_ocr = any((s or '').startswith('outline_ocr') for k, t, s, p_ in els)
    if has_a and not has_ocr:
        suspects.append((pid, key, pdf, bid, pg, bi, name, json.loads(bbox), els))

print("suspects:", len(suspects))
out = []
for pid, key, pdf, bid, pg, bi, name, bbox, els in suspects:
    doc = fitz.open(pdf)
    try:
        page = doc[pg]; page.remove_rotation()
        W, H = page.rect.width, page.rect.height
        rect = fitz.Rect(*bbox)
        # 与提取器一致：pad 6pt，300/400 双分辨率
        pad = 6.0
        orect = fitz.Rect(max(0, bbox[0]-pad), max(0, bbox[1]-pad), min(W, bbox[2]+pad), min(H, bbox[3]+pad))
        found = ocr_block(page, orect, dpi_list=(300, 400))
        existing = {normalize(t) for k, t, s, p_ in els if t}
        new_texts = []
        for txt, conf in found:
            if classify_text(txt) != "text":
                continue
            if not _is_valid_marking(txt):
                continue
            nt = normalize(txt)
            if not nt:
                continue
            # 与已有元素互相包含则视为已覆盖（容 OCR 识别差异）
            covered = any(nt in ex or ex in nt for ex in existing if len(ex) >= 4)
            if not covered:
                new_texts.append({"text": txt, "conf": round(conf, 3)})
        rec = {"profile_id": pid, "drawing": key, "page": pg + 1, "block_index": bi,
               "name": name, "missed": new_texts}
        flag = "MISS" if new_texts else "ok  "
        print(flag, pid, "P%d B%d" % (pg + 1, bi), repr(name)[:52],
              "->", [m["text"] for m in new_texts])
        out.append(rec)
    finally:
        doc.close()

with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "outputs", "audit_has_a_miss_20260929.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
n_miss = sum(1 for r in out if r["missed"])
print("SUMMARY: suspects=%d  with_missed_marking=%d" % (len(out), n_miss))
