# -*- coding: utf-8 -*-
"""JQ(profile 62) 三张真实照片 compare/upload 真实环境测试。
输出: outputs/jq3_p1.json / jq3_p2.json / jq3_p3.json + 控制台摘要
"""
import json, urllib.request, urllib.error, os, ssl, sys

BASE = "https://localhost:5443"
PHOTOS = [
    r"D:\workwechat\WXWork\1688858196673779\Cache\Image\2026-09\0f269c50b03426d89637c5ac9a9b0e65_compress.jpg",
    r"D:\workwechat\WXWork\1688858196673779\Cache\Image\2026-09\bad8f543c6f84b4f541e335036eb0637_compress.jpg",
    r"D:\workwechat\WXWork\1688858196673779\Cache\Image\2026-09\23bbf1c7f7ab36bbad4de175a96c8e6b_compress.jpg",
]
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

profiles = get("/api/drawingsv2/profiles")
arr = profiles if isinstance(profiles, list) else (profiles.get("profiles") or profiles.get("items") or [])
p62 = next((p for p in arr if str(p.get("id")) == "62"), None)
if not p62:
    print("profile 62 未找到; 可用:", [p.get("id") for p in arr]); raise SystemExit(1)
dk = p62.get("drawingKey")
print("JQ drawingKey =", dk)

def field_part(name, val):
    return (f"--{B}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n").encode("utf-8")

def file_part(name, filename, data, ctype="image/jpeg"):
    return ((f"--{B}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode("utf-8") + data + b"\r\n")

for i, ph in enumerate(PHOTOS, 1):
    B = "----wbjq3"
    with open(ph, "rb") as f:
        body = f.read()
    payload = field_part("drawing", dk) + file_part("file", os.path.basename(ph), body) + (f"--{B}--\r\n").encode()
    req = urllib.request.Request(BASE + "/api/drawingsv2/compare/upload", data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={B}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=600) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[photo{i}] HTTP", e.code, e.read().decode("utf-8", "replace")[:400]); continue
    out = os.path.join("outputs", f"jq3_p{i}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(resp, f, ensure_ascii=False, indent=1)
    bm = resp.get("blockMatch")
    print(f"\n===== photo{i}: {os.path.basename(ph)}")
    print("requiresViewSelection =", resp.get("requiresViewSelection"), "| verdicts =", len(resp.get("verdicts", [])))
    print("photoUrl =", resp.get("photoUrl"))
    if not bm or not bm.get("enabled"):
        print("blockMatch enabled =", bm.get("enabled") if bm else None, "| reason =", bm.get("reason") if bm else None)
        continue
    print("degraded =", bm.get("degraded"), bm.get("degradeReason"), "| nBlocks =", bm.get("nBlocks"), "| photoTexts =", len(bm.get("photoTexts") or []))
    print("photoTexts =", bm.get("photoTexts"))
    top = bm.get("top")
    if top:
        print(f"TOP [{top.get('blockIndex')}] {top.get('name')} | G{top.get('nGreen')} R{top.get('nRed')} Y{top.get('nYellow')} Gr{top.get('nGray')} | score={top.get('score')} viewHint={top.get('viewHint')}")
        print("blockNorm =", top.get("blockNorm"), "| pageIndex =", top.get("pageIndex"))
        for e in (top.get("elements") or []):
            pn = e.get("photoNorm")
            print(f"  - {e.get('color'):6s} {e.get('kind'):10s} hit={e.get('hit')} map={e.get('mappingLevel')} text={str(e.get('text'))[:36]!r} photoNorm={'Y' if pn else '-'} conf={e.get('ocrConf')}")
    cands = bm.get("candidates") or []
    for c in cands[:3]:
        print(f"  cand [{c.get('blockIndex')}] {str(c.get('name'))[:30]} G{c.get('nGreen')} R{c.get('nRed')} Y{c.get('nYellow')} score={c.get('score')}")
print("\nDONE")
