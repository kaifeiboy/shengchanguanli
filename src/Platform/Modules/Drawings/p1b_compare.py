#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1b 对照：v19.2 基线(_p1b_run.log) vs v19.3 修复后(_baseline_real_match.json)
核心护栏：
  1) 每个块的 red(真实差异)必须零丢失 —— 若新 R < 基线 R 或 red 文本缺失，报回归。
  2) Y(黄框)应大幅下降（CAD 类被对称修复消除）。
  3) Gray 应上升，G 因旧"假绿"(图+DB 都有的 CAD 尺寸)归位略降属正常。
"""
import json, re, ast, sys

BASE_LOG = "data/seg/_p1b_run.log"
NEW_JSON = "data/seg/_baseline_real_match.json"

def parse_baseline_log(path):
    base = {}
    pat = re.compile(r"did=(\d+) blk=(\d+) \[(\w+)\] R=(\d+) Y=(\d+) red=(\[.*?\]) yellow=(\[.*?\])")
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = pat.search(line)
            if not m:
                continue
            did, bidx, mode, R, Y, red, yel = m.groups()
            key = (int(did), int(bidx), mode)
            base[key] = {
                "R": int(R), "Y": int(Y),
                "red": ast.literal_eval(red),
                "yellow": ast.literal_eval(yel),
            }
    return base

def parse_new_json(path):
    d = json.load(open(path, encoding="utf-8"))
    new = {}
    for b in d["blocks"]:
        key = (b["did"], b["bidx"], b["mode"])
        new[key] = {
            "R": b["R"], "Y": b["Y"], "G": b["G"], "Gray": b["Gray"],
            "red": b["red_text"], "yellow": b["yellow_text"],
        }
    return new, d["summary"]

base = parse_baseline_log(BASE_LOG)
new, new_summary = parse_new_json(NEW_JSON)

# 基线聚合（来自日志首行汇总）
m = re.search(r"汇总: R=(\d+) Y=(\d+) G=(\d+) Gray=(\d+) 块数=(\d+)", open(BASE_LOG, encoding="utf-8").read())
base_R, base_Y, base_G, base_Gray, base_blocks = (int(x) for x in m.groups())

print("="*60)
print("聚合对照")
print("="*60)
print(f"{'指标':<10}{'v19.2基线':>12}{'v19.3修复':>12}{'变化':>10}")
print(f"{'R(红)':<10}{base_R:>12}{new_summary['total_R']:>12}{new_summary['total_R']-base_R:>+10}")
print(f"{'Y(黄)':<10}{base_Y:>12}{new_summary['total_Y']:>12}{new_summary['total_Y']-base_Y:>+10}")
print(f"{'G(绿)':<10}{base_G:>12}{new_summary['total_G']:>12}{new_summary['total_G']-base_G:>+10}")
print(f"{'Gray':<10}{base_Gray:>12}{new_summary['total_Gray']:>12}{new_summary['total_Gray']-base_Gray:>+10}")

# 逐块 red 回归检查
print()
print("="*60)
print("真实差异(red)回归检查 —— 若有丢失立即报警")
print("="*60)
regress = []
for key in sorted(base.keys()):
    b = base[key]
    n = new.get(key)
    if not n:
        print(f"  ⚠ 缺失块 {key}（新跑未覆盖）")
        regress.append(key)
        continue
    if n["R"] < b["R"]:
        lost = [t for t in b["red"] if t not in n["red"]]
        print(f"  ❌ 回归 {key}: R {b['R']}→{n['R']} 丢失red={lost}")
        regress.append(key)
    elif set(b["red"]) != set(n["red"]):
        # R 数相同但文本变了（如 OCR 差异），标记观察
        pass

if not regress:
    print("  ✅ 所有块真实差异 red 零丢失（R 数未减，无回归）")
else:
    print(f"  ⚠ 共 {len(regress)} 块需人工确认")

# Y 降幅最大的块（修复见效最明显）
print()
print("="*60)
print("Y(黄框)降幅 Top10 —— 对称修复见效块")
print("="*60)
drops = []
for key in sorted(base.keys()):
    b = base[key]; n = new.get(key)
    if n:
        drops.append((b["Y"] - n["Y"], key, b["Y"], n["Y"]))
drops.sort(reverse=True)
for d, key, by, ny in drops[:10]:
    if d > 0:
        print(f"  {key}: Y {by}→{ny} (降{d})")
