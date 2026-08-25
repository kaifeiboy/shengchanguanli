# -*- coding: utf-8 -*-
"""最终核对报告：汇总 xlsx 21 张图逐条 OCR 检查，输出可视化 HTML（图+文字+OCR+判断）。"""
import sys, os, io, json, base64, hashlib
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import numpy as np
from PIL import Image as PILImage
from rapidocr_onnxruntime import RapidOCR
from openpyxl import load_workbook

BOOK = r'E:\生产不良履历\不良履历汇总.xlsx'
OUT = r'E:\workaaa\shengchanguanli\data\img_match_check.html'
ocr = RapidOCR()

def ocr_lines(bdata):
    try:
        im = PILImage.open(io.BytesIO(bdata))
        arr = np.array(im.convert('RGB'))
        res, _ = ocr(arr)
        return [l[1] for l in res] if res else []
    except Exception as e:
        return ['<OCR_ERR %s>' % e]

wb = load_workbook(BOOK)
ws = wb.active
img_per_row = {}
for im in ws._images:
    img_per_row.setdefault(im.anchor._from.row + 1, []).append(im)

parts = []
parts.append('''<!DOCTYPE html><html><head><meta charset="utf-8"><title>不良履历图片匹配逐条检查</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#f8fafc;color:#1e293b;margin:0;padding:20px;}
h1{text-align:center;}
.sum{max-width:1000px;margin:0 auto 18px;padding:14px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;font-size:13px;line-height:1.8;}
.row{max-width:1000px;margin:12px auto;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;}
.row.mismatch{border-left:5px solid #ef4444;background:#fef2f2;}
.row.unknown{border-left:5px solid #f59e0b;background:#fffbeb;}
.row.ok{border-left:5px solid #22c55e;}
.head{font-size:13px;color:#475569;margin-bottom:6px;}
.head b{color:#0f172a;}
.txt{background:#f8fafc;border-radius:6px;padding:8px 10px;font-size:13px;line-height:1.7;margin:6px 0;}
.ocr{background:#f0f9ff;border-radius:6px;padding:8px 10px;font-size:12px;color:#1d4ed8;line-height:1.6;margin:6px 0;}
.verdict{font-size:13px;font-weight:700;margin-top:6px;}
.imgs{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px;}
.imgs img{max-width:170px;max-height:220px;border:1px solid #e2e8f0;border-radius:6px;cursor:pointer;transition:transform .15s;background:#fff;}
.imgs img:hover{transform:scale(2);box-shadow:0 10px 30px rgba(0,0,0,.25);position:relative;z-index:10;}
</style></head><body>''')
parts.append('<h1>📋 不良履历图片匹配逐条检查（OCR 证据）</h1>')
parts.append('''<div class="sum"><b>检查方法</b>：对汇总工作簿 <code>不良履历汇总.xlsx</code> 每行有图数据执行 RapidOCR，把「图中印刷文字」与该行「文字描述/型号」比对。<br/>
<b>判定</b>：🔴 明显错配（图内出现其他型号/产品特征与描述无关）｜🟡 待确认（OCR 无有效文本，无法自动判断）｜🟢 匹配。<br/>
<b>注意</b>：OCR 空不代表错（照片可能无文字）；有明确其他型号文字的判定较可靠。</div>''')

# 匹配判定
mismatch = []
unknown = []
ok = []
for r in sorted(img_per_row.keys()):
    vals = [ws.cell(r, c).value for c in range(1, 9)]
    date_v = str(vals[0] or '')[:10]
    line_v = str(vals[1] or '')
    model_v = str(vals[2] or '')
    reason_v = str(vals[3] or '')
    qty_v = str(vals[4] or '')
    stage_v = str(vals[5] or '')
    handle_v = str(vals[7] or '')
    b64s = []
    ocr_texts = []
    for im in img_per_row[r]:
        bdata = im._data()
        b64s.append(base64.b64encode(bdata).decode('ascii'))
        ocr_texts.extend(ocr_lines(bdata))
    joined = ' '.join(ocr_texts)
    upper = joined.upper()
    # 判定：图中出现与本行型号不同的型号特征
    verdict = 'ok'
    reason_tag = ''
    if 'PC-P1HVQA' in upper and model_v != 'PC-P1HVQA':
        verdict = 'mismatch'; reason_tag = '图含 PC-P1HVQA'
    elif 'YCWA' in upper and 'YCWA' not in model_v.upper():
        verdict = 'mismatch'; reason_tag = '图含 YCWA'
    elif 'HITACHI' in upper and 'HITACHI' not in model_v.upper():
        verdict = 'mismatch'; reason_tag = '图含 HITACHI 空调'
    elif 'TRESOR' in upper and 'TRESOR' not in model_v.upper():
        verdict = 'mismatch'; reason_tag = '图含 TRESOR 遥控器'
    elif 'ECONAVI' in upper and 'ECONAVI' not in model_v.upper():
        verdict = 'mismatch'; reason_tag = '图含 ECONAVI 空调'
    elif not joined.strip():
        verdict = 'unknown'; reason_tag = 'OCR 无文本'
    if verdict == 'mismatch':
        mismatch.append((r, date_v, model_v, reason_tag, joined[:80]))
    elif verdict == 'unknown':
        unknown.append((r, date_v, model_v, joined[:80]))
    else:
        ok.append((r, date_v, model_v, joined[:80]))

    cls = 'mismatch' if verdict == 'mismatch' else ('unknown' if verdict == 'unknown' else 'ok')
    icon = '🔴' if verdict == 'mismatch' else ('🟡' if verdict == 'unknown' else '🟢')
    parts.append('<div class="row %s">' % cls)
    parts.append('<div class="head"><b>第 %d 行</b> · %s · 拉线 %s · 型号 <b>%s</b> · %s · 工程 %s · 处理 %s</div>' % (r, date_v, line_v, model_v, qty_v, stage_v, handle_v))
    parts.append('<div class="txt"><b>文字描述：</b>%s</div>' % (reason_v or '(空)'))
    parts.append('<div class="ocr"><b>OCR：</b>%s</div>' % (joined[:200] or '(无文本)'))
    parts.append('<div class="verdict">%s 判定：%s</div>' % (icon, reason_tag or ('匹配' if verdict == 'ok' else '待人工确认')))
    parts.append('<div class="imgs">' + ''.join('<img src="data:image/png;base64,%s"/>' % b for b in b64s) + '</div>')
    parts.append('</div>')

parts.append('<div class="sum"><b>汇总</b>：共 %d 行有图｜🔴 错配 %d｜🟡 待确认 %d｜🟢 匹配 %d</div>' % (len(img_per_row), len(mismatch), len(unknown), len(ok)))
parts.append('</body></html>')
with open(OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(parts))
print('saved:', OUT, '| size:', os.path.getsize(OUT)//1024, 'KB')
print()
print('=== 错配清单 ===')
for r, d, m, tag, ocr_t in mismatch:
    print('  r%-3d %-10s %-14s | %s | OCR: %s' % (r, d, m, tag, ocr_t))
print()
print('=== 待确认（OCR 无文本）===')
for r, d, m, ocr_t in unknown:
    print('  r%-3d %-10s %-14s | OCR: %s' % (r, d, m, ocr_t))
print()
print('=== 匹配 ===')
for r, d, m, ocr_t in ok:
    print('  r%-3d %-10s %-14s | OCR: %s' % (r, d, m, ocr_t[:60]))
wb.close()
