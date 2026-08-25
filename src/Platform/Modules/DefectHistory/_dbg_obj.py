# -*- coding: utf-8 -*-
"""调试：dump 每 sheet 的 OBJ 记录与 MSODRAWING 中 ClientData/ClientAnchor 原始 hex。"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import olefile
import xls_img_extract as X

path = sys.argv[1] if len(sys.argv) > 1 else r'E:\workaaa\shengchanguanli\data\defecthistory_test\2025年11月份IPQC及QA发现检验问题点.xls'
ole = olefile.OleFileIO(path)
data = ole.openstream('Workbook').read()
ole.close()
records = list(X.walk_biff(data))

sheets = []
cur = None
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

names = X.sheet_names(path)
for si, s in enumerate(sheets):
    print('==== sheet', si, names[si] if names and si < len(names) else '?', '====')
    print('OBJ records:', len(s['objs']))
    for j, ob in enumerate(s['objs']):
        print('  OBJ[%d] len=%d hex=%s' % (j, len(ob), ob.hex()))
    msod = b''.join(s['seg'])
    print('MSODRAWING len', len(msod))
    for rt, rd, off, ver, inst in X.walk_oa(msod):
        if rt in (X.ART_CLIENT_DATA, X.ART_CLIENT_ANCHOR):
            print('  OA %s len=%d hex=%s' % (hex(rt), len(rd), rd.hex()[:100]))
        elif rt == X.ART_SP:
            print('  Sp len=%d hex=%s' % (len(rd), rd.hex()))
        elif rt == X.ART_OPT:
            print('  OPT len=%d hex=%s' % (len(rd), rd.hex()[:200]))
