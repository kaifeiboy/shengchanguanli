# -*- coding: utf-8 -*-
"""dump 组装 sheet 全部 MSODRAWING 记录的完整 hex，人工解析锚点。"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import olefile

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

# 组装 sheet = 第 2 个 worksheet BOF
sheets = []
cur = None
for rt, rec in records:
    if rt == 0x0809 and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == 0x0010:
        cur = {'msod': [], 'objs': []}
        sheets.append(cur)
    if cur is not None:
        if rt == 0x00EC:
            cur['msod'].append(rec)
        elif rt == 0x003C and cur['msod']:
            cur['msod'].append(rec)
        elif rt == 0x005D:
            cur['objs'].append(rec)

target = sheets[1] if len(sheets) > 1 else sheets[0]
print('MSODRAWING 记录数:', len(target['msod']), '| OBJ 数:', len(target['objs']))

for bi, blob in enumerate(target['msod'][:4]):
    print()
    print('===== MSODRAWING#%d (len=%d) hex =====' % (bi, len(blob)))
    # 打印前 200 字节，按 16 一组
    for off in range(0, min(len(blob), 240), 16):
        chunk = blob[off:off+16]
        hexs = ' '.join('%02X' % b for b in chunk)
        print('  %04X: %s' % (off, hexs))
