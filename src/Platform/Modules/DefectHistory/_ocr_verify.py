# -*- coding: utf-8 -*-
"""OCR 验证：提取原始 xls 每张图，OCR 识别图中型号文字，与数据行型号匹配推断真实归属。"""
import sys, os, io, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')

import xls_img_extract as X
import xlrd
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()

def ocr_text(img_bytes):
    try:
        import numpy as np
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(img_bytes))
        arr = np.array(im.convert('RGB'))
        res, _ = ocr(arr)
        if not res:
            return []
        return [line[1] for line in res]
    except Exception as e:
        return ['OCR_ERR:%s' % e]

f = r'E:\生产不良履历_迁移前备份\2026年3月份IPQC及QA发现检验问题点.xls'
res = X.analyze_file(f, outdir=r'E:\workaaa\shengchanguanli\temp\ocr_orig')
asm = [s for s in res['sheets'] if s['name'] == '组装'][0]

# 读数据行
wb = xlrd.open_workbook(f)
ws = wb.sheet_by_name('组装')
data_rows = []
for r in range(2, ws.nrows):
    vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
    if all(not v for v in vals):
        continue
    data_rows.append((r, vals))

print('=== OCR 每张图，找图中出现的型号字符串 ===')
models_in_data = set()
for r, vals in data_rows:
    models_in_data.add(vals[2].upper())

for i, im in enumerate(asm['images']):
    fpath = im['file']
    with open(fpath, 'rb') as fh:
        bdata = fh.read()
    texts = ocr_text(bdata)
    joined = ' '.join(texts)
    upper = joined.upper()
    hit = [m for m in models_in_data if m and m in upper]
    # 数据行2+i 的型号
    mapped_row = 2 + i
    mapped_model = ''
    for r, vals in data_rows:
        if r == mapped_row:
            mapped_model = vals[2]
            break
    print('图#%-2d 位置映射→行%d(%s) | OCR命中型号: %s' % (i, mapped_row, mapped_model, hit or '无'))
    print('      OCR文本前80字:', joined[:80].replace('\n', ' '))
