# -*- coding: utf-8 -*-
"""_verify_dy1.py —— 验证 WPS ClientAnchor 的 dy1 字段 = 图片真实视觉行号（xlrd 0-based）。

原理：MSODRAWING 记录顺序 = 图片提取顺序；每条 ClientAnchor 的 rd[6:8] 即 dy1，
经验证 = 图片所在数据行号。全部文件逐图输出 dy1 + 行内容，自动找多图行。"""
import sys, os, glob, io
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import olefile
import xlrd
from PIL import Image as PILImage

ORIG = r'E:\生产不良履历_迁移前备份'


def get_anchors(xls_path, sheet_no=1):
    """返回第 sheet_no 个 worksheet（0-based）的 MSODRAWING ClientAnchor dy1 列表（存储顺序）"""
    ole = olefile.OleFileIO(xls_path)
    d = ole.openstream('Workbook').read()
    ole.close()
    records = []
    i = 0
    while i + 4 <= len(d):
        rt = int.from_bytes(d[i:i + 2], 'little')
        sz = int.from_bytes(d[i + 2:i + 4], 'little')
        records.append((rt, d[i + 4:i + 4 + sz]))
        i += 4 + sz
    sheets = []
    cur = None
    for rt, rec in records:
        if rt == 0x0809 and len(rec) >= 4 and int.from_bytes(rec[2:4], 'little') == 0x0010:
            cur = []
            sheets.append(cur)
        if cur is not None:
            if rt == 0x00EC:
                cur.append(bytearray(rec))
            elif rt == 0x003C and cur:
                cur[-1] += rec  # CONTINUE：拼接续段
    target = sheets[sheet_no] if len(sheets) > sheet_no else (sheets[0] if sheets else [])
    blob_all = b"".join(bytes(b) for b in target)  # 0x00EC + CONTINUE 段拼接为完整 OfficeArt 流

    def walk_oa(dd):
        out = []
        j = 0
        while j + 8 <= len(dd):
            rt = int.from_bytes(dd[j + 2:j + 4], 'little')   # rt 在 rh 之后（j+2:j+4）
            sz = int.from_bytes(dd[j + 4:j + 8], 'little')
            out.append((rt, dd[j + 8:j + 8 + sz]))
            j += 8 + sz
        return out

    def walk(rd):
        res = []
        for rt, sub in walk_oa(rd):
            if rt in (0xF000, 0xF001, 0xF002, 0xF003, 0xF004):
                res.extend(walk(sub))
            elif rt == 0xF010:
                res.append(sub)
        return res

    anchors = []
    for rd in walk(blob_all):
        if len(rd) >= 10:
            row1 = int.from_bytes(rd[0:2], 'little')
            dy1 = int.from_bytes(rd[6:8], 'little')   # ← 关键字段
            anchors.append((row1, dy1))
    return anchors


total_ok = 0
total_imgs = 0
all_multi = {}
for f in sorted(glob.glob(os.path.join(ORIG, '*.xls'))):
    base = os.path.basename(f)
    wb = xlrd.open_workbook(f)
    if '组装' not in wb.sheet_names():
        continue
    ws = wb.sheet_by_name('组装')
    # 数据行内容（xlrd 0-based）
    rows = {}
    for r in range(0, ws.nrows):
        vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
        if all(not v for v in vals):
            continue
        if any(v.startswith('合计') for v in vals):
            continue
        rows[r] = vals
    anchors = get_anchors(f, sheet_no=1)  # 组装 = 第 2 个 worksheet（0-based 1）
    print('=== %s | 数据行%d | 图%d ===' % (base[:26], len(rows), len(anchors)))
    by_row = {}
    for i, (row1, dy1) in enumerate(anchors):
        vals = rows.get(dy1)
        if vals:
            match = 'OK'
        else:
            match = '?? 行%d 无数据' % dy1
        by_row.setdefault(dy1, []).append(i)
        print('  图#%-2d dy1=%d | %-12s | %-6s | %-14s | %s' % (i, dy1, vals[0][:12] if vals else '', vals[1][:6] if vals else '', vals[2][:14] if vals else '', match))
        if vals:
            total_ok += 1
        total_imgs += 1
    multi = {r: v for r, v in by_row.items() if len(v) > 1}
    if multi:
        print('  ⚠️ 多图行:', {r: v for r, v in multi.items()})
        all_multi[base] = multi
    wb.release_resources()

print()
print('=== 汇总 ===')
print('图总数:', total_imgs, '| dy1 命中数据行:', total_ok)
print('多图行:', all_multi if all_multi else '无')
