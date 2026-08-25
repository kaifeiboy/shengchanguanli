# -*- coding: utf-8 -*-
"""_verify_merge_dy1.py —— 合并结果校验器（长期杜绝错配的自动化防线）

验证项：
  1. 图数守恒：原始 21 锚点 == 合并嵌入图（多图行拼接不丢图）
  2. 逐图归属核对：合并 xlsx 每行图片 == dy1 权威归属的原始图
     - 单图行：media PNG == 原始图转 PNG（md5）
     - 多图行：拼接图按比例裁切 == 原始两图转 PNG（md5）
  3. 表格完整性：行数 32 / 表头 / 日期升序

用法：python _verify_merge_dy1.py   （退出码 0=全过 1=有失败）
"""
import sys, os, io, glob, zipfile, hashlib, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xls_img_extract as X
import xlrd
from PIL import Image as PILImage
from openpyxl import load_workbook
import merge_in_cell as MC

ORIG = r'E:\生产不良履历_迁移前备份'
BOOK = r'E:\生产不良履历\不良履历汇总.xlsx'
fail = 0


def md5(b):
    return hashlib.md5(b).hexdigest()[:12]


def png_bytes_of_img(im):
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=False)
    return buf.getvalue()


def crop(bdata, left, right):
    im = PILImage.open(io.BytesIO(bdata))
    w, h = im.size
    return im.crop((int(w * left), 0, int(w * right), h))


def main():
    global fail
    # ---- 期望表：(file, xlrd_row) -> [原图bytes...]（dy1 权威归属）----
    expect = {}
    total_anchors = 0
    file_rows = {}   # (file, xlrd_row) -> (date_str, model_str)
    for f in sorted(glob.glob(os.path.join(ORIG, '*.xls'))):
        base = os.path.basename(f)
        wb = xlrd.open_workbook(f)
        if '组装' not in wb.sheet_names():
            continue
        ws = wb.sheet_by_name('组装')
        rows = []
        for r in range(2, ws.nrows):
            vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
            if all(not v for v in vals):
                continue
            if any(v.startswith('合计') for v in vals):
                continue
            rows.append((r, vals))
        row_set = {r for r, _ in rows}
        for r, vals in rows:
            file_rows[(base, r)] = (vals[0], vals[2])
        res = X.analyze_file(f, outdir=r'E:\workaaa\shengchanguanli\temp\mimg_verify\%s' % base[:8])
        asm = [s for s in res['sheets'] if s['name'] == '组装'][0]
        imgs = asm['images']
        anchors = MC.get_anchors(f, sheet_no=1)
        if len(anchors) != len(imgs):
            print('  [FAIL] %s 锚点数(%d)!=图片数(%d)' % (base, len(anchors), len(imgs)))
            fail += 1
            continue
        for i, im in enumerate(imgs):
            r = anchors[i]
            if r not in row_set:
                print('  [FAIL] %s 图#%d 锚点行%d 无数据' % (base, i, r))
                fail += 1
                continue
            expect.setdefault((base, r), []).append(open(im['file'], 'rb').read())
            total_anchors += 1
        wb.release_resources()

    # ---- 合并结果：G 列 in-cell 单元格 → media 图片 ----
    z = zipfile.ZipFile(BOOK)
    sheet_xml = z.read('xl/worksheets/sheet1.xml').decode('utf-8')
    cells = [(int(m.group(1)), int(m.group(2))) for m in
             re.finditer(r'<c r="G(\d+)"[^>]*t="e" vm="(\d+)"', sheet_xml)]
    rels = dict(re.findall(r'<Relationship Id="rId(\d+)"[^>]*Target="([^"]+)"',
                           z.read('xl/richData/_rels/richValueRel.xml.rels').decode('utf-8')))
    merged = {}   # xlsx 行(1-based) -> bdata
    for g_row, vm in cells:
        media = rels.get(str(vm))
        if not media:
            print('  [FAIL] vm=%d 无 media 映射' % vm)
            fail += 1
            continue
        merged[g_row] = z.read(media.replace('../media/', 'xl/media/'))
    z.close()

    # ---- 表格完整性 ----
    wb = load_workbook(BOOK)
    ws = wb[MC.SHEET_NAME]
    n_data = ws.max_row - 2
    headers = [ws.cell(2, c).value for c in range(1, 9)]
    prev = None
    bad_order = 0
    for r in range(3, ws.max_row + 1):
        d = ws.cell(r, 1).value
        if d is None:
            continue
        if prev and d < prev:
            bad_order += 1
        prev = d
    print('表格: 数据行 %d (应32) | 表头 %s | 日期乱序 %d (应0)' % (n_data, headers == MC.HEADERS, bad_order))
    if n_data != 32 or headers != MC.HEADERS or bad_order:
        fail += 1

    # ---- 逐图核对：期望表(按 文件内行序) ↔ 合并行(按 日期+型号 key 队列配对) ----
    print('期望图组 %d / 锚点 %d / 合并单元格 %d' % (len(expect), total_anchors, len(merged)))
    # 合并表 key 队列：key=(日期str, 型号) → [xlsx 行...]（日期升序）
    merged_queue = {}
    for rr in range(3, ws.max_row + 1):
        dcell = ws.cell(rr, 1).value
        mcell = str(ws.cell(rr, 3).value or '').strip()
        if dcell is None:
            continue
        key = (str(dcell)[:10], mcell)
        merged_queue.setdefault(key, []).append(rr)
    checked = 0
    for (base, r), bdatas in sorted(expect.items()):
        dstr, model = file_rows[(base, r)]
        dd = MC.parse_date(dstr)
        key = (dd.strftime('%Y-%m-%d'), model)
        q = merged_queue.get(key, [])
        if not q:
            print('  [FAIL] %s 行%d(%s %s) 合并表无此 key' % (base, r, dstr, model))
            fail += 1
            continue
        target_row = q.pop(0)   # 同 key 按顺序配对
        got = merged.get(target_row)
        if got is None:
            print('  [FAIL] %s 行%d → 合并行%d 无图' % (base, r, target_row))
            fail += 1
            continue
        if len(bdatas) == 1:
            ok = md5(MC.to_png_bytes(bdatas[0])) == md5(got)
            print('  %-4s %-12s 行%-2d → 合并行%-2d 单图 %s' % (
                'OK' if ok else 'FAIL', base[:12], r, target_row, '' if ok else '✗ md5 不一致'))
            if not ok:
                fail += 1
        else:
            # 多图行：用 hstack_png 同参数重拼期望，整体 md5 对比（避免裁切缝隙误差）
            expect_merged = MC.hstack_png([MC.to_png_bytes(b) for b in bdatas])
            ok = md5(expect_merged) == md5(got)
            print('  %-4s %-12s 行%-2d → 合并行%-2d 多图%d张 %s' % (
                'OK' if ok else 'FAIL', base[:12], r, target_row, len(bdatas), '' if ok else '✗ 拼接不符'))
            if not ok:
                fail += 1
        checked += 1
    wb.close()

    print()
    print('=== 校验完成：核对 %d 组 / 锚点 %d | 失败 %d ===' % (checked, total_anchors, fail))
    print('结论:', '✅ 全部通过（合并与原始 dy1 归属一致，无丢图无错配）' if fail == 0 else '❌ 存在失败')
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
