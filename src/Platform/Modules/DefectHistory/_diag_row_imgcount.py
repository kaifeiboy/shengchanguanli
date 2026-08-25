# -*- coding: utf-8 -*-
"""_diag_row_imgcount.py —— 逐文件诊断：原始 xls 每行应得图数（OCR 归属 + 位置映射），
找出"一行 2 图"的行，并与当前合并结果对比，验证是否丢图/错配。"""
import sys, os, glob, io
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xls_img_extract as X
import xlrd
import numpy as np
from PIL import Image as PILImage
from rapidocr_onnxruntime import RapidOCR
import merge_in_cell as MC

ocr = RapidOCR()
norm = MC.norm

OUT = r'E:\workaaa\shengchanguanli\temp\diag_rowimgs'
os.makedirs(OUT, exist_ok=True)

report = []
for f in sorted(glob.glob(r'E:\生产不良履历_迁移前备份\*.xls')):
    base = os.path.basename(f)
    wb = xlrd.open_workbook(f)
    if '组装' not in wb.sheet_names():
        continue
    ws = wb.sheet_by_name('组装')
    rows = []
    for r in range(2, ws.nrows):
        vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
        if all(not v for v in vals):
            continue
        if any(v.startswith('合计') for v in vals):
            continue
        rows.append((r, vals))
    res = X.analyze_file(f, outdir=os.path.join(OUT, base[:8]))
    asm = [s for s in res['sheets'] if s['name'] == '组装'][0]
    imgs = asm['images']
    row_model = {r: norm(vals[2]) for r, vals in rows}
    model_rows = {}
    for r, vals in rows:
        model_rows.setdefault(row_model[r], []).append(r)

    # 归属决策（同 merge 脚本：OCR 型号匹配 → 型号行池按序 → 位置映射兜底）
    assigned = {}
    model_used = {}
    ocr_hits = []
    for i, im in enumerate(imgs):
        bdata = open(im['file'], 'rb').read()
        joined = norm(' '.join(MC.ocr_text(bdata)))
        best = None
        best_len = 0
        for r, vals in rows:
            mn = row_model[r]
            if not mn or len(mn) < 5:
                continue
            if mn in joined and len(mn) > best_len:
                best, best_len = mn, len(mn)
        if best is None:
            pc = set()
            for r, vals in rows:
                mn = row_model[r]
                if mn and len(mn) >= 8 and mn[:4] in joined:
                    pc.add(mn)
            if len(pc) == 1:
                best = pc.pop()
        if best and best in model_rows:
            pool = model_rows[best]
            k = model_used.get(best, 0)
            if k < len(pool):
                assigned[i] = pool[k]
                model_used[best] = k + 1
                ocr_hits.append((i, best, pool[k]))
                continue
        pm = 2 + i
        assigned[i] = pm if pm in row_model else (rows[0][0] if rows else 2)

    # 每行图数
    by_row = {}
    for i, r in assigned.items():
        by_row.setdefault(r, []).append(i)

    print('=== %s | 数据行%d | 图%d | OCR命中%d ===' % (base[:24], len(rows), len(imgs), len(ocr_hits)))
    for r, vals in rows:
        if r in by_row:
            print('  行%-2d | %-14s | %-30s | 图#%s' % (r, vals[2], vals[3][:30], by_row[r]))
    multi = {r: v for r, v in by_row.items() if len(v) > 1}
    print('  ⚠️ 多图行:', multi if multi else '无')
    # OCR 命中详情
    for i, mn, r in ocr_hits:
        print('     [OCR] 图#%d 型号=%s → 行%d' % (i, mn, r))
    report.append((base, len(rows), len(imgs), len(ocr_hits), multi))
    wb.release_resources()

print()
print('=== 汇总 ===')
for b, nr, ni, no, multi in report:
    print('  %-28s 行%d 图%d OCR%d 多图行:%s' % (b[:28], nr, ni, no, multi or '-'))
