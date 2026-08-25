# -*- coding: utf-8 -*-
"""逐条检查：汇总 xlsx 每行图片 + OCR 内容 + 该行文字描述，判断匹配与否。"""
import sys, os, io, json, hashlib, base64
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import numpy as np
from PIL import Image as PILImage
from rapidocr_onnxruntime import RapidOCR
from openpyxl import load_workbook

BOOK = r'E:\生产不良履历\不良履历汇总.xlsx'
ocr = RapidOCR()

def ocr_text(bdata):
    try:
        im = PILImage.open(io.BytesIO(bdata))
        arr = np.array(im.convert('RGB'))
        res, _ = ocr(arr)
        return [l[1] for l in res] if res else []
    except Exception as e:
        return ['OCR_ERR:%s' % e]

wb = load_workbook(BOOK)
ws = wb.active
img_per_row = {}
for im in ws._images:
    img_per_row.setdefault(im.anchor._from.row + 1, []).append(im)

print('=== 逐条核对（图片内容 OCR ↔ 行文字描述） ===')
report = []
for r in sorted(img_per_row.keys()):
    vals = [ws.cell(r, c).value for c in range(1, 9)]
    date_v = str(vals[0] or '')[:10]
    line_v = str(vals[1] or '')
    model_v = str(vals[2] or '')
    reason_v = str(vals[3] or '')
    qty_v = str(vals[4] or '')
    stage_v = str(vals[5] or '')
    handle_v = str(vals[7] or '')
    texts = []
    for im in img_per_row[r]:
        bdata = im._data()
        texts.append(ocr_text(bdata))
    all_ocr = ' | '.join(' '.join(t) for t in texts)
    entry = {
        'row': r, 'date': date_v, 'line': line_v, 'model': model_v,
        'reason': reason_v[:60], 'qty': qty_v, 'stage': stage_v, 'handle': handle_v,
        'ocr': all_ocr[:150],
    }
    report.append(entry)
    print('r%-3d | %-10s | %-6s | %-12s | 工程:%-4s | %-5s' % (r, date_v, line_v, model_v, stage_v, qty_v))
    print('     文字: %s' % reason_v[:50])
    print('     OCR : %s' % all_ocr[:120])
wb.close()

with open(r'E:\workaaa\shengchanguanli\temp\img_check_report.json', 'w', encoding='utf-8') as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
print()
print('报告已存 temp/img_check_report.json')
