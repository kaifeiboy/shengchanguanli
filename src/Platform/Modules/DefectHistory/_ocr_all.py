# -*- coding: utf-8 -*-
"""对全部 5 个原始 xls 做 OCR 型号匹配，推断每张图的正确归属行。"""
import sys, os, io, glob, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')

import xls_img_extract as X
import xlrd
import numpy as np
from PIL import Image as PILImage
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()

def ocr_models(bdata):
    try:
        im = PILImage.open(io.BytesIO(bdata))
        arr = np.array(im.convert('RGB'))
        res, _ = ocr(arr)
        texts = [line[1] for line in res] if res else []
        return ' '.join(texts)
    except Exception as e:
        return ''

def norm(s):
    return s.upper().replace(' ', '').replace('_', '')

files = sorted(glob.glob(r'E:\生产不良履历_迁移前备份\*.xls'))
os.makedirs(r'E:\workaaa\shengchanguanli\temp\ocr_all_imgs', exist_ok=True)
report = []
for fi, f in enumerate(files):
    base = os.path.basename(f)
    res = X.analyze_file(f, outdir=r'E:\workaaa\shengchanguanli\temp\ocr_all_imgs\%d' % fi)
    wb = xlrd.open_workbook(f)
    for si, s in enumerate(res['sheets']):
        sname = s['name']
        if sname not in ('组装', 'SMT'):
            continue
        ws = wb.sheet_by_name(sname)
        # 数据行
        data_rows = []
        for r in range(2, ws.nrows):
            vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
            if all(not v for v in vals):
                continue
            data_rows.append((r, vals))
        for i, im in enumerate(s['images']):
            bdata = open(im['file'], 'rb').read()
            text = ocr_models(bdata)
            mapped_row = im['row']  # 位置映射
            # 找数据行中与该映射行匹配的记录
            mapped_model = ''
            for r, vals in data_rows:
                if r == mapped_row:
                    mapped_model = vals[2]
                    break
            report.append({
                'file': base, 'sheet': sname, 'img_idx': i,
                'mapped_row': mapped_row, 'mapped_model': mapped_model,
                'ocr_text': text[:200],
            })

with open(r'E:\workaaa\shengchanguanli\temp\ocr_report.json', 'w', encoding='utf-8') as fh:
    json.dump(report, fh, ensure_ascii=False, indent=1)
for r in report:
    print('[%s/%s] 图#%-2d 位置映射→行%d(%s) | OCR: %s' % (
        r['file'][:12], r['sheet'], r['img_idx'], r['mapped_row'], r['mapped_model'], r['ocr_text'][:70]))
