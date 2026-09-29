import sys, fitz, sqlite3
from collections import Counter
sys.path.insert(0, r"E:/workaaa/shengchanguanli/src/Platform/Modules/DrawingsV2/Scripts/vpdf")
import logical_blocks as LB

def old_kind(t):
    if LB._is_dim_text(t): return "dim"
    if LB._is_note_text(t): return "note"
    return "text"

IDS = (50,52,56,57,61,62,64,67)
db=r"E:/workaaa/shengchanguanli/data/drawingsv2.db"
c=sqlite3.connect(db); c.execute("PRAGMA journal_mode=WAL"); cur=c.cursor()
q="SELECT id, drawing_key, pdf_path FROM v2_drawing_profiles WHERE id IN (%s)" % ",".join("?"*len(IDS))
cur.execute(q, IDS); pdfs=cur.fetchall()
lines=["图纸数: %d" % len(pdfs)]
total=[]
for pid,key,path in pdfs:
    try:
        rep = LB.run_separation(path)
    except Exception as e:
        lines.append("[skip %d] separation fail: %s" % (pid, str(e)[:100])); continue
    pg = rep["pages"][0]
    doc=fitz.open(path)
    try:
        page=doc[0]; page.remove_rotation()
        dd=page.get_text("dict")
        allspans=[]
        for b in dd.get("blocks",[]):
            for ln in b.get("lines",[]) or []:
                for sp in ln.get("spans",[]) or []:
                    t=(sp.get("text") or "").strip()
                    if t: allspans.append((t, fitz.Rect(*sp["bbox"]), sp.get("font") or "", round(float(sp.get("size") or 0),2)))
        ctx=LB.PageTextCtx(allspans)
        seen=set(); out=[]
        for b in pg["blocks"]:
            rect=fitz.Rect(*b["bbox_pt"])
            for bb in dd.get("blocks",[]):
                for ln in bb.get("lines",[]) or []:
                    for sp in ln.get("spans",[]) or []:
                        txt=(sp.get("text") or "").strip()
                        if not txt: continue
                        r=fitz.Rect(*sp["bbox"])
                        if (r & rect).is_empty or (r & rect).get_area() < 0.5*r.get_area(): continue
                        nk=LB.classify_text(txt, ctx, sp.get("font"), sp.get("size"), r)
                        ok=old_kind(txt)
                        if nk!=ok and (txt, sp.get("font")) not in seen:
                            seen.add((txt, sp.get("font")))
                            out.append((txt, sp.get("font"), round(float(sp.get("size") or 0),2), ok, nk))
        if out:
            lines.append("\n"+"="*78)
            lines.append("profile %d  %s" % (pid, key[:46]))
            for t,f,s,o,n in out:
                lines.append("   %-16s %-15s %6.2f  %-5s -> %-5s %s" % (repr(t)[:16],f,s,o,n,"NEW" if n=="text" else ""))
            total+=out
    finally:
        doc.close()
lines.append("\n"+"#"*78)
lines.append("去重变化合计: %d" % len(total))
lines.append("流向: %s" % Counter([(o,n) for _,_,_,o,n in total]).most_common())
open(r"E:/workaaa/shengchanguanli/tools/_diff_l0l1.txt","w",encoding="utf-8").write("\n".join(lines))
print("done")
