#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2 验收量化基线脚本（计划项 F 测试集 / H 量化基线）

做什么
------
把「打标首件比对」的验收门槛变成可重复执行、可比较的数字，而不是凭感觉说"够好用"。

指标分两类
----------
A 类｜零人工真值，每次跑都能自动出数（过程健康度）：
  photoUsableRate    照片合格率       = 通过质量门控的用例 / 全部用例
  viewInferRate      视图自动识别率   = 自动推断出视图的可用用例 / 可用用例
  anchorApplyRate    锚点配准生效率   = 锚点配准生效的可用用例 / 可用用例
  decisiveRate       高置信判定占比   = (Matched+Missing) / 参与判定的项（排除 NotApplicable/NotComparable）
  notDetectedRate    未检出占比       = NotDetected / 参与判定的项
  latencyP50/P95     端到端耗时分位（ms）

B 类｜需要人工真值（验收门槛本体，未标注时输出 n/a）：
  viewAccuracy       视图识别正确率   = inferredView == truth.view / 有真值用例
  recall             OCR 召回         = 真值 present 且系统"看到"了(Matched/LowConfidence) / 真值 present 总数
  falseRedRate       假红率           = 系统 Missing 但真值 present / 系统 Missing 总数
  falseGreenRate     假绿率           = 系统 Matched 但真值 missing / 系统 Matched 总数

用法
----
  python run_baseline.py --init                 # 从 DB 生成/刷新 testset.json（保留已有 truth）
  python run_baseline.py                        # 跑全量评估
  python run_baseline.py --limit 5              # 只跑前 5 个（冒烟）
  python run_baseline.py --export-truth         # 导出真值标注模板 CSV
  python run_baseline.py --import-truth x.csv   # 回填已标注真值到 testset.json

真值来源（双通道）：
  1) testset.json 里每题的 truth 字段（--export-truth / --import-truth 流程）
  2) v2_verdict_reviews 表（H5 上人工确认过的判定，取最新一次）—— 与 ② 联动

约束：只读 DB（真值/用例清单）与调用 v2 HTTP 端点，不写业务表。
"""
import argparse
import csv
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TESTSET = os.path.join(HERE, "testset.json")
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", ".."))   # 仓库根
DEFAULT_ENDPOINT = "http://127.0.0.1:5000"
DEFAULT_DB = os.path.join(ROOT, "data", "drawingsv2.db")
DEFAULT_PHOTO_DIR = os.path.join(ROOT, "data", "drawingsv2_photos")

# 参与"判定"统计的状态（NotApplicable 不适用 / NotComparable 不可比 不算判定）
DECISIVE_STATES = ("Matched", "Missing")
COUNTED_STATES = ("Matched", "Missing", "LowConfidence", "NotDetected", "Wrong", "Extra")
# 系统"看到了内容"（用于召回）
SEEN_STATES = ("Matched", "LowConfidence")


# ---------------- 用例集 ----------------

def build_testset(db_path, photo_dir):
    """从 DB 的比对记录里抽出全部 (profile, photo) 去重对作为用例；保留已有 truth 标注。"""
    old = {}
    if os.path.exists(TESTSET):
        try:
            with open(TESTSET, "r", encoding="utf-8") as f:
                for c in json.load(f).get("cases", []):
                    old[c["id"]] = c
        except Exception:
            pass

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()
    cur.execute("""
        SELECT r.profile_id, p.drawing_key, r.photo_sha256, r.photo_name
        FROM v2_compare_records r
        LEFT JOIN v2_drawing_profiles p ON p.id = r.profile_id
        GROUP BY r.profile_id, r.photo_sha256
        ORDER BY r.profile_id, MIN(r.id)
    """)
    cases = []
    for pid, dkey, sha, name in cur.fetchall():
        path = find_photo(photo_dir, sha)
        if not path:
            continue
        cid = "p%s-%s" % (pid, sha[:8])
        c = {
            "id": cid,
            "profileId": pid,
            "drawingKey": dkey or "",
            "photoSha256": sha,
            "photoName": name or "",
            "photoRel": os.path.relpath(path, ROOT).replace("\\", "/"),
            "truth": (old.get(cid) or {}).get("truth"),   # 保留人工真值
        }
        cases.append(c)
    con.close()
    return {
        "version": "1.0",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "从 v2_compare_records 去重的 (profile, photo) 对自动生成",
        "metricsNote": "A 类零真值可算；B 类（viewAccuracy/recall/falseRedRate/falseGreenRate）需标注 truth",
        "cases": cases,
    }


def find_photo(photo_dir, sha):
    if not sha:
        return None
    for sub in sorted(os.listdir(photo_dir)):
        d = os.path.join(photo_dir, sub)
        if not os.path.isdir(d):
            continue
        for ext in (".jpg", ".jpeg", ".png"):
            p = os.path.join(d, sha[:2] + "_" + sha + ext)
            if os.path.exists(p):
                return p
    return None


# ---------------- 执行 ----------------

def call_compare(endpoint, profile_id, photo_abs, timeout=180):
    url = endpoint.rstrip("/") + "/api/drawingsv2/compare"
    body = json.dumps({"drawing": str(profile_id), "photo": photo_abs.replace("\\", "/"),
                       "observe": True}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw), (time.time() - t0) * 1000


def run(endpoint, cases, photo_root, limit=None, verbose=True):
    results = []
    n = len(cases) if not limit else min(limit, len(cases))
    for i, c in enumerate(cases[:n], 1):
        photo = os.path.join(photo_root, c["photoRel"]) if not os.path.isabs(c["photoRel"]) else c["photoRel"]
        rec = {"id": c["id"], "profileId": c["profileId"], "photoName": c.get("photoName", "")}
        try:
            d, ms = call_compare(endpoint, c["profileId"], os.path.abspath(photo))
            rec.update({
                "ok": True, "ms": round(ms), "usable": d.get("photoUsable"),
                "inferredView": d.get("inferredView"), "selectedView": d.get("selectedView"),
                "anchorApplied": bool((d.get("anchor") or {}).get("applied")),
                "counts": d.get("counts") or {},
                "verdicts": [{"markKey": v.get("markKey"), "state": v.get("state"),
                              "view": v.get("view"), "text": v.get("text")}
                             for v in (d.get("verdicts") or [])],
                "recordId": d.get("recordId"),
            })
        except Exception as e:
            rec.update({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        results.append(rec)
        if verbose:
            flag = "OK " if rec.get("ok") else "ERR"
            print("  [%2d/%d] %s %-18s usable=%-5s view=%-10s %s" % (
                i, n, flag, c["id"], rec.get("usable"), rec.get("inferredView") or "-",
                rec.get("counts") or rec.get("error", "")), flush=True)
    return results


# ---------------- 指标 ----------------

def load_human_truth(db_path):
    """读人工确认结果，按 profile + photoSha + markKey 关联到回归用例。"""
    out = {}
    if not os.path.exists(db_path):
        return out
    try:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA journal_mode=WAL")
        cur = con.cursor()
        cur.execute("""SELECT r.profile_id, r.photo_sha256, v.mark_key, v.human_state
                       FROM v2_verdict_reviews v
                       JOIN v2_compare_records r ON r.id = v.record_id
                       ORDER BY v.id""")
        for pid, sha, mk, hs in cur.fetchall():
            out.setdefault((pid, sha, mk), []).append(hs)
        con.close()
    except Exception:
        pass
    return {k: v[-1] for k, v in out.items()}


def calc(results, cases, human_truth):
    m = {}
    ok = [r for r in results if r.get("ok")]
    m["casesTotal"] = len(results)
    m["casesOk"] = len(ok)
    usable = [r for r in ok if r.get("usable") is True]
    m["photoUsableRate"] = pct(len(usable), len(ok)) if ok else None
    m["viewInferRate"] = pct(sum(1 for r in usable if r.get("inferredView")), len(usable)) if usable else None
    m["anchorApplyRate"] = pct(sum(1 for r in usable if r.get("anchorApplied")), len(usable)) if usable else None

    # 判定分布 / 高置信
    pool = []
    for r in ok:
        for v in r.get("verdicts", []):
            if v.get("state") in COUNTED_STATES:
                pool.append(v)
    m["judgedTotal"] = len(pool)
    m["decisiveRate"] = pct(sum(1 for v in pool if v["state"] in DECISIVE_STATES), len(pool)) if pool else None
    m["notDetectedRate"] = pct(sum(1 for v in pool if v["state"] == "NotDetected"), len(pool)) if pool else None
    dist = {}
    for r in ok:
        for k, n in (r.get("counts") or {}).items():
            if isinstance(n, int):
                dist[k] = dist.get(k, 0) + n
    m["verdictDistribution"] = dict(sorted(dist.items(), key=lambda x: -x[1]))

    lat = [r["ms"] for r in ok if r.get("ms")]
    if lat:
        lat.sort()
        m["latencyP50Ms"] = round(statistics.median(lat))
        m["latencyP95Ms"] = round(lat[max(0, int(len(lat) * 0.95) - 1)])

    # ---- B 类：需真值 ----
    truth_map = {c["id"]: (c.get("truth") or {}) for c in cases}
    case_map = {c["id"]: c for c in cases}
    view_t = view_h = 0
    tp_seen = truth_present = 0
    red_labeled = red_false = 0
    green_labeled = green_false = 0
    for r in ok:
        t = truth_map.get(r["id"]) or {}
        if t.get("view"):
            view_t += 1
            if r.get("inferredView") == t["view"]:
                view_h += 1
        marks_truth = t.get("marks") or {}
        for v in r.get("verdicts", []):
            mk, st = v.get("markKey"), v.get("state")
            # 真值优先取用例标注，其次取人工确认表
            truth = marks_truth.get(mk)
            if truth is None:
                case = case_map.get(r["id"]) or {}
                hs = human_truth.get((r.get("profileId"), case.get("photoSha256"), mk))
                truth = {"HumanPresent": "present", "HumanMissing": "missing",
                         "HumanWrongPart": "offscreen"}.get(hs)
            if truth == "present":
                truth_present += 1
                if st in SEEN_STATES:
                    tp_seen += 1
            if st == "Missing" and truth in ("present", "missing"):
                red_labeled += 1
                if truth == "present":
                    red_false += 1
            if st == "Matched" and truth in ("present", "missing"):
                green_labeled += 1
                if truth == "missing":
                    green_false += 1
    m["truthCoverage"] = {
        "viewLabeled": view_t,
        "marksLabeled": sum(1 for c in cases for _ in ((c.get("truth") or {}).get("marks") or {})),
        "humanReviews": len(human_truth),
    }
    m["viewAccuracy"] = pct(view_h, view_t) if view_t else None
    m["recall"] = pct(tp_seen, truth_present) if truth_present else None
    m["falseRedRate"] = pct(red_false, red_labeled) if red_labeled else None
    m["falseGreenRate"] = pct(green_false, green_labeled) if green_labeled else None
    m["acceptanceReady"] = bool(view_t and truth_present and red_labeled and green_labeled)
    return m


def pct(a, b):
    return None if not b else round(a * 100.0 / b, 1)


# ---------------- 真值标注 ----------------

def export_truth(cases, results, out_csv):
    rows = []
    res = {r["id"]: r for r in results}
    for c in cases:
        r = res.get(c["id"], {})
        for v in r.get("verdicts", []):
            rows.append({
                "caseId": c["id"], "profileId": c["profileId"], "photoName": c.get("photoName", ""),
                "markKey": v.get("markKey"), "text": v.get("text") or "", "view": v.get("view") or "",
                "systemState": v.get("state") or "",
                "truth": ((c.get("truth") or {}).get("marks") or {}).get(v.get("markKey"), ""),
            })
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["caseId", "profileId", "photoName", "markKey", "text", "view", "systemState", "truth"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def import_truth(testset, csv_path):
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    by_case = {}
    for r in rows:
        t = (r.get("truth") or "").strip().lower()
        if not t:
            continue
        by_case.setdefault(r["caseId"], {})[r["markKey"]] = t
    n = 0
    for c in testset["cases"]:
        if c["id"] in by_case:
            c.setdefault("truth", {}).setdefault("marks", {}).update(by_case[c["id"]])
            n += 1
    return n


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="从 DB 生成/刷新 testset.json")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--photo-root", default=ROOT)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default=os.path.join(HERE, "baseline_report.json"))
    ap.add_argument("--export-truth", metavar="CSV")
    ap.add_argument("--import-truth", metavar="CSV")
    ap.add_argument("--require-truth", action="store_true",
                    help="无足够人工真值时以非 0 退出，用于 CI/验收闸门")
    a = ap.parse_args()

    if a.init:
        ts = build_testset(a.db, DEFAULT_PHOTO_DIR)
        with open(TESTSET, "w", encoding="utf-8") as f:
            json.dump(ts, f, ensure_ascii=False, indent=2)
        print("testset.json 已生成：%d 个用例" % len(ts["cases"]))
        return 0

    if not os.path.exists(TESTSET):
        print("缺少 testset.json，请先执行 --init", file=sys.stderr)
        return 2

    with open(TESTSET, "r", encoding="utf-8") as f:
        ts = json.load(f)
    cases = ts["cases"]

    if a.import_truth:
        n = import_truth(ts, a.import_truth)
        with open(TESTSET, "w", encoding="utf-8") as f:
            json.dump(ts, f, ensure_ascii=False, indent=2)
        print("已回填 %d 个用例的真值" % n)
        return 0

    print("== 执行 %d 个用例（endpoint=%s）==" % (len(cases) if not a.limit else a.limit, a.endpoint))
    results = run(a.endpoint, cases, a.photo_root, a.limit)
    human = load_human_truth(a.db)
    metrics = calc(results, cases, human)

    if a.export_truth:
        n = export_truth(cases, results, a.export_truth)
        print("真值模板已导出：%s（%d 行）" % (a.export_truth, n))

    report = {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "endpoint": a.endpoint,
              "metrics": metrics, "results": results}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n== 基线指标 ==")
    for k, v in metrics.items():
        if isinstance(v, dict):
            print("  %-20s %s" % (k, v))
        else:
            print("  %-20s %s" % (k, "n/a（待标注）" if v is None else v))
    if not metrics["acceptanceReady"]:
        print("\n警告：人工真值不足，本报告只能表示流程可运行，不能证明准确率。", file=sys.stderr)
    print("\n报告：%s" % a.out)
    return 3 if a.require_truth and not metrics["acceptanceReady"] else 0


if __name__ == "__main__":
    sys.exit(main())
