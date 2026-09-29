import json, os, time, urllib.request, sqlite3

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", ".."))
ENDPOINT = "http://127.0.0.1:5000"
CASE = "p61-c91b6258"
PROFILE = 61
MARK_KEY = "02-PC-P1HVQ效果图25.6.20-Model#005"
REAL = "QHRW52005127437232"
WRONG = "WRONG_QR_VALUE_XYZ_000"

# 取 mark 数值 id
con = sqlite3.connect(os.path.join(ROOT, "data", "drawingsv2.db"))
con.execute("PRAGMA journal_mode=WAL")
mid = con.execute("SELECT id FROM v2_drawing_marks WHERE profile_id=? AND mark_key=? AND mark_type='Qr'",
                 (PROFILE, MARK_KEY)).fetchone()[0]
con.close()
print("QR mark 数值 id =", mid)

def call_compare():
    ts = json.load(open(os.path.join(HERE, "testset.json"), encoding="utf-8"))
    c = [x for x in ts["cases"] if x["id"] == CASE][0]
    photo = c["photoRel"]
    if not os.path.isabs(photo):
        photo = os.path.join(ROOT, photo)
    url = ENDPOINT + "/api/drawingsv2/compare"
    body = json.dumps({"drawing": str(PROFILE), "photo": os.path.abspath(photo).replace("\\", "/"),
                       "observe": True}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    d = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
    ms = time.time() - t0
    for v in (d.get("verdicts") or []):
        if v.get("markKey") == MARK_KEY:
            return v, round(ms, 1)
    return None, round(ms, 1)

def set_expected(text):
    url = ENDPOINT + "/api/drawingsv2/profiles/%d/marks/%d/expected-text" % (PROFILE, mid)
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())

print("\n=== 步骤1：设预期=真实值 '%s' ===" % REAL)
print("  endpoint:", set_expected(REAL))
v, ms = call_compare()
print("  重跑 %.0fms → 状态=%s | evidence=%s" % (ms, v["state"], v.get("evidence", "")[:120]))

print("\n=== 步骤2：设预期=错值 '%s' ===" % WRONG)
print("  endpoint:", set_expected(WRONG))
v, ms = call_compare()
print("  重跑 %.0fms → 状态=%s | evidence=%s" % (ms, v["state"], v.get("evidence", "")[:120]))

print("\n=== 步骤3：还原预期=null ===")
print("  endpoint:", set_expected(None))
v, ms = call_compare()
print("  重跑 %.0fms → 状态=%s | evidence=%s" % (ms, v["state"], v.get("evidence", "")[:120]))
print("\nDONE")
