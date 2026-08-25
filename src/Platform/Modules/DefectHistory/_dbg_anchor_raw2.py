# -*- coding: utf-8 -*-
"""调试：递归 dump 每个图片形状的完整 ClientAnchor 18 字节。"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import olefile
import xls_img_extract as X

f = sys.argv[1] if len(sys.argv) > 1 else r'E:\workaaa\shengchanguanli\data\defecthistory_test\2026年3月份IPQC及QA发现检验问题点.xls'
ole = olefile.OleFileIO(f)
data = ole.openstream('Workbook').read()
ole.close()
records = list(X.walk_biff(data))
names = X.sheet_names(f)
sheets = X.collect_sheet_msods(records)
print(os.path.basename(f))

def find_anchors(rd, out):
    for rt, srd, off, ver, inst in X.walk_oa(rd):
        if rt in (X.ART_SP_CONTAINER, X.ART_SPGR_CONTAINER, X.ART_DG_CONTAINER):
            find_anchors(srd, out)
        elif rt == X.ART_CLIENT_ANCHOR:
            out.append(srd.hex())

for si, s in enumerate(sheets):
    nm = names[si] if names and si < len(names) else '?'
    all_anchors = []
    for blob in s['blobs']:
        aa = []
        find_anchors(bytes(blob), aa)
        if aa:
            all_anchors.append(aa[0])
    for i, a in enumerate(all_anchors):
        # 多种布局尝试
        b = bytes.fromhex(a)
        r1 = int.from_bytes(b[0:2], 'little'); c1 = int.from_bytes(b[2:4], 'little')
        r2a = int.from_bytes(b[4:6], 'little'); c2a = int.from_bytes(b[6:8], 'little')
        dx1 = int.from_bytes(b[8:10], 'little')
        r2b = int.from_bytes(b[10:12], 'little')
        print('  %s[%d] hex=%s  A(row1=%d,col1=%d,row2=%d,col2=%d,dx1=%d)  B(row2=%d,dx1=%d)' % (
            nm, i + 1, a, r1, c1, r2a, c2a, dx1, r2b, dx1))
