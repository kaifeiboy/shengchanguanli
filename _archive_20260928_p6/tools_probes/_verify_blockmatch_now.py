import urllib.request, json

BASE = "http://127.0.0.1:5000/api/drawingsv2"
photos = {
    "61_ab": ("61", r"E:\workaaa\shengchanguanli\data\drawingsv2_photos\202609\ab_ab54f2916c6948beb2b25f7306bf62bd21323ef211d2da2df6ae38395727c23c.jpg"),
    "61_64": ("61", r"E:\workaaa\shengchanguanli\data\drawingsv2_photos\202609\64_64d971618edb64178292a85ccc6001529c3842e34b70e9655ef9a07369c4be9d.jpg"),
}

def fmt(pn):
    if pn is None:
        return None
    return [round(x, 3) for x in pn]

for name, (pid, ph) in photos.items():
    req = urllib.request.Request(
        f"{BASE}/profiles/{pid}/blocks/match",
        data=json.dumps({"photo": ph}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=180))
    except Exception as e:
        print(name, "ERR", e)
        continue
    m = r.get("match", {})
    top = m.get("top")
    print(f"\n=== {name} ===")
    print(" enabled=", m.get("enabled"), " degraded=", m.get("degraded"),
          " nPhotoTexts=", r.get("nPhotoTexts"), " nPhotoCodes=", r.get("nPhotoCodes"))
    if top:
        print(" TOP:", top.get("name"), " G=", top.get("nGreen"), " R=", top.get("nRed"),
              " Y=", top.get("nYellow"), " Gr=", top.get("nGray"))
        for el in top.get("elements", []):
            print(f"   {el.get('color'):6} {el.get('kind'):6} {str(el.get('text')):26} "
                  f"hit={el.get('hit')} L={el.get('mappingLevel')} photoNorm={fmt(el.get('photoNorm'))}")
    for ex in m.get("extra", []):
        print(f"   EXTRA {ex.get('color'):6} {ex.get('kind'):12} {str(ex.get('text')):26} "
              f"L={ex.get('mappingLevel')} photoNorm={fmt(ex.get('photoNorm'))}")
