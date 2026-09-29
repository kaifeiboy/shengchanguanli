"""B｜6.5s 固定开销拆解：独立计时 observe(全图盲检) 与 verify(定点) 两次 python 调用。

目的：把每请求固定的 ~6.5s 开销拆成
  - spawn 成本（python 进程 + import，被 ①/③ 攻击）
  - observe 全图 OCR 成本（被 ②/④ 攻击）
  - C# 编排/HTTP/JSON 成本（两者都不直接攻击）

方法：对同一张真实照片，分别子进程调用 observe_run.py 的 observe / verify 子命令，
读脚本内部计时器 observe_ms / verify_ms（不含 spawn），并用墙钟时间反推 spawn。

输出：data/_exp/_decompose.json + 控制台拆解表。
"""
import os, sys, json, sqlite3, subprocess, statistics, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, ".."))          # .../DrawingsV2/Scripts
OBSERVE = os.path.join(SCRIPTS, "observe")
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
DB = os.path.abspath(os.path.join(SCRIPTS, "..", "..", "..", "..", "..", "data", "drawingsv2.db"))
# DB 相对 Scripts 上溯：Scripts -> DrawingsV2 -> Modules -> Platform -> src -> repo -> data
# 实际：repo/data；Scripts 在 repo/src/Platform/Modules/DrawingsV2/Scripts
# 上溯 6 层到 repo
DB = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", "..", "data", "drawingsv2.db"))

MAX_PHOTOS = int(os.environ.get("DEC_PHOTOS", "4"))
MEASURE_RUNS = 3          # 每次命令计时次数（取中位，已含预热收敛）
WARMUP = 1

KIND_MAP = {"qr": "qr", "qrcode": "qr", "text": "text", "group": "any",
            "icon": "icon", "any": "any", "": "any"}


def load_cases():
    con = sqlite3.connect(DB); con.execute("PRAGMA journal_mode=WAL")
    rows = con.cursor().execute(
        "SELECT id, profile_id, photo_path, verdicts_json FROM v2_compare_records "
        "WHERE usable=1 ORDER BY id DESC LIMIT 400").fetchall()
    con.close()
    out, seen = [], set()
    for rid, pid, path, vj in rows:
        if not path or not os.path.exists(path):
            continue
        try: vs = json.loads(vj or "[]")
        except Exception: continue
        regs = []
        for i, v in enumerate(vs):
            eb = v.get("expectedBbox")
            if not eb or len(eb) != 4:
                continue
            t = str(v.get("type", "")).lower()
            kind = KIND_MAP.get(t, "any")
            regs.append({"id": v.get("markKey") or str(i),
                         "kind": kind, "norm_bbox": [float(x) for x in eb]})
        if not regs:
            continue
        key = os.path.basename(path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"photo": path, "profile": pid, "record": rid, "regions": regs})
        if len(out) >= MAX_PHOTOS:
            break
    return out


def run_cmd(args):
    t0 = time.perf_counter()
    p = subprocess.run([PY, "observe_run.py"] + args,
                       cwd=SCRIPTS, capture_output=True, text=True, timeout=600)
    wall = (time.perf_counter() - t0) * 1000
    if p.returncode != 0:
        return None, wall, p.stderr[-300:]
    try:
        doc = json.loads(p.stdout)
    except Exception:
        return None, wall, "json-parse-fail: " + p.stdout[:200]
    return doc, wall, ""


def inner_ms(doc, key):
    if not doc:
        return None
    diag = doc.get("diagnostics") or {}
    return diag.get(key)


def main():
    cases = load_cases()
    print(f"选中 {len(cases)} 张照片做拆解测量")
    results = []
    for c in cases:
        tmp = os.path.join(tempfile.gettempdir(), f"_dec_reg_{c['record']}.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c["regions"], f)
        row = {"record": c["record"], "photo": os.path.basename(c["photo"]),
               "n_regions": len(c["regions"]),
               "observe_wall": [], "observe_ms": [],
               "verify_wall": [], "verify_ms": []}
        # warmup（模型/导入缓存）
        for _ in range(WARMUP):
            run_cmd(["observe", c["photo"], "--json-only"])
            run_cmd(["verify", c["photo"], "--regions", tmp, "--json-only"])
        # 测量
        for _ in range(MEASURE_RUNS):
            d, w, e = run_cmd(["observe", c["photo"], "--json-only"])
            row["observe_wall"].append(w); row["observe_ms"].append(inner_ms(d, "observe_ms") or 0)
            d, w, e = run_cmd(["verify", c["photo"], "--regions", tmp, "--json-only"])
            row["verify_wall"].append(w); row["verify_ms"].append(inner_ms(d, "verify_ms") or 0)
        results.append(row)
        print(f"  #{c['record']} {os.path.basename(c['photo'])} regions={len(c['regions'])} "
              f"observe_wall={int(statistics.median(row['observe_wall']))} "
              f"observe_ms={int(statistics.median(row['observe_ms']))} "
              f"verify_wall={int(statistics.median(row['verify_wall']))} "
              f"verify_ms={int(statistics.median(row['verify_ms']))}")

    # 聚合
    def med(xs): return statistics.median(xs) if xs else 0
    obs_w = med([med(r["observe_wall"]) for r in results])
    obs_m = med([med(r["observe_ms"]) for r in results])
    ver_w = med([med(r["verify_wall"]) for r in results])
    ver_m = med([med(r["verify_ms"]) for r in results])
    spawn = obs_w - obs_m                      # 单次 spawn+import 概算
    blind_extra = max(0.0, obs_m - ver_m)     # observe 比 verify 多做的全图 OCR/detect
    csharp_overhead = max(0.0, 6500 - (2 * spawn + obs_m + ver_m))  # 端点固定 6.5s 中 C# 编排剩余

    summary = {
        "per_case": results,
        "aggregate": {
            "observe_wall_ms": round(obs_w, 1), "observe_ms": round(obs_m, 1),
            "verify_wall_ms": round(ver_w, 1),  "verify_ms": round(ver_m, 1),
            "spawn_per_call_ms": round(spawn, 1),
            "blind_ocr_extra_ms": round(blind_extra, 1),
            "endpoint_fixed_overhead_ms": 6500,
            "csharp_overhead_est_ms": round(csharp_overhead, 1),
        },
        "implied_benefit": {
            "C_merge_observe_verify_saves": round(spawn, 1),       # 省 1 次 spawn
            "C3_process_pool_saves": round(2 * spawn, 1),          # 省 2 次 spawn
            "C2_skip_blind_ocr_when_view_known_saves_max": round(blind_extra, 1),
        },
    }
    outp = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", "data", "_exp", "_decompose.json"))
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== 拆解结论（中位，4 张照片） ===")
    print(f"observe 墙钟={obs_w:.0f}ms  内部 observe_ms={obs_m:.0f}ms  → spawn≈{spawn:.0f}ms/次")
    print(f"verify  墙钟={ver_w:.0f}ms  内部 verify_ms={ver_m:.0f}ms")
    print(f"盲检比定点多做的 OCR/detect ≈ {blind_extra:.0f}ms")
    print(f"C# 编排残留（端点固定6.5s 扣除 2*spawn+observe+verify）≈ {csharp_overhead:.0f}ms")
    print(f"\n性能轨收益估算：")
    print(f"  ① observe+verify 合并 → 省 ≈{spawn:.0f}ms/请求（v2 内，低风险）")
    print(f"  ③ 常驻进程池       → 省 ≈{2*spawn:.0f}ms/请求（普适，但改共用 PythonProcessFactory，须批范围）")
    print(f"  ② 已知 view 跳过盲检 → 最多省 ≈{blind_extra:.0f}ms/请求（仅 view 已知用例有效，有准确度风险）")
    print(f"已写 {outp}")


if __name__ == "__main__":
    import time
    main()
