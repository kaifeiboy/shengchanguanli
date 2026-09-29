# -*- coding: utf-8 -*-
"""抽样探针：判断「42 例回归在当前代码下是否还能产出判定」。

背景：历史 P6 基线 viewInferRate=100%、judgedTotal=173；
而 2026-09-22 抽查 p61 时返回 requiresViewSelection + NeedsReview + 0 条判定。
若不先探明就跑全量 42 例（≈25 分钟），很可能得到一份全空判定的无效基线。

本脚本对每个抽样用例做两问：
  Q1 自动视图识别能否成立（是否 needsView）？
  Q2 显式传入 Top-1 view 后能否产出判定、判定分布如何？
"""
from __future__ import annotations

import collections
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(r"E:/workaaa/shengchanguanli")
TESTSET = os.path.join(HERE, "testset.json")
ENDPOINT = "http://127.0.0.1:5000"

sys.stdout.reconfigure(encoding="utf-8")


def call(pid, photo_abs, view=None, timeout=240):
    body = {"drawing": str(pid), "photo": photo_abs.replace("\\", "/"), "observe": True}
    if view:
        body["view"] = view
    req = urllib.request.Request(
        ENDPOINT + "/api/drawingsv2/compare",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    return json.loads(raw), (time.time() - t0) * 1000


def main():
    cases = json.load(open(TESTSET, encoding="utf-8"))["cases"]
    # 每个 profile 取前 2 个（尽量跨档案）
    picked, seen = [], collections.Counter()
    for c in cases:
        if seen[c["profileId"]] < 2:
            picked.append(c)
            seen[c["profileId"]] += 1
    print(f"抽样 {len(picked)} 例（覆盖 profile {sorted(seen)}）\n")

    for c in picked:
        photo = os.path.join(ROOT, c["photoRel"])
        if not os.path.exists(photo):
            print(f"{c['id']} 照片缺失")
            continue
        try:
            d, ms = call(c["profileId"], photo)
        except Exception as e:
            print(f"{c['id']:16s} p{c['profileId']}  ERR-自动: {str(e)[:220]}")
            d, ms = None, 0.0
            try:
                d, ms = call(c["profileId"], photo, view="TopCover")
                print(f"{'':16s}   └─ 显式 TopCover 仍可用，判定={len(d.get('verdicts') or [])}")
            except Exception as e2:
                print(f"{'':16s}   └─ 显式也 ERR: {str(e2)[:200]}")
            continue
        needs = d.get("requiresViewSelection")
        cands = d.get("viewCandidates") or []
        inf = d.get("inferredView")
        nv = len(d.get("verdicts") or [])
        print(f"{c['id']:16s} p{c['profileId']}  needsView={str(needs):5s} "
              f"inferred={inf or '-':10s} 判定={nv:3d}  {ms/1000:.1f}s  "
              f"cands={[(x['view'], round(x['score'],1)) for x in cands][:3]}")

        if needs and cands:
            top = cands[0]["view"]
            d2, ms2 = call(c["profileId"], photo, view=top)
            states = collections.Counter(v.get("state") for v in (d2.get("verdicts") or []))
            print(f"{'':16s}   └─ 显式 view={top:10s} 判定={len(d2.get('verdicts') or []):3d} "
                  f"{ms2/1000:.1f}s  分布={dict(states)}  usable={d2.get('photoUsable')} "
                  f"anchor={bool((d2.get('anchor') or {}).get('applied'))}")


if __name__ == "__main__":
    main()
