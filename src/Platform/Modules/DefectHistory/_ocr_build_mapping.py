# -*- coding: utf-8 -*-
"""OCR 增强图片归属：对 5 个原始 xls 的每张图，OCR 识别图内型号文字，
与数据行型号匹配，推断每张图正确的数据行（替代不可靠的位置映射）。
输出 mapping JSON 供合并脚本使用。"""
import sys, os, io, glob, json, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xls_img_extract as X
import xlrd
import numpy as np
from PIL import Image as PILImage
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()

def norm(s):
    return (s or '').upper().replace(' ', '').replace('_', '')

def ocr_text(bdata):
    try:
        im = PILImage.open(io.BytesIO(bdata))
        arr = np.array(im.convert('RGB'))
        res, _ = ocr(arr)
        return [l[1] for l in res] if res else []
    except Exception:
        return []

def parse_date(s):
    s = str(s).strip()
    m = re.match(r'^(\d{4,5})[.\-/](\d{1,2})[.\-/](\d{1,2})$', s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y >= 10000:
        y -= 10000
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return (y, mo, d)

# 收集所有数据行型号（含原始写法）
all_models = set()

# 预读所有文件的型号集合
file_info = {}  # fname -> {sheet: {row: vals}}
for f in sorted(glob.glob(r'E:\生产不良履历_迁移前备份\*.xls')):
    base = os.path.basename(f)
    wb = xlrd.open_workbook(f)
    for sname in ('组装', 'SMT'):
        if sname not in wb.sheet_names():
            continue
        ws = wb.sheet_by_name(sname)
        rows = []
        for r in range(2, ws.nrows):
            vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
            if all(not v for v in vals):
                continue
            rows.append((r, vals))
            if sname == '组装' and vals[2]:
                all_models.add(norm(vals[2]))
        file_info.setdefault(base, {})[sname] = rows

# 对每张图 OCR + 匹配
result = {}
for f in sorted(glob.glob(r'E:\生产不良履历_迁移前备份\*.xls')):
    base = os.path.basename(f)
    res = X.analyze_file(f, outdir=r'E:\workaaa\shengchanguanli\temp\mapimg\%s' % base[:10])
    for si, s in enumerate(res['sheets']):
        sname = s['name']
        if sname == 'SMT':
            continue
        rows = file_info[base].get('组装', [])
        # 该文件数据行（组装）
        imgs = s['images']
        file_map = {}
        for i, im in enumerate(imgs):
            bdata = open(im['file'], 'rb').read()
            texts = ocr_text(bdata)
            joined = ' '.join(texts)
            upper = norm(joined)
            # 匹配型号：图中出现某个数据行型号（最长匹配优先）
            best = None
            best_len = 0
            for m in all_models:
                if m and len(m) >= 4 and m in upper:
                    if len(m) > best_len:
                        best = m
                        best_len = len(m)
            # 找到 best 对应的数据行（该文件中第一个同型号行）
            target_row = None
            if best:
                for r, vals in rows:
                    if norm(vals[2]) == best:
                        target_row = r
                        break
            file_map[i] = {
                'ocr': joined[:120],
                'matched_model': best,
                'target_row': target_row,   # 该文件内 xlrd 行号
            }
            print('%s 组装 图#%-2d → %s' % (base[:12], i, ('行%d(%s)' % (target_row, best)) if target_row else '无法OCR匹配'))
        result[base] = file_map

# 输出 JSON
out = r'E:\workaaa\shengchanguanli\temp\ocr_mapping.json'
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w', encoding='utf-8') as fh:
    json.dump(result, fh, ensure_ascii=False, indent=1)
print()
print('mapping saved:', out)
