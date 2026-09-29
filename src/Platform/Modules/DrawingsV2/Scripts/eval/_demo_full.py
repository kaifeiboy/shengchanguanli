import json, os, time, urllib.request, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))
ENDPOINT = "http://127.0.0.1:5000"

ts = json.load(open(os.path.join(os.path.dirname(__file__), "testset.json"), encoding="utf-8"))
cases = ts["cases"]

def call_compare(profile_id, photo_abs, timeout=180):
    url = ENDPOINT + "/api/drawingsv2/compare"
    body = json.dumps({"drawing": str(profile_id), "photo": photo_abs.replace("\\", "/"),
                       "observe": True}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw), (time.time() - t0) * 1000

out = []
qr_decoded = []  # (caseId, profileId, markKey, markId, observedData)
for i, c in enumerate(cases, 1):
    photo = c["photoRel"]
    if not os.path.isabs(photo):
        photo = os.path.join(ROOT, photo)
    try:
        d, ms = call_compare(c["profileId"], os.path.abspath(photo))
        rec = {"id": c["id"], "profileId": c["profileId"], "ms": round(ms),
               "ok": True, "counts": d.get("counts") or {},
               "verdicts": d.get("verdicts") or []}
        # 收集解出的 QR
        for v in rec["verdicts"]:
            if (v.get("type") == "Qr" or "Qr" in str(v.get("markKey", ""))) and v.get("observedData"):
                qr_decoded.append((c["id"], c["profileId"], v.get("markKey"), v.get("observedData")))
    except Exception as e:
        rec = {"id": c["id"], "profileId": c["profileId"], "ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    out.append(rec)
    print("[%2d/%d] %s ok=%s ms=%.0f qr_decoded_so_far=%d" % (i, len(cases), c["id"], rec.get("ok"), rec.get("ms", 0), len(qr_decoded)), flush=True)

json.dump({"results": out, "qrDecoded": qr_decoded},
          open(os.path.join(os.path.dirname(__file__), "_demo_full.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
print("=== DONE === qr marks decoded (total %d):" % len(qr_decoded))
for q in qr_decoded[:20]:
    print("  ", q[0], "prof", q[1], "mark", q[2], "data=", repr(q[3])[:50])
