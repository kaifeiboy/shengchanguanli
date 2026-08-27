"""批量验证 driver：每张图用独立子进程跑 worker，撞段错误(rc=139)自动重试。
收集 summary.json + 控制台汇总表。Paddle 在本机间歇性原生崩溃，子进程隔离+重试是必需要护栏。
"""
import subprocess, sys, os, glob, json

SRC = r"E:/生产打标效果图"
OUT = r"E:/workaaa/shengchanguanli/data/seg/_batch_verify"
PY = r"C:/Users/Administrator/.workbuddy/binaries/python/envs/paddle/Scripts/python.exe"
WORKER = r"E:/workaaa/shengchanguanli/src/Platform/Modules/Drawings/_verify_worker.py"
MAX_RETRY = 5

def sanitize(n):
    return n.replace(" ", "_").replace("(", "_").replace(")", "_").replace("/", "_")

def main():
    pdfs = sorted(glob.glob(os.path.join(SRC, "*.pdf")))
    summary = []
    print(f"FOUND {len(pdfs)} PDFs  (subprocess-isolated, retry={MAX_RETRY})\n")
    for i, pdf in enumerate(pdfs):
        base = os.path.basename(pdf); sname = sanitize(base)
        sub = os.path.join(OUT, sname)
        rec = {"file": base, "idx": i, "status": None, "n_blocks": None, "blocks": [], "errors": []}
        ok = False
        for attempt in range(MAX_RETRY):
            try:
                # 字节模式读取，避免 worker 的 GBK stderr 触发 UTF-8 解码崩溃导致管道卡死
                r = subprocess.run([PY, WORKER, pdf, sub], capture_output=True, timeout=150)
                if r.returncode == 0:
                    with open(os.path.join(sub, "result.json"), encoding="utf-8") as f:
                        rec.update(json.load(f))
                    rec["status"] = "OK"; ok = True
                    break
                else:
                    tail = (r.stderr or b"").decode("utf-8", "replace")[-240:].replace("\n", " ")
                    rec["errors"].append(f"att{attempt} rc={r.returncode} {tail}")
            except subprocess.TimeoutExpired:
                rec["errors"].append(f"att{attempt} TIMEOUT")
        if not ok:
            rec["status"] = "FAILED_ALL_RETRY"
        summary.append(rec)
        bs = ",".join(f"b{b['blk']}:{b['w']}x{b['h']} AR{b['aspect']}" for b in rec.get("blocks", []))
        print(f"[{i+1:02d}/{len(pdfs)}] {base[:40]:40} {rec['status']:14} n={str(rec.get('n_blocks','-')):>2} {bs}")
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    ok_n = sum(1 for r in summary if r["status"] == "OK")
    print(f"\nDONE: {ok_n}/{len(pdfs)} OK  summary.json written")

if __name__ == "__main__":
    main()
