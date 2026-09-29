# -*- coding: utf-8 -*-
"""离线标定：OCR 输入尺寸限制（V2_TEXT_MAX_SIDE）对「判定」与「耗时」的影响。

【数据来源】真实生产记录的 verdicts_json.expectedBbox —— 它就是 C# 下发给 Python
裁剪所用的框（MarkVerifier.ExpectedBbox = ViewNormMap[mark.Id] ?? m.NormBbox），
坐标同源，因此本标定可代表生产。

【判定零变化的判据】同一 crop 在不同阈值下，_verify_text 返回的
(text, conf) 多重集合完全一致 —— 因为下游 MarkVerifier 只吃 text/conf/位置，
文本集不变 ⇒ 判定链输入不变。

【两档】
  census  —— 只量 crop / canvas 尺寸分布，不跑 OCR（秒级）
  run     —— 逐 region 跑 0 / 1024 / 1280 / 1600 四档，比对文本集与耗时
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import Counter

import cv2
import numpy as np

SCRIPTS = r"E:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2\Scripts"
sys.path.insert(0, SCRIPTS)
ROOT = r"E:\workaaa\shengchanguanli"
DB = os.path.join(ROOT, "data", "drawingsv2.db")
OUT = os.path.join(ROOT, "data", "_exp", "textsize_calib.json")

from observe import verify as V, geometry as G  # noqa: E402

MAX_PHOTOS = int(os.environ.get("CAL_PHOTOS", "14"))
MAX_REGIONS = int(os.environ.get("CAL_REGIONS", "30"))
THRESHOLDS = [0, 1024, 1280, 1600]


def load_cases():
    """按 photo 去重取最近的生产记录，收集 Text 类 mark 的 expectedBbox。"""
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()
    rows = cur.execute(
        "SELECT id, profile_id, photo_path, verdicts_json, verify_ms FROM v2_compare_records "
        "WHERE usable=1 ORDER BY id DESC LIMIT 400").fetchall()
    con.close()

    by_photo, order = {}, []
    types = Counter()
    for rid, pid, path, vj, vms in rows:
        if not path or not os.path.exists(path):
            continue
        try:
            vs = json.loads(vj or "[]")
        except Exception:
            continue
        for v in vs:
            types[str(v.get("type"))] += 1
        regs = [v for v in vs
                if v.get("expectedBbox") and len(v["expectedBbox"]) == 4
                and str(v.get("type", "")).lower() in ("text", "group", "any", "")]
        if not regs:
            continue
        key = os.path.basename(path)
        if key not in by_photo:
            by_photo[key] = {"photo": path, "profile": pid, "record": rid,
                             "verify_ms": vms, "regions": regs[:MAX_REGIONS]}
            order.append(key)
        if len(order) >= MAX_PHOTOS:
            break
    print("verdict type 分布（最近 400 条记录）：", dict(types))
    return [by_photo[k] for k in order]


def read_photo(path):
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def canvas_size(crop):
    ch, cw = crop.shape[:2]
    pad = max(V.TEXT_PAD_MIN, int(max(ch, cw) * V.TEXT_PAD_RATIO))
    return (ch + 2 * pad, cw + 2 * pad)


def census(cases):
    crops, canvases = [], []
    for c in cases:
        bgr = read_photo(c["photo"])
        if bgr is None:
            print("  !! 读图失败", c["photo"])
            continue
        warped, _ = G.normalize(bgr, do_warp=True)
        for v in c["regions"]:
            crop, _ = V._crop_margin(warped, [float(x) for x in v["expectedBbox"]])
            if crop.size == 0:
                continue
            crops.append(max(crop.shape[:2]))
            canvases.append(max(canvas_size(crop)))
    crops.sort()
    canvases.sort()
    n = len(canvases)

    def pct(a, p):
        return a[min(len(a) - 1, int(len(a) * p))]

    print(f"\n=== 尺寸普查：{len(cases)} 张照片 / {n} 个 Text region ===")
    print(f"crop 长边   min={crops[0]} P50={pct(crops,.5)} P90={pct(crops,.9)} max={crops[-1]}")
    print(f"canvas 长边 min={canvases[0]} P50={pct(canvases,.5)} P90={pct(canvases,.9)} max={canvases[-1]}")
    for t in (1024, 1280, 1600):
        cut = sum(1 for x in canvases if x > t)
        print(f"  阈值 {t}: {cut}/{n} 个 region 会被缩放（{cut/max(n,1)*100:.0f}%）")
    return canvases


def _sig(out):
    return sorted((str(o["text"]), round(float(o["conf"]), 3)) for o in out)


def run_bbox(cases):
    """只比 0 vs 1280，额外比对 bbox（bbox → norm_bbox → Offset/容差，会翻状态）。"""
    WHY = 1280
    # 预热：首次 T.detect 含模型加载（实测首个 region 基线 18.6s vs 中位 1.1s）
    V.TEXT_MAX_SIDE = 0
    t0 = time.perf_counter()
    V._verify_text(np.zeros((96, 96, 3), np.uint8))
    print(f"预热完成 {(time.perf_counter()-t0)*1000:.0f}ms\n")

    recs = []
    for c in cases:
        bgr = read_photo(c["photo"])
        if bgr is None:
            continue
        warped, _ = G.normalize(bgr, do_warp=True)
        for ri, v in enumerate(c["regions"]):
            crop, _ = V._crop_margin(warped, [float(x) for x in v["expectedBbox"]])
            if crop.size == 0:
                continue
            row = {"photo": os.path.basename(c["photo"]), "region": ri,
                   "text": v.get("text"), "crop": list(crop.shape[:2]), "res": {}}
            for t in (0, WHY):
                V.TEXT_MAX_SIDE = t
                t0 = time.perf_counter()
                out = V._verify_text(crop)
                row["res"][str(t)] = {"ms": round((time.perf_counter() - t0) * 1000, 1),
                                      "sig": _sig(out),
                                      "bbox": {o["text"]: o["bbox"] for o in out}}
            recs.append(row)
            r0, r1 = row["res"]["0"], row["res"][str(WHY)]
            same = r0["sig"] == r1["sig"]
            print(f"[{len(recs):3d}] {row['photo'][:12]} r{ri} crop={row['crop']} "
                  f"ms {r0['ms']:7.0f} -> {r1['ms']:6.0f}  textSame={same}")
            sys.stdout.flush()

    json.dump(recs, open(OUT.replace(".json", "_bbox.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    b0 = sum(r["res"]["0"]["ms"] for r in recs)
    b1 = sum(r["res"][str(WHY)]["ms"] for r in recs)
    print(f"\n=== 汇总（{len(recs)} region，已预热）===")
    print(f"基线 {b0/1000:.1f}s  均值 {b0/len(recs):.0f}ms | "
          f"{WHY} {b1/1000:.1f}s  均值 {b1/len(recs):.0f}ms | 省 {(1-b1/b0)*100:.1f}%")

    ntext = sum(1 for r in recs if r["res"]["0"]["sig"] != r["res"][str(WHY)]["sig"])
    print(f"文本集/conf 不同：{ntext}/{len(recs)}")
    tgt_lost = []
    for r in recs:
        t = (r["text"] or "").strip()
        if not t:
            continue
        b = [x[0] for x in r["res"]["0"]["sig"]]
        n = [x[0] for x in r["res"][str(WHY)]["sig"]]
        if any(t in x or x in t for x in b) and not any(t in x or x in t for x in n):
            tgt_lost.append(r)
    print(f"**目标文本从有到无**（会翻判定）：{len(tgt_lost)}")
    for r in tgt_lost:
        print(f"   !! {r['photo'][:12]} r{r['region']} 目标={r['text']!r}")

    print("\n共有文本的 bbox 偏移（crop 像素 / 占 crop 比例）：")
    worst = []
    for r in recs:
        b, n = r["res"]["0"]["bbox"], r["res"][str(WHY)]["bbox"]
        ch, cw = r["crop"]
        for k in set(b) & set(n):
            d = [abs(a - c) for a, c in zip(b[k], n[k])]
            rel = max(d[0] / max(ch, 1), d[2] / max(ch, 1), d[1] / max(cw, 1), d[3] / max(cw, 1))
            worst.append((rel, max(d), r["photo"][:12], r["region"], k, d))
    worst.sort(reverse=True)
    for w in worst[:12]:
        print(f"   {w[2]} r{w[3]} {w[4]!r} Δpx={[round(x) for x in w[5]]} 最大Δ={w[1]:.0f}px "
              f"占crop={w[0]*100:.1f}%")
    if worst:
        rels = [w[0] for w in worst]
        print(f"   → 共有 {len(worst)} 项：中位偏移 {sorted(rels)[len(rels)//2]*100:.2f}%，"
              f"最大 {max(rels)*100:.2f}%")


def run(cases):
    recs = []
    for ci, c in enumerate(cases):
        bgr = read_photo(c["photo"])
        if bgr is None:
            continue
        warped, _ = G.normalize(bgr, do_warp=True)
        for ri, v in enumerate(c["regions"]):
            crop, _ = V._crop_margin(warped, [float(x) for x in v["expectedBbox"]])
            if crop.size == 0:
                continue
            cw = max(canvas_size(crop))
            row = {"photo": os.path.basename(c["photo"]), "region": ri,
                   "text": v.get("text"), "canvas": cw, "res": {}}
            for t in THRESHOLDS:
                V.TEXT_MAX_SIDE = t
                t0 = time.perf_counter()
                try:
                    out = V._verify_text(crop)
                except Exception as e:
                    out = [{"text": "__ERR__" + str(e), "conf": 0, "bbox": None}]
                ms = (time.perf_counter() - t0) * 1000
                row["res"][str(t)] = {
                    "ms": round(ms, 1),
                    "sig": sorted((str(o["text"]), round(float(o["conf"]), 3)) for o in out),
                }
            recs.append(row)
            print(f"[{len(recs):3d}] {row['photo'][:14]} r{ri} canvas={cw:5d} "
                  f"ms0={row['res']['0']['ms']:7.0f} "
                  f"ms1024={row['res']['1024']['ms']:7.0f} "
                  f"ms1280={row['res']['1280']['ms']:7.0f} "
                  f"ms1600={row['res']['1600']['ms']:7.0f} "
                  f"same1024={row['res']['1024']['sig'] == row['res']['0']['sig']} "
                  f"same1280={row['res']['1280']['sig'] == row['res']['0']['sig']} "
                  f"same1600={row['res']['1600']['sig'] == row['res']['0']['sig']}")
            sys.stdout.flush()
    json.dump(recs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n=== 汇总（{len(recs)} region）===")
    base_ms = sum(r["res"]["0"]["ms"] for r in recs)
    print(f"基线(0) 总耗时 {base_ms/1000:.1f}s，均值 {base_ms/max(len(recs),1):.0f}ms/region")
    for t in THRESHOLDS[1:]:
        k = str(t)
        tot = sum(r["res"][k]["ms"] for r in recs)
        diff = [r for r in recs if r["res"][k]["sig"] != r["res"]["0"]["sig"]]
        # 区分「完全丢文本」和「文本有变化」
        lost = [r for r in diff if len(r["res"][k]["sig"]) < len(r["res"]["0"]["sig"])]
        print(f"\n阈值 {t}: 总耗时 {tot/1000:.1f}s（{(1-tot/max(base_ms,1e-9))*100:+.1f}%）"
              f"，均值 {tot/max(len(recs),1):.0f}ms")
        print(f"  文本集变化 {len(diff)}/{len(recs)} region"
              f"（其中检出条数变少 {len(lost)}）")
        for r in diff[:12]:
            b = [x[0] for x in r["res"]["0"]["sig"]]
            n2 = [x[0] for x in r["res"][k]["sig"]]
            print(f"    - {r['photo'][:14]} r{r['region']} 目标={r['text']!r}\n"
                  f"        0    -> {b}\n        {t} -> {n2}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "census"
    cases = load_cases()
    print(f"取到 {len(cases)} 张照片：")
    for c in cases:
        print(f"  p{c['profile']} rec{c['record']} {os.path.basename(c['photo'])[:20]} "
              f"regions={len(c['regions'])} verify_ms={c['verify_ms']}")
    if mode == "census":
        census(cases)
    elif mode == "bbox":
        run_bbox(cases)
    else:
        run(cases)
