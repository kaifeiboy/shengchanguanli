"""B 细化：对指定 record 跑一次 verify，按 region 打印 kind/ms/检出数，确认 verify 成本来源。"""
import os, sys, json, sqlite3, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, ".."))
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
DB = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", "..", "data", "drawingsv2.db"))

KIND_MAP = {"qr": "qr", "qrcode": "qr", "text": "text", "group": "any",
            "icon": "icon", "any": "any", "": "any"}


def main():
    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 247
    con = sqlite3.connect(DB); con.execute("PRAGMA journal_mode=WAL")
    row = con.cursor().execute(
        "SELECT photo_path, verdicts_json FROM v2_compare_records WHERE id=?", (rid,)).fetchone()
    con.close()
    if not row:
        print("no record", rid); return
    path, vj = row
    vs = json.loads(vj or "[]")
    regs = [{"id": v.get("markKey") or str(i), "kind": KIND_MAP.get(str(v.get("type", "")).lower(), "any"),
             "norm_bbox": [float(x) for x in v["expectedBbox"]]}
            for i, v in enumerate(vs) if v.get("expectedBbox") and len(v["expectedBbox"]) == 4]
    tmp = os.path.join(tempfile.gettempdir(), f"_vb_reg_{rid}.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(regs, f)
    p = subprocess.run([PY, "observe_run.py", "verify", path, "--regions", tmp, "--json-only"],
                       cwd=SCRIPTS, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        print("ERR", p.stderr[-400:]); return
    doc = json.loads(p.stdout)
    print(f"#{rid} regions={len(regs)} verify_ms={doc['diagnostics'].get('verify_ms')}")
    for r in doc.get("regions", []):
        texts = r.get("texts") or []
        qr = r.get("qr") or {}
        print(f"  {r['id']:>14} kind={r.get('kind'):<5} ms={r.get('ms'):>7.1f} "
              f"texts={len(texts)} qr_found={qr.get('found')} "
              f"blank={r.get('blank_skipped', False)} "
              f"content_ink={r.get('content', {}).get('ink')}")


if __name__ == "__main__":
    main()
