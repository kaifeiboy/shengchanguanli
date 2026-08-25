# -*- coding: utf-8 -*-
"""根因验证：原始 xls 中每张图的真实视觉位置 vs 位置映射。
用锚点 row2 反推图片真实所属数据行，与"第 i 张→数据行2+i"位置映射对比。"""
import sys, os, base64, hashlib
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import olefile, xlrd

f = r'E:\生产不良履历_迁移前备份\2026年3月份IPQC及QA发现检验问题点.xls'
ole = olefile.OleFileIO(f)
d = ole.openstream('Workbook').read()
ole.close()

records = []
i = 0
while i + 4 <= len(d):
    rt = int.from_bytes(d[i:i+2], 'little'); sz = int.from_bytes(d[i+2:i+4], 'little')
    records.append((rt, d[i+4:i+4+sz]))
    i += 4 + sz

sheets = []
cur = None
for rt, rec in records:
    if rt == 0x0809 and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == 0x0010:
        cur = {'msod': []}
        sheets.append(cur)
    if cur is not None:
        if rt == 0x00EC:
            cur['msod'].append(rec)
        elif rt == 0x003C and cur['msod']:
            cur['msod'].append(rec)

target = sheets[1] if len(sheets) > 1 else sheets[0]

def walk_oa(dd):
    out = []; i = 0
    while i + 8 <= len(dd):
        rt = int.from_bytes(dd[i:i+2], 'little')
        sz = int.from_bytes(dd[i+4:i+8], 'little')
        out.append((rt, dd[i+8:i+8+sz]))
        i += 8 + sz
    return out

def walk_container(rd):
    res = []
    for rt, sub in walk_oa(rd):
        if rt in (0xF000, 0xF001, 0xF002, 0xF003, 0xF004):
            res.extend(walk_container(sub))
        elif rt == 0xF010:
            res.append(('anchor', sub))
    return res

anchors = []
for blob in target['msod']:
    for it in walk_container(blob):
        if it[0] == 'anchor':
            rd = it[1]
            if len(rd) >= 18:
                vals = [int.from_bytes(rd[k:k+2], 'little', signed=False) for k in range(0, 16, 2)]
                anchors.append(tuple(vals))

# 提取图片
import sys
sys.path.insert(0, '.')
import xls_img_extract as X
res = X.analyze_file(f)
asm = [s for s in res['sheets'] if s['name'] == '组装'][0]
imgs = asm['images']  # 位置映射结果

# 读数据行
wb = xlrd.open_workbook(f)
ws = wb.sheet_by_name('组装')
data_rows = []
for r in range(2, ws.nrows):
    vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
    if all(not v for v in vals):
        continue
    data_rows.append((r, vals))

print('图片数:', len(imgs), '| 锚点数:', len(anchors), '| 数据行数:', len(data_rows))
print()
print('=== 位置映射 vs 锚点反推（row2）===')
for i, (im, a) in enumerate(zip(imgs, anchors)):
    mapped_row = im['row']          # 位置映射 → 数据行 2+i
    row1, col1, dx1, dy1, row2, col2, dx2, dy2 = a
    # 锚点 row2 是图片底部所在行（1-based 从 1 开始计数？WPS 中 row 是从 0 开始）
    # 数据行 r2~r16 在 WPS 中 row 索引 1~15（1-based）。row2 直接是行号？
    print('  图#%-2d | 位置映射→数据行%d | 锚点row1=%d row2=%d (1-based ~%d) | md5=%s'
          % (i, mapped_row, row1, row2, row2 + 1, hashlib.md5(im.get('file') and open(im['file'], 'rb').read() or b'').hexdigest()[:10]))
print()
print('=== 数据行映射（供人工核对）===')
for r, vals in data_rows:
    print('  数据行 r%d: %s | %s | %s | %s' % (r, vals[0], vals[1], vals[2], vals[3][:25]))
