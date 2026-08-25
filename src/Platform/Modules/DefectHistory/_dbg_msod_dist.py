# -*- coding: utf-8 -*-
"""调试：列出每文件全部 MSODRAWING/OBJ/CONTINUE 记录的 sheet 归属与大小。"""
import sys, os, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import olefile
import xls_img_extract as X

TEST_DIR = r'E:\workaaa\shengchanguanli\data\defecthistory_test'
for f in sorted(glob.glob(os.path.join(TEST_DIR, '*.xls'))):
    ole = olefile.OleFileIO(f)
    data = ole.openstream('Workbook').read()
    ole.close()
    records = list(X.walk_biff(data))
    names = X.sheet_names(f)
    # sheet 边界（record idx）
    sheet_bounds = []  # (sheet_idx, start_idx, end_idx)
    cur = None
    for idx, (rt, rec, hpos) in enumerate(records):
        if rt == X.REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == X.DT_WORKSHEET:
            cur = idx
        if cur is not None and rt == X.REC_EOF:
            sheet_bounds.append((len(sheet_bounds), cur, idx))
            cur = None
    print('===', os.path.basename(f))
    for si, start, end in sheet_bounds:
        msod_recs, objs, conts = [], 0, 0
        for idx in range(start, end):
            rt, rec, hpos = records[idx]
            if rt == X.REC_MSODRAWING:
                msod_recs.append(len(rec))
            elif rt == X.REC_CONTINUE:
                conts += 1
            elif rt == X.REC_OBJ:
                objs += 1
        nm = names[si] if names and si < len(names) else '?'
        print('  %-6s msod_recs=%s (sizes=%s) objs=%d cont=%d' % (nm, len(msod_recs), msod_recs[:6], objs, conts))
