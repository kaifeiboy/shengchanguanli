# -*- coding: utf-8 -*-
"""调试：转储 .xls 的 MSODRAWINGGROUP / 每 sheet MSODRAWING 的 OfficeArt 记录树。"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import olefile
import xls_img_extract as X

path = sys.argv[1] if len(sys.argv) > 1 else r'E:\workaaa\shengchanguanli\data\defecthistory_test\2025年11月份IPQC及QA发现检验问题点.xls'

ole = olefile.OleFileIO(path)
data = ole.openstream('Workbook').read()
ole.close()
records = list(X.walk_biff(data))

names = X.sheet_names(path)
print('sheets:', names)

# 定位第一个 worksheet BOF
first_sheet_idx = None
for idx, (rt, rec, hpos) in enumerate(records):
    if rt == X.REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == X.DT_WORKSHEET:
        first_sheet_idx = idx
        break
print('first worksheet BOF record idx:', first_sheet_idx)

# globals MSODRAWINGGROUP
seg, started = [], False
for rt, rec, hpos in records[:first_sheet_idx]:
    if rt == X.REC_MSODRAWINGGROUP:
        started = True; seg.append(rec)
    elif rt == X.REC_CONTINUE and started:
        seg.append(rec)
    elif started:
        break
blob = b''.join(seg)
print('MSODRAWINGGROUP blob len:', len(blob))
print('--- globals OA tree (depth 2) ---')
for rt, rd, off, ver, inst in X.walk_oa(blob):
    print('  top', hex(rt), 'len', len(rd), 'ver', ver, 'inst', inst)
    if rt == X.ART_DGG_CONTAINER:
        for rt2, rd2, off2, ver2, inst2 in X.walk_oa(rd):
            print('    dgg>', hex(rt2), 'len', len(rd2), 'ver', ver2, 'inst', inst2)
            if rt2 == X.ART_BSTORE_CONTAINER:
                for rt3, rd3, off3, ver3, inst3 in X.walk_oa(rd2):
                    print('      bstore>', hex(rt3), 'len', len(rd3), 'ver', ver3, 'inst', inst3)

# per-sheet MSODRAWING
sheets = []
cur = None
for idx, (rt, rec, hpos) in enumerate(records):
    if rt == X.REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == X.DT_WORKSHEET:
        cur = {'msod_seg': [], 'objs': 0}; sheets.append(cur)
    if cur is not None and rt == X.REC_EOF:
        cur = None
    if cur is not None:
        if rt == X.REC_MSODRAWING:
            cur['msod_seg'] = [rec]
        elif rt == X.REC_CONTINUE and cur['msod_seg']:
            cur['msod_seg'].append(rec)
        elif rt == X.REC_OBJ:
            cur['objs'] += 1

for si, s in enumerate(sheets):
    msod = b''.join(s['msod_seg'])
    print('--- sheet', si, names[si] if names and si < len(names) else '?', 'msod len', len(msod), 'objs', s['objs'], '---')
    for rt, rd, off, ver, inst in X.walk_oa(msod):
        print('  ', hex(rt), 'len', len(rd), 'ver', ver, 'inst', inst)
        if rt == X.ART_DG_CONTAINER:
            for rt2, rd2, off2, ver2, inst2 in X.walk_oa(rd):
                print('    dg>', hex(rt2), 'len', len(rd2), 'ver', ver2, 'inst', inst2)
                if rt2 in (X.ART_SP_CONTAINER, 0xF00A, 0xF009):
                    for rt3, rd3, off3, ver3, inst3 in X.walk_oa(rd2):
                        print('      sp>', hex(rt3), 'len', len(rd3), 'ver', ver3, 'inst', inst3)
                        if rt3 == X.ART_OPT and len(rd3) < 200:
                            print('        OPT data:', rd3.hex()[:240])
                        if rt3 in (X.ART_CLIENT_ANCHOR, X.ART_CLIENT_DATA):
                            print('        ANCHOR data:', rd3.hex()[:80])
