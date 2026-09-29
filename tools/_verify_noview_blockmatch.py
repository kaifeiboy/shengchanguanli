import json, urllib.request, urllib.error, os, ssl

BASE = "https://localhost:5443"
PHOTO = r"E:\workaaa\shengchanguanli\data\drawingsv2_photos\202609\ab_ab54f2916c6948beb2b25f7306bf62bd21323ef211d2da2df6ae38395727c23c.jpg"
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

# 1) 取 profile 61 的 drawingKey
profiles = get("/api/drawingsv2/profiles")
arr = profiles if isinstance(profiles, list) else (profiles.get("profiles") or profiles.get("items") or [])
p61 = next((p for p in arr if str(p.get("id")) == "61"), None)
if not p61:
    print("profile 61 未找到；可用 id:", [p.get("id") for p in arr][:10]); raise SystemExit(1)
dk = p61.get("drawingKey")
print("profile 61 drawingKey =", dk)

# 2) 不传 view，POST compare/upload
boundary = "----wbtestboundary"
with open(PHOTO, "rb") as f:
    body = f.read()
def field_part(name, val):
    return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n").encode("utf-8")
def file_part(name, filename, data, ctype="image/jpeg"):
    return ((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode("utf-8") + data + b"\r\n")
payload = field_part("drawing", dk) + file_part("file", os.path.basename(PHOTO), body) + (f"--{boundary}--\r\n").encode()
req = urllib.request.Request(BASE + "/api/drawingsv2/compare/upload", data=payload,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
try:
    with urllib.request.urlopen(req, context=ctx, timeout=180) as r:
        resp = json.loads(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode("utf-8", "replace")[:500]); raise SystemExit(1)

bm = resp.get("blockMatch")
print("requiresViewSelection =", resp.get("requiresViewSelection"))
print("has blockMatch =", bm is not None)
print("drawingPdfPath =", resp.get("drawingPdfPath"))
print("drawingPageIndex =", resp.get("drawingPageIndex"))
print("photoUrl =", resp.get("photoUrl"))
print("photo =", resp.get("photo"))
import json as _j
if bm is not None:
    print("RAW blockMatch keys =", list(bm.keys()))
    print("RAW blockMatch (truncated) =", _j.dumps(bm, ensure_ascii=False)[:1400])
if not bm:
    print("!! 未返回 blockMatch —— 后端解耦未生效或照片无命中")
    print("verdicts 数量:", len(resp.get("verdicts", [])))
else:
    print("enabled =", bm.get("enabled"), "| degraded =", bm.get("degraded"))
    top = bm.get("top")
    print("TOP =", (top.get("name") if top else None),
          "| G", top.get("nGreen") if top else "-",
          "R", top.get("nRed") if top else "-",
          "Y", top.get("nYellow") if top else "-",
          "Gr", top.get("nGray") if top else "-")
    print("TOP blockNorm(page) =", (top.get("blockNorm") if top else None),
          "| pageIndex =", (top.get("pageIndex") if top else None))
    pn = [e for e in (top.get("elements") or []) if e.get("photoNorm")]
    print(f"L2 可画框元素 {len(pn)} / {len(top.get('elements') or [])}")
    print("verdicts 零回归数量:", len(resp.get("verdicts", [])))
print("\nCONCLUSION:", "OK 型号驱动·不依赖部位" if bm and bm.get("enabled") else "FAIL")
