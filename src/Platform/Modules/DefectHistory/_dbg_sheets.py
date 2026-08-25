# -*- coding: utf-8 -*-
"""调试：每文件每 sheet 的 msod 长度、DgContainer 结构、形状/锚点统计。"""
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
    sheets, cur = [], None
    for rt, rec, hpos in records:
        if rt == X.REC_BOF and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == X.DT_WORKSHEET:
            cur = {'seg': [], 'objs': 0}
            sheets.append(cur)
        if cur is not None and rt == X.REC_EOF:
            cur = None
        if cur is not None:
            if rt == X.REC_MSODRAWING:
                cur['seg'] = [rec]
            elif rt == X.REC_CONTINUE and cur['seg']:
                cur['seg'].append(rec)
            elif rt == X.REC_OBJ:
                cur['objs'] += 1
    print('===', os.path.basename(f))
    names = X.sheet_names(f)
    for si, s in enumerate(sheets):
        msod = b''.join(s['seg'])
        # count shapes with anchors
        shapes = []
        for rt, rd, off, ver, inst in X.walk_oa(msod):
            if rt == X.ART_DG_CONTAINER:
                for rt2, rd2, off2, ver2, inst2 in X.walk_oa(rd):
                    if rt2 in (X.ART_SP_CONTAINER, X.ART_SPGR_CONTAINER):
                        X.parse_sp_container(rd2, shapes)
        with_anchor = [sh for sh in shapes if sh.anchor]
        name = names[si] if names and si < len(names) else '?'
        print('  %-6s msod=%6d objs=%d shapes=%d anchors=%d' % (name, len(msod), s['objs'], len(shapes), len(with_anchor)))
        for sh in with_anchor[:12]:
            print('      anchor=%s spid=%s pib=%s' % (sh.anchor, sh.spid, sh.pib))
