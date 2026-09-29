import urllib.request, json, ssl

BASE = "https://localhost:5443/api/drawingsv2"
ph = r"E:\workaaa\shengchanguanli\data\drawingsv2_photos\202609\ab_ab54f2916c6948beb2b25f7306bf62bd21323ef211d2da2df6ae38395727c23c.jpg"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

def post(path, body):
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=240, context=CTX))

r0 = post("/compare", {"drawing": "61", "photo": ph, "view": None, "withObserve": True})
cands = r0.get("viewCandidates", [])
view = cands[0].get("view") if cands else None
r = post("/compare", {"drawing": "61", "photo": ph, "view": view, "withObserve": True})

bm = r.get("blockMatch")
print("has blockMatch:", bm is not None)
if not bm:
    print("NO blockMatch -> H5 回退到旧 verdicts 画法（无回归，但新四色不可见）")
    raise SystemExit
# 注意：匿名 DTO 显式字段是小写(top/extra/candidates/photoTexts)，嵌套类型字段是 PascalCase
print("Enabled:", bm.get("Enabled"), "| Degraded:", bm.get("Degraded"),
      "| nPhotoCodes:", bm.get("nPhotoCodes"), "| DegradeReason:", bm.get("DegradeReason"))
top = bm.get("top")
assert top, "top 为 null -> 新引擎未产出结论（H5 将回退旧画法）"
print("TOP block:", top.get("Name"), "| G", top.get("NGreen"), "R", top.get("NRed"),
      "Y", top.get("NYellow"), "Gr", top.get("NGray"), "| Score", top.get("Score"))
nL2 = 0
for el in top.get("Elements", []):
    pn = el.get("PhotoNorm")
    if pn: nL2 += 1
    print(f"   {el.get('Color'):6} {str(el.get('Kind')):6} {str(el.get('Text')):26} "
          f"L={el.get('MappingLevel')} photoNorm={'DRAW' if pn else 'NONE(list)'}")
print("extra 项:", len(bm.get("extra", [])),
      "->", [(e.get('Color'), e.get('Kind'), str(e.get('Text'))[:14]) for e in bm.get("extra", [])])
print("候选块数:", len(bm.get("candidates", [])))
print(f"==> L2 可画框元素 {nL2} / {len(top.get('Elements', []))}；H5 画框与卡片结论可达")
# 既有 verdicts 零回归
vs = r.get("verdicts", [])
print("verdicts 数量(不变):", len(vs), "| 状态:", [v.get("state") for v in vs][:6])
