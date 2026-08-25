# -*- coding: utf-8 -*-
"""调试：dump 指定文件全部图片形状的完整 ClientAnchor 18 字节。"""
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
for si, s in enumerate(sheets):
    nm = names[si] if names and si < len(names) else '?'
    n = 0
    for blob in s['blobs']:
        shapes = []
        X.parse_sp_container(bytes(blob), shapes)
        for sh in shapes:
            if sh.anchor:
                # 重新找该形状的 anchor 原始字节
                n += 1
                raw = None
                for rt, rd, off, ver, inst in X.walk_oa(bytes(blob)):
                    if rt == X.ART_CLIENT_ANCHOR:
                        raw = rd.hex()
                print('  %s[%d] anchor=%s raw=%s' % (nm, n, sh.anchor, raw))
