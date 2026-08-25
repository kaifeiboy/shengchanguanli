# -*- coding: utf-8 -*-
"""按标准 ClientAnchor 布局(flag,row1,col1,dx1,dy1,row2,col2,dx2,dy2)解析，同时尝试多种偏移。"""
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

# 从 MSODRAWING 中提取所有 ClientAnchor(0xF010) 原始 18 字节
raw_anchors = []
for blob in target['msod']:
    j = 0
    while j + 8 <= len(blob):
        art = int.from_bytes(blob[j:j+2], 'little')
        asz = int.from_bytes(blob[j+4:j+8], 'little')
        if art == 0xF010 and asz >= 16:
            raw_anchors.append(blob[j+8:j+8+asz])
        j += 8 + asz

print('anchors:', len(raw_anchors))
for k, rd in enumerate(raw_anchors):
    # 标准 18 字节：flag,row1,col1,dx1,dy1,row2,col2,dx2,dy2
    v = [int.from_bytes(rd[i:i+2], 'little') for i in range(0, min(len(rd), 18), 2)]
    flag, row1, col1, dx1, dy1, row2, col2, dx2, dy2 = v[:9]
    # 也输出 dx/dy 换算像素（EMU/9525? 或 twips?）
    print('#%-2d flag=%-3d row1=%-3d col1=%-3d dx1=%-4d dy1=%-3d | row2=%-3d col2=%-3d dx2=%-4d dy2=%-3d'
          % (k, flag, row1, col1, dx1, dy1, row2, col2, dx2, dy2))
