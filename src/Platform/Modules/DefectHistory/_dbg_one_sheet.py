# -*- coding: utf-8 -*-
"""调试：dump 指定 sheet 的 MSODRAWING 完整 hex 与 OA 树。"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import olefile
import xls_img_extract as X

f = sys.argv[1] if len(sys.argv) > 1 else r'E:\workaaa\shengchanguanli\data\defecthistory_test\2026年3月份IPQC及QA发现检验问题点.xls'
target_sheet = int(sys.argv[2]) if len(sys.argv) > 2 else 1
ole = olefile.OleFileIO(f)
data = ole.openstream('Workbook').read()
ole.close()
records = list(X.walk_biff(data))
sheets, cur = [], None
for rt, rec, hpos in records:
    if rt == X.REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == X.DT_WORKSHEET:
        cur = {'seg': [], 'objs': []}
        sheets.append(cur)
    if cur is not None and rt == X.REC_EOF:
        cur = None
    if cur is not None:
        if rt == X.REC_MSODRAWING:
            cur['seg'] = [rec]
        elif rt == X.REC_CONTINUE and cur['seg']:
            cur['seg'].append(rec)
        elif rt == X.REC_OBJ:
            cur['objs'].append(rec)

s = sheets[target_sheet]
msod = b''.join(s['seg'])
print(os.path.basename(f), 'sheet', target_sheet, 'msod len', len(msod), 'objs', len(s['objs']))
print('hex:', msod.hex())
print('--- OA tree ---')
def walk(data, depth=0):
    i, n = 0, len(data)
    while i + 8 <= n:
        ver_inst = int.from_bytes(data[i:i+2], 'little')
        rt = int.from_bytes(data[i+2:i+4], 'little')
        sz = int.from_bytes(data[i+4:i+8], 'little')
        rd = data[i+8:i+8+sz]
        print('  ' * depth + '%s len=%d ver=%d inst=%d' % (hex(rt), sz, ver_inst & 0x0F, ver_inst >> 12))
        if rt in (0xF002, 0xF003, 0xF004):
            walk(rd, depth + 1)
        elif rt in (0xF00A,):
            print('  ' * depth + '    Sp data: ' + rd.hex())
        elif rt in (0xF00B, 0xF010, 0xF011, 0xF009):
            print('  ' * depth + '    data: ' + rd.hex()[:140])
        i += 8 + sz
walk(msod)
