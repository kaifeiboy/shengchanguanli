#!/usr/bin/env python3
# 对比 _before_fix.json(改动前/v19.28) 与 _after_fix.json(改动后/我的修复) 的逐块差异。
import json, os

SEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "seg")
SEG = os.path.abspath(SEG)

def load(p):
    with open(os.path.join(SEG, p), encoding="utf-8") as f:
        return json.load(f)

before = load("_before_fix.json")
after = load("_after_fix.json")

def key(b):
    return (b.get("did"), b.get("bidx"), b.get("mode"))

bb = {key(b): b for b in before["blocks"]}
ab = {key(b): b for b in after["blocks"]}

# 总览
def tot(d):
    s = d["summary"]
    return (s["total_R"], s["total_Y"], s["total_G"], s["total_Gray"], s["blocks"])

print("=== 总览 ===")
print(f"改动前(v19.28): R={before['summary']['total_R']} Y={before['summary']['total_Y']} "
      f"G={before['summary']['total_G']} Gray={before['summary']['total_Gray']} 块={before['summary']['blocks']}")
print(f"改动后(修复)  : R={after['summary']['total_R']} Y={after['summary']['total_Y']} "
      f"G={after['summary']['total_G']} Gray={after['summary']['total_Gray']} 块={after['summary']['blocks']}")
print(f"DELTA         : R={after['summary']['total_R']-before['summary']['total_R']:+d} "
      f"Y={after['summary']['total_Y']-before['summary']['total_Y']:+d} "
      f"G={after['summary']['total_G']-before['summary']['total_G']:+d} "
      f"Gray={after['summary']['total_Gray']-before['summary']['total_Gray']:+d}")

# 逐块差异
only_before = set(bb) - set(ab)
only_after = set(ab) - set(bb)
common = set(bb) & set(ab)

regress_r = []   # R 增加（潜在回归）
regress_y = []   # Y 增加
gray_increase = []  # Gray 增加（坏：新灰化）
gray_decrease = []  # Gray 减少（好：修复灰化）
g_increase = []  # G 增加
changed = []

for k in sorted(common):
    b0, b1 = bb[k], ab[k]
    if "R" not in b0 or "R" not in b1:
        continue
    dR = b1["R"] - b0["R"]
    dY = b1["Y"] - b0["Y"]
    dG = b1["G"] - b0["G"]
    dGray = b1["Gray"] - b0["Gray"]
    if dR or dY or dG or dGray:
        changed.append((k, dR, dY, dG, dGray, b0, b1))
        if dR > 0: regress_r.append((k, dR, b0, b1))
        if dY > 0: regress_y.append((k, dY, b0, b1))
        if dGray > 0: gray_increase.append((k, dGray, b0, b1))
        if dGray < 0: gray_decrease.append((k, dGray, b0, b1))
        if dG > 0: g_increase.append((k, dG, b0, b1))

print(f"\n=== 变更块数: {len(changed)} (common={len(common)}, only_before={len(only_before)}, only_after={len(only_after)}) ===")

print(f"\n--- [坏] Gray 增加(新灰化) 共 {len(gray_increase)} ---")
for k, d, b0, b1 in gray_increase:
    print(f"  {k} dGray={dGray} before(Gray={b0['Gray']}) after(Gray={b1['Gray']})")

print(f"\n--- [好] Gray 减少(修复灰化) 共 {len(gray_decrease)} ---")
for k, d, b0, b1 in gray_decrease:
    print(f"  {k} dGray={d} before(Gray={b0['Gray']}) after(Gray={b1['Gray']})")

print(f"\n--- [审视] R 增加 共 {len(regress_r)} ---")
for k, d, b0, b1 in regress_r:
    print(f"  {k} dR={d} before(R={b0['R']},red={b0.get('red_text')}) after(R={b1['R']},red={b1.get('red_text')})")

print(f"\n--- [审视] Y 增加 共 {len(regress_y)} ---")
for k, d, b0, b1 in regress_y:
    print(f"  {k} dY={d} before(Y={b0['Y']},yellow={b0.get('yellow_text')}) after(Y={b1['Y']},yellow={b1.get('yellow_text')})")

print(f"\n--- [好] G 增加 共 {len(g_increase)} ---")
for k, d, b0, b1 in g_increase:
    print(f"  {k} dG={d} before(G={b0['G']}) after(G={b1['G']})")

# 判定
print("\n=== 结论 ===")
if not gray_increase and not regress_r and not regress_y:
    print("✅ 零回归：仅 Gray 减少(修复灰化)、G 增加，R/Y 无变化。")
else:
    print(f"⚠️ 需审视：Gray新增={len(gray_increase)} R新增={len(regress_r)} Y新增={len(regress_y)}")
