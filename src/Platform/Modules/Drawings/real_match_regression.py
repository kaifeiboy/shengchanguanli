#!/usr/bin/env python3
"""P1b 真实匹配回归 — 全量 27 图端到端差异量化（复现 `4` 等 OCR 多检问题）。

照片输入 = 块图 PNG 自身（自比对模式，无需真实照片即可复现 OCR 多检 `4`/`3`/`2`）。
对 did=115 额外用真实照片（vqa_front/bottom/diag_front）跑真实模式。

输出 P1b 报告 JSON + 摘要：每图每块 R/Y/G/Gray/Icon 计数 + 所有 red/yellow 文本。

用法:
  python real_match_regression.py                 # 全量
  python real_match_regression.py --did 123 --bidx 4   # 单块 quick test
"""
import sqlite3, os, sys, json, argparse, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))  # workaaa/shengchanguanli
sys.path.insert(0, HERE)
os.chdir(HERE)

from diff_visualizer import run, _ocr_image

DB = os.path.join(ROOT, "data", "app.db")
SEG = os.path.join(ROOT, "data", "seg")

REAL_PHOTOS_115 = {
    "front": os.path.join(SEG, "115", "vqa_front_photo.jpg"),
    "bottom": os.path.join(SEG, "115", "vqa_bottom_photo.jpg"),
    "diag": os.path.join(SEG, "115", "diag_front_real.jpg"),
}


def db_blocks():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT DrawingId,BIdx,RawText,Icons FROM drawing_blocks ORDER BY DrawingId,BIdx"
    ).fetchall()
    conn.close()
    return rows


def run_one(photo, block_png, raw, icons, mode, did, bidx, out):
    """跑单块匹配，返回结果 dict 或 error。"""
    # DB 的 Icons 是 JSON 字符串，需解析为 list[list[int]]（CLI 路径由 main 解析，直调需手动）
    if isinstance(icons, str) and icons.strip():
        try:
            icons = json.loads(icons)
        except Exception:
            icons = []
    elif not isinstance(icons, list):
        icons = []
    try:
        # 块侧 OCR 一次作为 bbox 缓存（避免与照片侧重复 OCR，全量提速一倍）
        try:
            block_ocr = _ocr_image(block_png)
        except Exception:
            block_ocr = None
        res = run(photo, block_png, out,
                  block_text_override=raw,
                  photo_text_override=None,      # ← 让 Python 跑 OCR(复现 `4` 多检)
                  block_bbox_cache=block_ocr,
                  icon_regions_override=icons)
        if not res.get("success"):
            return {"did": did, "bidx": bidx, "mode": mode, "error": res.get("error")}
        return {
            "did": did, "bidx": bidx, "mode": mode,
            "R": len(res.get("redRegions", [])),
            "Y": len(res.get("yellowRegions", [])),
            "G": len(res.get("greenRegions", [])),
            "Gray": len(res.get("grayRegions", [])),
            "red_text": res.get("blockExclusive", []),
            "yellow_text": res.get("photoExclusive", []),
            "icon_missing": len(res.get("iconMissingRegions", [])),
            "icon_extra": len(res.get("iconExtraRegions", [])),
        }
    except Exception as e:
        traceback.print_exc()
        return {"did": did, "bidx": bidx, "mode": mode, "error": str(e)[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--did", type=int, default=None)
    ap.add_argument("--bidx", type=int, default=None)
    args = ap.parse_args()

    rows = db_blocks()
    report = {"blocks": [], "summary": {}}
    total_r = total_y = total_g = total_gray = 0
    tmp = os.path.join(SEG, "_p1b_tmp")
    os.makedirs(tmp, exist_ok=True)

    # 单块模式
    if args.did is not None:
        target = [r for r in rows if r["DrawingId"] == args.did and (args.bidx is None or r["BIdx"] == args.bidx)]
    else:
        target = rows

    for r in target:
        did, bidx = r["DrawingId"], r["BIdx"]
        raw = (r["RawText"] or "").strip()
        if not raw:
            continue
        block_png = os.path.join(SEG, str(did), "blocks", f"{did}_blk_{bidx:02d}.png")
        if not os.path.exists(block_png):
            continue
        icons = r["Icons"] or ""
        out = os.path.join(tmp, f"{did}_{bidx}.jpg")

        # 自比对模式：照片=块图（复现 `4`）
        res = run_one(block_png, block_png, raw, icons, "selfblock", did, bidx, out)
        report["blocks"].append(res)
        if "R" in res:
            total_r += res["R"]; total_y += res["Y"]; total_g += res["G"]; total_gray += res["Gray"]

    # did=115 真实照片模式
    if args.did is None or args.did == 115:
        conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
        rows115 = conn.execute(
            "SELECT BIdx,RawText,Icons FROM drawing_blocks WHERE DrawingId=115 ORDER BY BIdx"
        ).fetchall()
        conn.close()
        for tag, photo in REAL_PHOTOS_115.items():
            if not os.path.exists(photo):
                continue
            for r in rows115:
                bidx = r["BIdx"]
                raw = (r["RawText"] or "").strip()
                if not raw:
                    continue
                block_png = os.path.join(SEG, "115", "blocks", f"115_blk_{bidx:02d}.png")
                if not os.path.exists(block_png):
                    continue
                out = os.path.join(tmp, f"115_real_{tag}_{bidx}.jpg")
                res = run_one(photo, block_png, raw, r["Icons"] or "", f"real_{tag}", 115, bidx, out)
                report["blocks"].append(res)
                if "R" in res:
                    total_r += res["R"]; total_y += res["Y"]; total_g += res["G"]; total_gray += res["Gray"]

    report["summary"] = {
        "total_R": total_r, "total_Y": total_y, "total_G": total_g,
        "total_Gray": total_gray, "blocks": len(report["blocks"]),
    }
    out_path = os.path.join(SEG, "_baseline_real_match.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"✅ P1b 报告: {out_path}")
    print(f"汇总: R={total_r} Y={total_y} G={total_g} Gray={total_gray} 块数={len(report['blocks'])}")
    print("\n有差异(red/yellow)的块:")
    for b in report["blocks"]:
        if b.get("R", 0) > 0 or b.get("Y", 0) > 0:
            print(f"  did={b['did']} blk={b['bidx']} [{b.get('mode')}] R={b.get('R')} Y={b.get('Y')} "
                  f"red={b.get('red_text')} yellow={b.get('yellow_text')}")


if __name__ == "__main__":
    main()
