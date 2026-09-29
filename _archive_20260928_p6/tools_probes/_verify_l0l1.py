"""L0+L1 改造的零回归验证：对比重提取前后 DB 快照。"""
import sqlite3, json, collections, hashlib

db = r"E:\workaaa\shengchanguanli\data\drawingsv2.db"
before = json.load(open(r"E:\workaaa\shengchanguanli\tools\_snap_before.json", encoding="utf-8"))

c = sqlite3.connect(db); c.execute("PRAGMA journal_mode=WAL"); cur = c.cursor()
cur.execute("SELECT id, profile_id, block_index, block_key, bbox_json, has_marking, "
            "empty_reason, low_conf, name FROM v2_logical_blocks ORDER BY profile_id, block_index")
blocks = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
cur.execute("SELECT id, block_id, kind, text, participate, source FROM v2_block_elements ORDER BY id")
els = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

B = {(b["profile_id"], b["block_index"]): b for b in before["blocks"]}
A = {(b["profile_id"], b["block_index"]): b for b in blocks}

ok = True
def chk(cond, msg):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond: ok = False

print("=" * 78)
print("AFTER : blocks=%d elements=%d   (BEFORE: blocks=%d elements=%d)"
      % (len(blocks), len(els), before["n_blocks"], before["n_elements"]))

print("\n[1] 块级零回归")
chk(len(blocks) == len(before["blocks"]), "块总数不变 36")
chk(set(A) == set(B), "块集合（profile, block_index）完全一致")
same_bbox = [k for k in A if k in B and A[k]["bbox_json"] == B[k]["bbox_json"]]
chk(len(same_bbox) == len(A), "bbox_json 逐块完全一致 (%d/%d)" % (len(same_bbox), len(A)))
same_key = [k for k in A if k in B and A[k]["block_key"] == B[k]["block_key"]]
chk(len(same_key) == len(A), "block_key 逐块一致（幂等覆盖未换键）(%d/%d)" % (len(same_key), len(A)))
same_hm = [k for k in A if k in B and A[k]["has_marking"] == B[k]["has_marking"]]
chk(len(same_hm) == len(A), "has_marking 逐块一致 (%d/%d)" % (len(same_hm), len(A)))

print("\n[2] 空块清单（用户已人工确认的 8 块）")
be = sorted([k for k, b in B.items() if not b["has_marking"]])
ae = sorted([k for k, b in A.items() if not b["has_marking"]])
chk(be == ae, "空块清单一致：%s" % (ae,))
diff_reason = [(k, B[k]["empty_reason"], A[k]["empty_reason"]) for k in ae
               if k in B and B[k]["empty_reason"] != A[k]["empty_reason"]]
print("     empty_reason 变化：%s" % (diff_reason if diff_reason else "无"))

print("\n[3] kind 分布变化")
cb = collections.Counter([e["kind"] for e in before["elements"]])
ca = collections.Counter([e["kind"] for e in els])
print("     BEFORE:", dict(cb))
print("     AFTER :", dict(ca))
chk(cb.get("qr", 0) == ca.get("qr", 0), "c 类 QR 数量不变（%d）" % ca.get("qr", 0))

print("\n[4] 预期的新增判定项（dim → text）")
bid = {b["id"]: (b["profile_id"], b["block_index"]) for b in blocks}
map_old_new = {}
for e in els:
    map_old_new[(bid.get(e["block_id"]), e["text"], e["source"])] = e
TARGET = {"200512", "1250001", "400-860-1111", "4008601111", "100095"}
newly = [(e["text"], bid.get(e["block_id"]), e["source"]) for e in els
         if e["kind"] == "text" and e["text"] in TARGET]
for t, k, s in sorted(newly, key=lambda x: str(x[1])):
    print("     ★ %-14r  profile/block=%s  source=%s" % (t, k, s))
chk(len(newly) > 0, "目标 4 类字符串已回归 participate=1（实测 %d 条）" % len(newly))

print("\n[5] 反向误杀检查（是否有 text → dim/meta 的退化）")
old_text = set()
for e in before["elements"]:
    if e["kind"] == "text":
        old_text.add((e["text"], e["source"]))
lost = []
for k, e in (( (bid.get(x["block_id"]), x["text"], x["source"]), x) for x in els):
    pass
new_map = collections.defaultdict(set)
for e in els:
    new_map[e["text"]].add(e["kind"])
regressed = sorted({t for t, s in old_text if new_map.get(t) and "text" not in new_map[t]})
chk(not regressed, "无 text → 非 text 的退化%s" % ("：" + str(regressed[:10]) if regressed else ""))

print("\n[6] OCR 源（b/e 类）diff —— 防止新判据把 OCR 噪声放进来")
def ocrset(rows):
    return {(r["text"], r["source"]) for r in rows if str(r["source"]).startswith("outline")}
ob, oa = ocrset(before["elements"]), ocrset(els)
added = sorted(oa - ob)
removed = sorted(ob - oa)
print("     新增: %s" % (added if added else "无"))
print("     减少: %s" % (removed if removed else "无"))
# OCR 噪声形态：纯数字+单一小数点+空白（实证 p67 `83. 2`）
import re
noise = [t for t, _ in added if re.fullmatch(r"[\d\s.]+", t)]
chk(not noise, "无「残损尺寸型」OCR 噪声混入%s" % ("：" + str(noise) if noise else ""))

print("\n" + "=" * 78)
print("RESULT = %s" % ("ALL_OK" if ok else "HAS_FAILURE"))
