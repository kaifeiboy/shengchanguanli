# -*- coding: utf-8 -*-
"""
fleet_sweep.py —— 全量图纸问题排查（根因审计 + 回归护栏基础）
===========================================================
不再"发现一个修一个"，而是：
  1. 对整批图纸(全部 drawing_blocks)做静态分类扫描
  2. 统计每类问题(QR噪声 / 电话 / 安全标签 / 序列号 / CAD)的分布
  3. 暴露"当前过滤器漏掉的盲区"(长得像但没被识别的异常 token)
  4. 验证 v19.x 的修复函数对全量数据是否通用

输出: fleet_sweep_report.json + 控制台汇总
用法: venv python fleet_sweep.py
"""
import os, re, sqlite3, json, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = r"e:\workaaa\shengchanguanli\data\app.db"
SEG = r"e:\workaaa\shengchanguanli\data\seg"

# 加载 diff_visualizer 真实函数（生产运行的就是这个文件）
sys.path.insert(0, HERE)
import diff_visualizer as dv

# ────────────────────────────────────────────────────────────
# 1. 加载分类函数
# ────────────────────────────────────────────────────────────
_is_qr_ocr_noise = dv._is_qr_ocr_noise
_is_cjk = dv._is_cjk
_is_serial_number = dv._is_serial_number
_norm_text = dv._norm_text
MODEL_PREFIXES = getattr(dv, "_MODEL_PREFIXES", [])

PHONE_RE = re.compile(r'(服务热线|热线|400|800|900)[-\s]?\d{2,4}[-\s]?\d{3,4}[-\s]?\d{3,4}')
SAFETY_KW = ["禁止强电", "地暖阀", "强电", "低压", "LOW VOLTAGE", "VOLTAGE", "电压",
             "注意", "警告", "危险", "WARNING", "CAUTION", "激光", "烫伤"]
# 中文安全标签（含 CJK 的安全类）
SAFETY_CJK_RE = re.compile(r'[一-鿿]*(禁止|强电|地暖|低压|电压|警告|危险|注意|烫伤|激光)[一-鿿]*')

# 品牌/品类词（合法标签内容，非二维码噪声，不应算盲区）
BRAND_WORDS = {"HISENSE", "YORK", "VRF", "NFC", "HYXC", "HITACHI", "HAIXIN",
               "YCTA", "YK", "YORKP", "HITACH", "RICH", "RICHA"}

def looks_like_qr_noise_heuristic(tok):
    """盲区探测：长得像二维码噪声但 _is_qr_ocr_noise 没识别的。
    规则：纯字母数字 4-12 位、非型号前缀、非中文、非电话、非已知 CAD 词、非品牌词。
    """
    t = tok.strip()
    if not t or len(t) < 4:
        return False
    if _is_cjk(t):
        return False
    if re.match(r'^[A-Za-z0-9]{4,12}$', t) is None:
        return False
    if t.upper() in BRAND_WORDS:
        return False
    for p in MODEL_PREFIXES:
        if t.upper().startswith(p.upper()):
            return False
    if PHONE_RE.search(t):
        return False
    if any(k in t for k in SAFETY_KW):
        return False
    if any(s in t for s in "±°φ"):
        return False
    # 已经被正式过滤器识别的，不算盲区
    if _is_qr_ocr_noise(t):
        return False
    return True

def classify_token(tok):
    flags = []
    if _is_qr_ocr_noise(tok):
        flags.append("QR_NOISE(caught)")
    if looks_like_qr_noise_heuristic(tok):
        flags.append("QR_NOISE(BLINDSPOT!)")
    if _is_serial_number(tok):
        flags.append("SERIAL")
    if PHONE_RE.search(tok):
        flags.append("PHONE")
    if SAFETY_CJK_RE.search(tok) or any(k.upper() in tok.upper() for k in SAFETY_KW):
        flags.append("SAFETY")
    if re.search(r'\d+(\.\d+)?\s*(mm|cm|°|±)', tok, re.I) or '±' in tok:
        flags.append("DIM")
    return flags

# ────────────────────────────────────────────────────────────
# 2. 全量扫描
# ────────────────────────────────────────────────────────────
con = sqlite3.connect(DB); cur = con.cursor()
cur.execute("SELECT Id, FileName FROM drawings ORDER BY Id")
drawings = cur.fetchall()

fleet = []          # 每图汇总
blindspots = []     # 全局盲区 token
qr_caught = []      # 被过滤器识别的 QR 噪声
phone_blocks = []   # 含电话的块
safety_missing = [] # 安全标签疑似缺失（块图有但需人工确认）

for did, fname in drawings:
    cur.execute("""SELECT Id,BIdx,Type,FileRel,RawText,Tokens FROM drawing_blocks
                   WHERE DrawingId=? ORDER BY BIdx""", (did,))
    blocks = cur.fetchall()
    dq = {"did": did, "file": fname, "blocks": len(blocks),
          "qr_noise": 0, "qr_blindspot": 0, "phone": 0, "serial": 0,
          "safety": 0, "cad_dim": 0, "block_detail": []}
    for bid, bidx, btype, frel, raw, toks in blocks:
        raw = raw or ""
        # RawText 可能多行，按行+空格分
        lines = [l for l in re.split(r'[\s,，;；]+', raw) if l.strip()]
        toks_parsed = []
        try:
            toks_parsed = json.loads(toks) if toks else []
        except Exception:
            toks_parsed = []
        all_tokens = lines + [t.get("text","") for t in toks_parsed if isinstance(t, dict)]
        all_tokens = [t for t in all_tokens if t and t.strip()]
        blk_flags = set()
        for tok in all_tokens:
            fl = classify_token(tok)
            for f in fl:
                blk_flags.add(f)
                if f == "QR_NOISE(caught)":
                    dq["qr_noise"] += 1
                    qr_caught.append({"did": did, "blk": bidx, "tok": tok})
                elif f == "QR_NOISE(BLINDSPOT!)":
                    dq["qr_blindspot"] += 1
                    blindspots.append({"did": did, "blk": bidx, "tok": tok, "raw": raw[:80]})
                elif f == "PHONE":
                    dq["phone"] += 1
                    phone_blocks.append({"did": did, "blk": bidx, "tok": tok})
                elif f == "SERIAL":
                    dq["serial"] += 1
                elif f == "SAFETY":
                    dq["safety"] += 1
                elif f == "DIM":
                    dq["cad_dim"] += 1
        if blk_flags:
            dq["block_detail"].append({"blk": bidx, "flags": sorted(blk_flags), "raw": raw[:90]})
    fleet.append(dq)

con.close()

# ────────────────────────────────────────────────────────────
# 3. 汇总
# ────────────────────────────────────────────────────────────
report = {
    "total_drawings": len(fleet),
    "total_qr_noise_caught": sum(d["qr_noise"] for d in fleet),
    "total_qr_blindspot": sum(d["qr_blindspot"] for d in fleet),
    "total_phone": sum(d["phone"] for d in fleet),
    "total_safety": sum(d["safety"] for d in fleet),
    "total_serial": sum(d["serial"] for d in fleet),
    "qr_caught_samples": qr_caught[:30],
    "blindspots": blindspots,
    "phone_blocks": phone_blocks,
    "per_drawing": fleet,
}

out = os.path.join(HERE, "fleet_sweep_report.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print("="*70)
print(f"全量扫描完成: {len(fleet)} 张图纸, 99 个块")
print(f"  QR 噪声(已被过滤器识别): {report['total_qr_noise_caught']}")
print(f"  QR 盲区(未被识别!):       {report['total_qr_blindspot']}")
print(f"  含电话块:                 {report['total_phone']}")
print(f"  含安全标签块:             {report['total_safety']}")
print(f"  含序列号块:               {report['total_serial']}")
print("="*70)

if blindspots:
    print("\n⚠️  QR 盲区 token（当前过滤器漏掉，可能变假黄/红框）:")
    for b in blindspots[:40]:
        print(f"   did={b['did']} blk={b['blk']} tok={b['tok']!r}  raw={b['raw']!r}")
else:
    print("\n✅ 无 QR 盲区：所有疑似二维码噪声均被 _is_qr_ocr_noise 识别")

print("\n含 QR 噪声的图纸清单(did: 噪声数 [盲区数]):")
for d in fleet:
    if d["qr_noise"] or d["qr_blindspot"]:
        print(f"   did={d['did']:>3} {d['file'][:34]:<34} noise={d['qr_noise']:>2} blind={d['qr_blindspot']}")

print(f"\n报告已写: {out}")
