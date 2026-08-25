# -*- coding: utf-8 -*-
"""dump 原始 xls 组装 sheet 所有形状的完整锚点原始字节 + 顺序（对照 WPS 存储顺序 vs 位置映射）。"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import olefile

f = sys.argv[1] if len(sys.argv) > 1 else r'E:\生产不良履历_迁移前备份\2026年3月份IPQC及QA发现检验问题点.xls'
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
        cur = {'msod': [], 'order': []}
        sheets.append(cur)
    if cur is not None:
        if rt == 0x00EC:
            cur['msod'].append(rec)
            cur['order'].append(len(cur['msod']) - 1)  # 标记这是第 N 条 MSODRAWING
        elif rt == 0x003C and cur['msod']:
            cur['msod'].append(rec)

# 组装 = sheets[1]
target = sheets[1] if len(sheets) > 1 else sheets[0]

def walk_oa(dd):
    out = []; i = 0
    while i + 8 <= len(dd):
        ver_inst = int.from_bytes(dd[i:i+2], 'little')
        rt = int.from_bytes(dd[i+2:i+4], 'little')
        sz = int.from_bytes(dd[i+4:i+8], 'little')
        out.append((rt, dd[i+8:i+8+sz]))
        i += 8 + sz
    return out

def walk_container(rd, depth=0):
    """递归遍历容器，收集 ClientAnchor 与 SP 顺序"""
    res = []
    for rt, sub in walk_oa(rd):
        if rt in (0xF000, 0xF001, 0xF002, 0xF003, 0xF004):
            res.extend(walk_container(sub, depth + 1))
        elif rt == 0xF010:  # ClientAnchor
            res.append(('anchor', sub))
        elif rt == 0xF00A:  # ClientTextbox / sp
            res.append(('sp', sub))
        elif rt == 0xF00B:  # OPT
            res.append(('opt', sub))
    return res

print('=== 组装 sheet：每条 MSODRAWING 记录的形状锚点（存储顺序）===')
for bi, blob in enumerate(target['msod']):
    items = walk_container(blob)
    for it in items:
        if it[0] == 'anchor':
            rd = it[1]
            if len(rd) >= 18:
                row1, col1, dx1, dy1, row2, col2, dx2, dy2 = [
                    int.from_bytes(rd[k:k+2], 'little', signed=False) for k in range(0, 16, 2)]
                flag = int.from_bytes(rd[16:18], 'little')
                print('  MSODRAWING#%d  anchor: row1=%d col1=%d dx1=%d dy1=%d | row2=%d col2=%d dx2=%d dy2=%d flag=%d'
                      % (bi, row1, col1, dx1, dy1, row2, col2, dx2, dy2, flag))
            else:
                print('  MSODRAWING#%d  anchor(len=%d): %s' % (bi, len(rd), rd.hex()))
print()
print('MSODRAWING 记录条数:', len(target['msod']))