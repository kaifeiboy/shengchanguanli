import urllib.request, json

BASE = "http://127.0.0.1:5000/api/drawingsv2"
ph = r"E:\workaaa\shengchanguanli\data\drawingsv2_photos\202609\ab_ab54f2916c6948beb2b25f7306bf62bd21323ef211d2da2df6ae38395727c23c.jpg"

def post(path, body):
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=240))

r0 = post("/compare", {"drawing": "61", "photo": ph, "view": None, "withObserve": True})
cands = r0.get("viewCandidates", [])
view = cands[0].get("view")
r = post("/compare", {"drawing": "61", "photo": ph, "view": view, "withObserve": True})

bm = r.get("blockMatch")
print("has blockMatch:", bm is not None)
if bm:
    print("enabled:", bm.get("enabled"), "degraded:", bm.get("degraded"),
          "nPhotoCodes:", bm.get("nPhotoCodes"))
    top = bm.get("top")
    if top:
        print("TOP:", top.get("name"), "G", top.get("nGreen"), "R", top.get("nRed"),
              "Y", top.get("nYellow"), "Gr", top.get("nGray"))
        for el in top.get("elements", []):
            pn = el.get("photoNorm")
            print(f"   {el.get('color'):6} {el.get('kind'):6} {str(el.get('text')):26} "
                  f"L={el.get('mappingLevel')} photoNorm={None if pn is None else [round(x,3) for x in pn]}")
    for ex in bm.get("extra", []):
        pn = ex.get("photoNorm")
        print(f"   EXTRA {ex.get('color'):6} {ex.get('kind'):12} {str(ex.get('text')):26} "
              f"L={ex.get('mappingLevel')} photoNorm={None if pn is None else [round(x,3) for x in pn]}")
print("verdicts unchanged count:", len(r.get("verdicts", [])))
print("verdicts sample:", [v.get("state") for v in r.get("verdicts", [])[:6]])
