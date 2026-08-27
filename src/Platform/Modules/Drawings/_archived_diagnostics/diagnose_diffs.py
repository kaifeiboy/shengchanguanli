#!/usr/bin/env python3
"""多产品差异归因诊断 —— 对每款产品的每个块跑匹配，对每处 R/Y 输出详细归因。

用法:
  python diagnose_diffs.py                    # 全量诊断(所有 did)
  python diagnose_diffs.py --dids 123,124,132  # 指定 did
  python diagnose_diffs.py --did 123 --bidx 4  # 单块深度诊断

输出: JSON + 控制台摘要，按根因分类:
  - OCR_QUALITY: OCR 读错/漏读/乱码
  - RULE_GAP: 应被忽略但未被任何规则覆盖
  - ROI_CROP: _auto_roi 裁掉了关键区域
  - TEXT_NORMALIZE: 归一化后仍不匹配(需增强容错)
  - GENUINE: 真实差异(确实缺/多标)
"""
import sqlite3, os, sys, json, argparse, traceback, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, HERE)
os.chdir(HERE)

from diff_visualizer import (
    run, _ocr_image, _norm_text, _texts_match_strict,
    _is_cad_annotation, _is_dimension, _is_serial_number,
    _is_lcd_display_text, _is_annotation_note, _is_cad_stopword,
    _is_ocr_noise, _is_qr_or_icon,
)

DB = os.path.join(ROOT, "data", "app.db")
SEG = os.path.join(ROOT, "data", "seg")
OUT_DIR = os.path.join(SEG, "_diagnose")
os.makedirs(OUT_DIR, exist_ok=True)


def db_blocks(dids=None):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = "SELECT DrawingId,BIdx,RawText,Icons FROM drawing_blocks ORDER BY DrawingId,BIdx"
    if dids:
        placeholders = ",".join("?" for _ in dids)
        sql = f"WHERE DrawingId IN ({placeholders}) " + sql.split("ORDER")[1]
        rows = conn.execute(f"SELECT DrawingId,BIdx,RawText,Icons FROM drawing_blocks WHERE DrawingId IN ({placeholders}) ORDER BY DrawingId,BIdx", dids).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def analyze_one_difference(text, side, all_block_texts, all_photo_texts):
    """对单条差异文本做归因分析，返回 (category, detail)。

    side = 'photo' (黄框: 照片有块无) 或 'block' (红框: 块有照无)
    """
    t_norm = _norm_text(text)
    cat = "UNKNOWN"
    details = []

    # 1) 检查是否应被序列号规则覆盖但没覆盖
    if _is_serial_number(text):
        cat = "RULE_GAP_SERIAL"
        details.append(f"序列号 '{text}' 未被 silentSkipped 处理")
        return cat, details

    # 2) 检查是否应被 CAD 规则覆盖
    if _is_cad_annotation(text):
        cat = "RULE_GAP_CAD"
        if _is_dimension(text):
            details.append(f"尺寸文本 '{text}' 未灰化")
        elif _is_lcd_display_text(text):
            details.append(f"LCD屏显 '{text}' 未灰化")
        elif _is_annotation_note(text):
            details.append(f"注释词 '{text}' 未灰化")
        else:
            details.append(f"CAD标注 '{text}' 未灰化(_is_cad_annotation=True 但子规则不明)")
        return cat, details

    # 3) 检查是否含 CAD 停用词
    if _is_cad_stopword(text):
        cat = "RULE_GAP_STOPWORD"
        details.append(f"含停用词 '{text}' 未灰化")
        return cat, details

    # 4) 检查是否是 OCR 噪声
    if _is_ocr_noise(text):
        cat = "RULE_GAP_NOISE"
        details.append(f"OCR噪声 '{text}' 未灰化")
        return cat, details

    # 5) 检查与对面文本的相似度（找最接近的）
    pool = all_block_texts if side == "photo" else all_photo_texts
    best_sim = 0
    best_match = ""
    for other in pool:
        o_norm = _norm_text(other)
        # Jaccard
        sa, sb = set(t_norm.lower()), set(o_norm.lower())
        jaccard = len(sa & sb) / max(len(sa | sb), 1)
        # LCS
        m, n = len(t_norm), len(o_norm)
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if t_norm[i-1] == o_norm[j-1]:
                    dp[i][j] = dp[i-1][j-1] + 1
                else:
                    dp[i][j] = max(dp[i-1][j], dp[i][j-2] if j >= 2 else 0)
        lcs_len = dp[m][n]
        lcs_ratio = lcs_len * 2 / max(m + n, 1)
        sim = max(jaccard, lcs_ratio)
        if sim > best_sim:
            best_sim = sim
            best_match = other

    if best_sim >= 0.5:
        cat = "TEXT_NORMALIZE"
        details.append(f"与 '{best_match}' 相似度={best_sim:.2f} 但未匹配(需增强容错)")
    elif best_sim >= 0.3:
        cat = "OCR_QUALITY"
        details.append(f"与最接近的 '{best_match}' 相似度={best_sim:.2f}(可能OCR读错)")
    else:
        # 检查是否是纯垃圾文本
        ascii_ratio = sum(1 for c in text if c.isascii()) / max(len(text), 1)
        if ascii_ratio < 0.5 and len(text) <= 4:
            cat = "OCR_QUALITY"
            details.append(f"短CJK垃圾 '{text}' 可能是OCR误检")
        else:
            cat = "GENUINE"
            details.append(f"疑似真实差异(最接近 '{best_match}' sim={best_sim:.2f})")

    return cat, details


def run_diagnosis(dids=None, single_did=None, single_bidx=None):
    blocks = db_blocks(dids)
    results = []
    summary = {"total": 0, "R": 0, "Y": 0, "G": 0, "Gray": 0, "by_category": {}}

    for row in blocks:
        did = row["DrawingId"]
        bidx = row["BIdx"]
        raw = row["RawText"]
        icons_str = row["Icons"]

        if single_did and did != single_did:
            continue
        if single_bidx is not None and bidx != single_bidx:
            continue

        block_png = os.path.join(SEG, str(did), "blocks", f"{did}_blk_{bidx:02d}.png")
        if not os.path.exists(block_png):
            continue

        # 自比对模式：照片=块图本身（隔离 OCR+规则问题，排除拍摄因素）
        photo_path = block_png

        try:
            icons = []
            if isinstance(icons_str, str) and icons_str.strip():
                try:
                    icons = json.loads(icons_str)
                except Exception:
                    icons = []

            block_ocr = _ocr_image(block_png)
            res = run(photo_path, block_png,
                      os.path.join(OUT_DIR, f"diag_{did}_{bidx:02d}.jpg"),
                      block_bbox_cache=block_ocr,
                      block_text_override=raw,
                      icon_regions_override=icons)

            if not res.get("success"):
                results.append({"did": did, "bidx": bidx, "error": res.get("error")})
                continue

            r_texts = res.get("blockExclusive", [])
            y_texts = res.get("photoExclusive", [])
            g_count = len(res.get("greenRegions", []))
            gray_count = len(res.get("grayRegions", []))

            # 收集所有文本用于归因
            all_photo = [l["text"] for l in block_ocr] if block_ocr else []
            all_block = raw.split("\n") if isinstance(raw, str) else []

            # 对每条红/黄差异做归因
            r_analysis = []
            for rt in r_texts:
                cat, det = analyze_one_difference(rt, "block", all_block, all_photo)
                r_analysis.append({"text": rt, "category": cat, "detail": "; ".join(det)})
                summary["by_category"][cat] = summary["by_category"].get(cat, 0) + 1

            y_analysis = []
            for yt in y_texts:
                cat, det = analyze_one_difference(yt, "photo", all_block, all_photo)
                y_analysis.append({"text": yt, "category": cat, "detail": "; ".join(det)})
                summary["by_category"][cat] = summary["by_category"].get(cat, 0) + 1

            entry = {
                "did": did, "bidx": bidx,
                "R": len(r_texts), "Y": len(y_texts),
                "G": g_count, "Gray": gray_count,
                "red_details": r_analysis,
                "yellow_details": y_analysis,
                "silentSkipped": res.get("silentSkipped", []),
            }
            results.append(entry)

            summary["total"] += 1
            summary["R"] += len(r_texts)
            summary["Y"] += len(y_texts)
            summary["G"] += g_count
            summary["Gray"] += gray_count

            # 实时打印
            status = "OK" if len(r_texts) == 0 and len(y_texts) == 0 else f"R{len(r_texts)}Y{len(y_texts)}"
            print(f"  did={did} blk={bidx}  G={g_count} Gray={gray_count} [{status}]")
            if r_analysis:
                for ra in r_analysis[:3]:
                    print(f"    RED   {ra['text'][:40]:40s}  [{ra['category']}]")
            if y_analysis:
                for ya in y_analysis[:3]:
                    print(f"    YELLOW {ya['text'][:40]:40s}  [{ya['category']}]")

        except Exception as e:
            traceback.print_exc()
            results.append({"did": did, "bidx": bidx, "error": str(e)[:200]})

    # 写结果
    out_json = os.path.join(OUT_DIR, f"diagnosis_{int(time.time())}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "details": results}, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"诊断完成: {summary['total']} 块")
    print(f"  绿框(G)={summary['G']}  灰框(Gray)={summary['Gray']}")
    print(f"  红框(R)={summary['R']}  黄框(Y)={summary['Y']}")
    print(f"\n按根因分类:")
    for cat, cnt in sorted(summary["by_category"].items(), key=lambda x: -x[1]):
        print(f"  {cat:25s}: {cnt}")
    print(f"\n详情写入: {out_json}")

    return results, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="多产品差异归因诊断")
    parser.add_argument("--dids", type=str, help="逗号分隔的 drawingId 列表")
    parser.add_argument("--did", type=int, help="单个 drawingId")
    parser.add_argument("--bidx", type=int, help="单个 blockIndex(需配合 --did)")
    args = parser.parse_args()

    dids = None
    if args.dids:
        dids = [int(x.strip()) for x in args.dids.split(",")]
    elif args.did:
        dids = [args.did]

    run_diagnosis(dids=dids, single_did=args.did, single_bidx=args.bidx)
