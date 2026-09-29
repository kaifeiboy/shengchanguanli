# -*- coding: utf-8 -*-
"""
P20：回答「是不是完全无法自动命中」——显式给定部位后，逐看判定分布。

分两阶段：
  阶段1  不带 view 调 compare，记录是否 requiresViewSelection 及候选
  阶段2  带候选首位 view 重调，统计 verdict state 分布 / needsReview 数

只读，不写业务数据。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTSET = os.path.join(ROOT, "testset.json")
BASE = "http://127.0.0.1:5000/api/drawingsv2/compare"
OUT = os.path.join(ROOT, "_p20_autohit.json")


def call(drawing, photo, view=None, timeout=180):
    body = {"drawing": drawing, "photo": photo.replace("\\", "/"), "observe": True}
    if view:
        body["view"] = view
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), (time.time() - t0) * 1000, None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw), (time.time() - t0) * 1000, e.code
        except Exception:
            return {"error": raw[:200]}, (time.time() - t0) * 1000, e.code


def main():
    cases = json.load(open(TESTSET, encoding="utf-8"))["cases"]
    print(f"用例数 = {len(cases)}")
    rows = []
    limit = int(os.environ.get("P20_LIMIT", "0")) or len(cases)
    for i, c in enumerate(cases[:limit], 1):
        photo = os.path.abspath(os.path.join(
            os.path.dirname(ROOT), "..", "..", "..", "..", "..", c["photoRel"]))
        drawing = c.get("drawingKey") or str(c["profileId"])
        d1, ms1, code = call(drawing, photo)
        if d1.get("error"):
            rows.append({"id": c["id"], "pid": c["profileId"], "stage1": "ERR",
                         "error": str(d1.get("error"))[:80], "httpCode": code})
            print(f"[{i:2d}] {c['id']:16s} ERR {str(d1.get('error'))[:60]}")
            continue
        if d1.get("requiresViewSelection"):
            cands = d1.get("viewCandidates") or []
            cands = sorted(cands, key=lambda x: -(x.get("score") or 0))
            pick = cands[0]["view"] if cands else None
            rows.append({"id": c["id"], "pid": c["profileId"], "stage1": "needsView",
                         "nCands": len(cands),
                         "topScore": (cands[0].get("score") if cands else None), "pick": pick})
            print(f"[{i:2d}] {c['id']:16s} needsView cands={len(cands)} pick={pick}")
            if pick:
                time.sleep(0.2)
                d2, ms2, code2 = call(drawing, photo, pick)
                vs = d2.get("verdicts") or []
                st = Counter(v.get("state") for v in vs)
                nr = sum(1 for v in vs if v.get("needsReview"))
                rows[-1].update({"stage2": "ok", "verdicts": len(vs),
                                 "states": dict(st), "needsReview": nr, "ms2": round(ms2)})
                print(f"        -> view={pick} verdicts={len(vs)} {dict(st)} needsReview={nr}")
            continue
        vs = d1.get("verdicts") or []
        st = Counter(v.get("state") for v in vs)
        rows.append({"id": c["id"], "pid": c["profileId"], "stage1": "auto",
                     "inferred": d1.get("inferredView"), "verdicts": len(vs), "states": dict(st)})
        print(f"[{i:2d}] {c['id']:16s} auto={d1.get('inferredView')} verdicts={len(vs)} {dict(st)}")

    json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n" + "=" * 68)
    print("【汇总】")
    print("  阶段1 分布 =", dict(Counter(r["stage1"] for r in rows)))
    auto = [r for r in rows if r["stage1"] == "auto"]
    s2 = [r for r in rows if r.get("stage2") == "ok"]
    print(f"  自动识别成功 = {len(auto)}  补选后成功 = {len(s2)}")
    tot = Counter()
    nr_all = 0
    n_all = 0
    for r in auto + s2:
        for k, v in (r.get("states") or {}).items():
            tot[k] += v
        n_all += r.get("verdicts", 0)
        nr_all += r.get("needsReview", 0)
    print(f"  判定总数 = {n_all}  状态分布 = {dict(tot)}")
    print(f"  needsReview 数 = {nr_all}")
    if n_all:
        green = tot.get("Matched", 0)
        grey = tot.get("NotComparable", 0) + tot.get("NotApplicable", 0) + tot.get("ProcessingError", 0)
        print(f"  绿(自动命中)率 = {green/n_all*100:.1f}%   灰率 = {grey/n_all*100:.1f}%")
    print(f"  明细 -> {OUT}")


if __name__ == "__main__":
    main()
