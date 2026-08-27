"""21 图跨图批量验证：对 生产打标效果图 全部 *Model.pdf 跑 ONNX 自动切图。
绝不杀进程（与方向 A 纪律一致）；超时仅标记、不 kill。打印汇总表。
"""
import os, sys, json, subprocess, glob, time

DRAW_DIR = "E:/生产打标效果图"
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "segment_blocks.py")
PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
OUT_ROOT = "E:/workaaa/shengchanguanli/data/seg/_batch_onnx"
os.makedirs(OUT_ROOT, exist_ok=True)

pdfs = sorted(glob.glob(os.path.join(DRAW_DIR, "*Model.pdf")))
print(f"TOTAL PDFs: {len(pdfs)}", flush=True)

results = []
t0 = time.time()
for i, pdf in enumerate(pdfs):
    name = os.path.basename(pdf)
    did = str(900000 + i)  # 整数 did（生产 C# 契约要求 int），避免 int(did) 失败
    out = os.path.join(OUT_ROOT, did)
    os.makedirs(out, exist_ok=True)
    try:
        p = subprocess.run([PY, SCRIPT, "segment", pdf, did, out, "200"],
                           capture_output=True, text=True, timeout=180)
        man = None
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    man = json.loads(line); break
                except Exception:
                    pass
        if man:
            results.append((name, man.get("mode"), man.get("algorithm"),
                            len(man.get("blocks", [])), "OK"))
        else:
            results.append((name, "PARSE_FAIL", "", 0,
                            (p.stderr or p.stdout)[:160].replace("\n", " ")))
    except subprocess.TimeoutExpired:
        results.append((name, "TIMEOUT", "", 0, ""))
    except Exception as e:
        results.append((name, "ERR", "", 0, str(e)[:160]))
    print(f"[{i+1}/{len(pdfs)}] {name} -> mode={results[-1][1]} n={results[-1][3]}",
          flush=True)

print("\n================ SUMMARY ================")
auto = [r for r in results if r[1] == "auto-paddlev3"]
fallback = [r for r in results if r[1] != "auto-paddlev3"]
for r in results:
    print(f"  {r[0]:50s} mode={r[1]:16s} algo={r[2]:20s} n={r[3]} {r[4]}")
print(f"\nTOTAL={len(results)}  AUTO={len(auto)}  FALLBACK/FAIL={len(fallback)}  "
      f"time={time.time()-t0:.1f}s")
