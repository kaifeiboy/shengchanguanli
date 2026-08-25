#!/usr/bin/env python3
"""P1a 规则矩阵回归 — 量化当前所有图纸的文本分类现状（不依赖 OCR）。

从 DB 读出 drawings_blocks.RawText，逐行用 _is_cad_annotation 分类，
输出每图/每块的 ignore(忽略)/active(比对) 统计 + 可疑边界行 + 纯数字尺寸行覆盖。

目的：
  1. 建立"文本规则"基线（golden master），每次改规则前/后跑，自动 diff 两份报告。
  2. 暴露分散规则下的不一致（如纯数字尺寸标线 `4`/`3`/`2` 是否被忽略）。
  3. 不依赖 RapidOCR，managed Python 直接可跑，零风险。

用法:
  python rule_matrix_regression.py            # 跑全部 + 打印摘要
  python rule_matrix_regression.py --json     # 额外写出 baseline_rule_matrix.json
"""
import sqlite3, os, sys, json, re

# 项目根（脚本在 src/Platform/Modules/Drawings/）
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))  # workaaa/shengchanguanli
sys.path.insert(0, HERE)
os.chdir(HERE)

from diff_visualizer import (
    _is_cad_annotation, _is_dimension, _is_serial_number,
    _is_annotation_note, _is_lcd_display_text, _is_cad_stopword,
)

DB = os.path.join(ROOT, "data", "app.db")


def classify(t: str) -> dict:
    """返回该文本行的分类详情。"""
    res = {
        "text": t,
        "cad_anno": _is_cad_annotation(t),
        "dimension": _is_dimension(t),
        "serial": _is_serial_number(t),
        "annot_note": _is_annotation_note(t),
        "lcd": _is_lcd_display_text(t),
        "stopword": _is_cad_stopword(t),
    }
    # 细分忽略类别
    if res["cad_anno"]:
        cats = []
        if res["dimension"]: cats.append("尺寸")
        if res["serial"]: cats.append("序列号")
        if res["annot_note"]: cats.append("注释")
        if res["lcd"]: cats.append("LCD")
        if not cats: cats.append("未知(应查)")
        res["ignore_as"] = "+".join(cats)
        res["active"] = False
    else:
        res["ignore_as"] = ""
        res["active"] = True
    return res


def is_pure_number(t: str) -> bool:
    return bool(re.match(r'^\d+(\.\d+)?$', t.strip()))


def main():
    write_json = "--json" in sys.argv
    if not os.path.exists(DB):
        print(f"❌ DB 不存在: {DB}")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT DrawingId, BIdx, RawText, Icons FROM drawing_blocks "
        "ORDER BY DrawingId, BIdx"
    ).fetchall()
    conn.close()

    print("=" * 78)
    print("P1a 规则矩阵回归 — 当前文本分类现状（golden master 基线）")
    print("=" * 78)

    drawings = {}
    global_stats = {
        "total_blocks": 0,
        "total_lines": 0,
        "ignore_lines": 0,
        "active_lines": 0,
        "pure_number_lines": 0,
        "pure_number_ignored": 0,
        "pure_number_active": 0,   # ⚠️ 纯数字却没被忽略 = 潜在尺寸标线漏判
        "suspicious": [],          # 边界行：非明显CAD也非明显ACTIVE
    }

    for r in rows:
        did = r["DrawingId"]
        bidx = r["BIdx"]
        raw = (r["RawText"] or "").strip()
        if not raw:
            continue
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        if not lines:
            continue

        if did not in drawings:
            drawings[did] = {"blocks": {}, "ignore": 0, "active": 0, "lines": 0}
        blk = drawings[did]["blocks"].setdefault(bidx, {"ignore": [], "active": []})

        for t in lines:
            c = classify(t)
            global_stats["total_lines"] += 1
            drawings[did]["lines"] += 1
            if c["cad_anno"]:
                global_stats["ignore_lines"] += 1
                drawings[did]["ignore"] += 1
                blk["ignore"].append(c["ignore_as"])
            else:
                global_stats["active_lines"] += 1
                drawings[did]["active"] += 1
                blk["active"].append(t)

            # 纯数字尺寸标线专项（对应 `4`/`3`/`2`/`16`/`52.9` 类问题）
            if is_pure_number(t):
                global_stats["pure_number_lines"] += 1
                if c["cad_anno"]:
                    global_stats["pure_number_ignored"] += 1
                else:
                    global_stats["pure_number_active"] += 1
                    global_stats["suspicious"].append(
                        {"drawing": did, "block": bidx, "text": t,
                         "reason": "纯数字但未被忽略(尺寸标线漏判?)"})

            # 边界行：非明显 CAD（无 dim/serial/annot/lcd/stopword）也非明显 ACTIVE（无型号/热线特征）
            if (not c["cad_anno"]) and not re.search(r'[A-Za-z]{2,}', t) and len(t) <= 6:
                # 短、无字母、非CAD → 既不像型号也不像明确尺寸，人工复查
                global_stats["suspicious"].append(
                    {"drawing": did, "block": bidx, "text": t,
                     "reason": "短文本无字母非CAD=边界case"})

    # ── 打印摘要 ──
    print(f"\n总块数: {global_stats['total_blocks']} (有效块) | 总行数: {global_stats['total_lines']}")
    print(f"忽略(CAD/尺寸/序列号/LCD): {global_stats['ignore_lines']}")
    print(f"比对(ACTIVE):              {global_stats['active_lines']}")
    print(f"\n纯数字尺寸行: {global_stats['pure_number_lines']} → 已忽略 {global_stats['pure_number_ignored']} / 漏判(ACTIVE) {global_stats['pure_number_active']}")
    print(f"\n⚠️ 可疑边界行: {len(global_stats['suspicious'])}")
    for s in global_stats["suspicious"][:40]:
        print(f"   did={s['drawing']} blk={s['block']} [{s['text']}] — {s['reason']}")
    if len(global_stats["suspicious"]) > 40:
        print(f"   ... 其余 {len(global_stats['suspicious'])-40} 条略")

    # 每图汇总
    print(f"\n{'─'*78}")
    print(f"{'图纸':<8} {'块':<5} {'行':<6} {'忽略':<6} {'比对':<6} {'图标'}")
    print(f"{'─'*78}")
    for did in sorted(drawings.keys()):
        d = drawings[did]
        nblk = len(d["blocks"])
        # 图标数（从 Icons 字段粗略统计，这里只数块数有图标）
        print(f"{did:<8} {nblk:<5} {d['lines']:<6} {d['ignore']:<6} {d['active']:<6}")

    # ── 写出 JSON 基线 ──
    if write_json:
        out = {
            "generated_by": "rule_matrix_regression.py (P1a)",
            "global": global_stats,
            "drawings": {str(k): v for k, v in drawings.items()},
        }
        out_path = os.path.join(ROOT, "data", "seg", "_baseline_rule_matrix.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 基线已写出: {out_path}")

    print("\n" + "=" * 78)
    print("说明: 本报告量化'文本规则'现状。纯数字尺寸行漏判(ACTIVE) + 边界case")
    print("是后续 P3 几何识别 / P2 规则中心化的重点。OCR 产物行(如块图多检的 `4`)")
    print("需 P1b 真实匹配回归覆盖（依赖 RapidOCR 或历史照片）。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
