# -*- coding: utf-8 -*-
"""调试：dump 每文件每 sheet 每个图片形状的锚点原始 hex 与 row1/col1 解析值。"""
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
    sheets = X.collect_sheet_msods(records)
    print('===', os.path.basename(f))
    for si, s in enumerate(sheets):
        nm = names[si] if names and si < len(names) else '?'
        anchors = []
        for blob in s['blobs']:
            shapes = []
            X.parse_sp_container(bytes(blob), shapes)
            for sh in shapes:
                if sh.anchor:
                    anchors.append(sh.anchor)
        print('  %-6s images=%d anchors=%s' % (nm, len(anchors), anchors[:14]))
