#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 V2 真值标注工作台（自包含 HTML，照片内嵌）。

用途：让人工标注员用浏览器打开后，对每张实物照片逐标判定
      present(实物有标) / missing(实物缺标) / offscreen(拍不到)，
      并确认本照片的真实视图，最后一键导出 truth_answers.json，
      再由 harvest_truth.py 回填进 testset.json 出 B 类指标。

不依赖任何外部服务器；照片会被缩小到最长边 <=1000px 后 base64 内嵌，
离线可用、可拷到任意机器。
"""
import base64
import csv
import json
import os

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", ".."))
TESTSET = os.path.join(HERE, "testset.json")
TEMPLATE = os.path.join(HERE, "truth_template.csv")
REPORT = os.path.join(HERE, "baseline_report_1280.json")
OUT = os.path.join(HERE, "truth_workbook.html")

MAX_SIDE = 1000
JPEG_QUALITY = 80

# 视图下拉候选（从模板提取）
VIEWS = ["TopCover", "Bottom", "Left", "Right", "Front", "Back", "Other", "未知"]


def b64_photo(rel):
    p = os.path.join(ROOT, rel) if not os.path.isabs(rel) else rel
    try:
        img = cv2.imread(p)
        if img is None:
            return None
        h, w = img.shape[:2]
        s = max(w, h)
        if s > MAX_SIDE:
            f = MAX_SIDE / float(s)
            img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        return None


def load_view_hints():
    """从最新基线报告取每个用例的推断视图，作为视图下拉的默认提示。"""
    hints = {}
    if os.path.exists(REPORT):
        try:
            d = json.load(open(REPORT, encoding="utf-8"))
            for r in d.get("results", []):
                if r.get("ok"):
                    hints[r["id"]] = r.get("inferredView") or ""
        except Exception:
            pass
    return hints


def load_marks():
    """用例 -> [ {markKey, text, view, systemState} ]"""
    by_case = {}
    with open(TEMPLATE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            by_case.setdefault(r["caseId"], []).append({
                "markKey": r["markKey"], "text": r.get("text", ""),
                "view": r.get("view", ""), "systemState": r.get("systemState", ""),
            })
    return by_case


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# 系统判定 -> 配色（仅作提示，不可照抄）
STATE_COLOR = {
    "Matched": "#2e7d32",        # 绿：系统认为一致
    "Missing": "#c62828",        # 红：系统认为缺标
    "LowConfidence": "#f9a825",  # 黄：低置信
    "NotDetected": "#ef6c00",    # 橙：未检出
    "Wrong": "#6a1b9a",          # 紫：部位错
    "Extra": "#00838f",          # 青：多标
    "NotApplicable": "#9e9e9e",  # 灰：不适用
    "NotComparable": "#757575",  # 灰：不可比
}
STATE_HINT = {
    "Matched": "系统：一致（绿）", "Missing": "系统：缺标（红）",
    "LowConfidence": "系统：低置信（黄）", "NotDetected": "系统：未检出（橙）",
    "Wrong": "系统：部位错（紫）", "Extra": "系统：多标（青）",
    "NotApplicable": "系统：不适用（灰）", "NotComparable": "系统：不可比（灰）",
}


def render_case(cid, photo_rel, marks, view_hint):
    img = b64_photo(photo_rel)
    img_html = ('<img class="photo" src="%s" alt="%s"/>' % (img, esc(cid))) if img else \
               '<div class="nophoto">照片读取失败：%s</div>' % esc(photo_rel)
    # 视图下拉
    opts = []
    for v in VIEWS:
        sel = " selected" if v == (view_hint or "") else ""
        opts.append('<option value="%s"%s>%s</option>' % (esc(v), sel, esc(v)))
    if view_hint and view_hint not in VIEWS:
        opts.insert(0, '<option value="%s" selected>%s</option>' % (esc(view_hint), esc(view_hint)))
    view_sel = '<select class="viewsel"><option value="">—请选择—</option>%s</select>' % "".join(opts)

    rows = []
    for m in marks:
        st = m["systemState"]
        color = STATE_COLOR.get(st, "#9e9e9e")
        chip = '<span class="chip" style="background:%s">%s</span>' % (color, esc(STATE_HINT.get(st, st)))
        text = esc(m["text"]) or "<i>（图标/无文本）</i>"
        rows.append(
            '<tr class="mark" data-mk="%s">'
            '<td class="mk">%s</td>'
            '<td class="tx">%s</td>'
            '<td class="sy">%s</td>'
            '<td class="rb">'
            '<label><input type="radio" name="t" value="present"/>有标</label>'
            '<label><input type="radio" name="t" value="missing"/>缺标</label>'
            '<label><input type="radio" name="t" value="offscreen"/>拍不到</label>'
            '</td>'
            '<td class="nt"><input class="note" type="text" placeholder="备注(可选)"/></td>'
            '</tr>' % (esc(m["markKey"]), esc(m["markKey"]), text, chip)
        )
    return (
        '<section class="case" data-cid="%s">'
        '<h2>%s <span class="cnt">（%d 个标）</span></h2>'
        '<div class="meta">本照片真实视图：%s &nbsp;|&nbsp; 照片：%s</div>'
        '<div class="imgwrap">%s</div>'
        '<table class="grid"><thead><tr>'
        '<th>markKey</th><th>期望文本</th><th>系统判定(仅提示)</th>'
        '<th>真值(请独立判断)</th><th>备注</th>'
        '</tr></thead><tbody>%s</tbody></table>'
        '</section>' % (esc(cid), esc(cid), len(marks), view_sel, esc(photo_rel), img_html, "".join(rows))
    )


def main():
    ts = json.load(open(TESTSET, encoding="utf-8"))
    cases = ts["cases"]
    marks_by_case = load_marks()
    view_hints = load_view_hints()

    sections = []
    for c in cases:
        cid = c["id"]
        marks = marks_by_case.get(cid, [])
        if not marks:
            continue
        sections.append(render_case(cid, c["photoRel"], marks, view_hints.get(cid, "")))

    total = sum(len(marks_by_case.get(c["id"], [])) for c in cases)
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>V2 真值标注工作台</title>
<style>
 body{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#fafafa;color:#222;margin:0;padding:0;}
 header{position:sticky;top:0;background:#1565c0;color:#fff;padding:10px 16px;z-index:10;box-shadow:0 2px 6px rgba(0,0,0,.2);}
 header h1{margin:0;font-size:18px;}
 .bar{margin-top:6px;font-size:13px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;}
 .bar button{font-size:13px;padding:6px 12px;cursor:pointer;border:0;border-radius:6px;background:#ffd54f;color:#222;font-weight:600;}
 .legend{font-size:12px;line-height:1.7;background:#fff;padding:10px 16px;border-bottom:1px solid #ddd;}
 .legend code{background:#eee;padding:1px 5px;border-radius:3px;}
 section.case{border:1px solid #ddd;background:#fff;margin:14px 16px;padding:12px 14px;border-radius:8px;}
 section.case h2{margin:0 0 6px;font-size:15px;}
 .cnt{color:#888;font-weight:400;font-size:12px;}
 .meta{font-size:12px;color:#555;margin-bottom:8px;}
 .viewsel{font-size:12px;padding:2px 4px;}
 .imgwrap{text-align:center;background:#111;padding:6px;border-radius:6px;margin-bottom:10px;}
 img.photo{max-width:100%%;max-height:760px;border-radius:4px;}
 .nophoto{color:#fff;padding:30px;}
 table.grid{width:100%%;border-collapse:collapse;font-size:12px;}
 table.grid th,table.grid td{border:1px solid #e0e0e0;padding:5px 7px;vertical-align:top;text-align:left;}
 table.grid th{background:#eceff1;position:sticky;top:96px;}
 tr.mark:nth-child(even){background:#f7f9fc;}
 .chip{color:#fff;padding:1px 7px;border-radius:10px;font-size:11px;white-space:nowrap;}
 .rb label{margin-right:10px;cursor:pointer;white-space:nowrap;}
 .note{width:100%%;box-sizing:border-box;font-size:12px;}
 .mk{font-family:monospace;font-size:11px;word-break:break-all;max-width:200px;}
 textarea#out{width:calc(100%% - 32px);height:160px;margin:10px 16px;font-family:monospace;font-size:11px;}
 .done{color:#2e7d32;font-weight:600;}
</style></head><body>
<header><h1>V2 真值标注工作台（人工目视判读）</h1>
<div class="bar">
 <span id="prog">已填 0 / %d</span>
 <button onclick="exportAnswers()">导出 truth_answers.json</button>
 <button onclick="showJson()">显示 JSON 到下方文本框</button>
</div></header>
<div class="legend">
<b>判定三态（对每张实物照片独立判断，<u>不要抄系统判定</u>）：</b><br>
• <code>present</code> 实物有此标 —— 即使系统判 NotDetected/Matched，只要实物上确实打了对的标就选它（这是召回信号）。<br>
• <code>missing</code> 实物确实没有此标 —— 若系统判 Matched，即为<b>假绿</b>；若系统判 Missing，即为<b>真缺标</b>（系统正确）。<br>
• <code>offscreen</code> 该标在照片取景范围外 / 被手指或外壳完全遮挡看不到 —— 不计召回，也不要猜。<br>
<b>视图</b>：选本照片实际拍的是产品的哪个面（TopCover 顶盖等），用于视图正确率。<br>
<b>合格红线</b>：每个标必须凭眼判断、三态互斥且只选一个；offscreen 仅限真的拍不到；系统判定一列只是提示，禁止照抄。
</div>
%s
<h3 style="margin:16px">导出 JSON（点上方按钮后这里会出现内容，可复制保存为 truth_answers.json）：</h3>
<textarea id="out" placeholder="点击「显示 JSON」或「导出」后此处出现内容"></textarea>
<script>
function collect(){
  const out={cases:[]};
  document.querySelectorAll('section.case').forEach(sec=>{
    const cid=sec.dataset.cid;
    const view=sec.querySelector('.viewsel').value;
    const marks=[];
    sec.querySelectorAll('tr.mark').forEach(tr=>{
      const mk=tr.dataset.mk;
      const sel=tr.querySelector('input[name=t]:checked');
      const note=(tr.querySelector('.note').value||'').trim();
      if(sel) marks.push({markKey:mk, truth:sel.value, note:note});
    });
    out.cases.push({caseId:cid, view:view, marks:marks});
  });
  return out;
}
function updateProg(){
  const total=%d; let done=0;
  document.querySelectorAll('section.case').forEach(sec=>{ done+=sec.querySelectorAll('tr.mark input[name=t]:checked').length; });
  document.getElementById('prog').textContent='已填 '+done+' / '+total;
}
document.addEventListener('change', updateProg);
function showJson(){ document.getElementById('out').value=JSON.stringify(collect(),null,2); updateProg(); }
function exportAnswers(){
  const json=JSON.stringify(collect(),null,2);
  document.getElementById('out').value=json; updateProg();
  const b=new Blob([json],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(b); a.download='truth_answers.json'; a.click();
}
updateProg();
</script>
</body></html>""" % (total, "".join(sections), total)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成：%s" % OUT)
    print("  用例数=%d  标记总数=%d  文件大小=%.1f MB" % (len(sections), total, len(html) / 1e6))


if __name__ == "__main__":
    main()
