# -*- coding: utf-8 -*-
"""
merge_in_cell.py —— 以迁移前备份为唯一标准重建汇总工作簿；
图片用 **Excel 365 in-cell image（图片真正嵌入单元格）** 嵌入 G 列对应行。

满足用户两点要求：
  1. 图片嵌入"当前对应的行"，形成完整表格；嵌入原始像素 PNG（不损画质）
  2. 表格具备完整行/列/框线（图片在单元格内渲染，不遮挡边框，随单元格移动/排序）

实现：openpyxl 生成完整表格（含框线/日期/列宽/行高适配，G 列占位）→
     zipfile 注入 in-cell image 所需全部部件：
       sheet XML 单元格改 t="e" vm="N"（保留边框样式 s）
       metadata.xml / richData/rdrichvalue.xml / rdrichvaluestructure.xml /
       rdRichValueTypes.xml / richValueRel.xml(.rels) / xl/media/imageN.png
       [Content_Types].xml / xl/_rels/workbook.xml.rels 补条目
（格式参考 place-in-cell 库：https://pypi.org/project/place-in-cell）
"""
import sys, os, io, glob, re, datetime, zipfile, shutil
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xls_img_extract as X
import xlrd
from PIL import Image as PILImage
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter

ORIG_DIR = r'E:\生产不良履历_迁移前备份'
OUT_BOOK = r'E:\生产不良履历\不良履历汇总.xlsx'
SHEET_NAME = '不良履历'
HEADERS = ['日期', '线别', '型号', '问题原因', '数量', '发生工程', '不良图片', '处理方式']
G_COL_W = 16            # G 列宽（字符），in-cell 图片显示宽度≈此列宽
G_COL_PX = 115.0        # G 列近似像素宽（16字符×7px+5px）
ROW_PAD_PT = 3.0        # 行高余量，防止图片被裁切


def parse_date(s):
    """容错：任意格式 → date；5 位年份如 20226.3.14 → 2026-03-14（丢弃第3位冗余数字）。"""
    s = str(s).strip()
    if not s:
        return None
    s2 = s.replace('/', '.').replace('-', '.').replace('年', '.').replace('月', '.').replace('日', '.')
    parts = [p for p in s2.split('.') if p.strip()]
    if len(parts) < 3:
        return None
    ys, mo_s, d_s = parts[0], parts[1], parts[2]
    if len(ys) == 5 and ys.startswith('20') and ys[2] == '2':
        ys = ys[:2] + ys[3:]
    try:
        y, mo, d = int(ys), int(mo_s), int(d_s)
        return datetime.date(y, mo, d)
    except (ValueError, OverflowError):
        return None


def read_sheet_rows(ws):
    rows = []
    for r in range(2, ws.nrows):
        vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
        if all(not v for v in vals):
            continue
        if any(v.startswith('合计') for v in vals):
            continue
        rows.append((r, vals))
    return rows


def get_anchors(xls_path, sheet_no=1):
    """解析 xls 组装 sheet 的 MSODRAWING ClientAnchor dy1（真实视觉行号，xlrd 0-based）。

    关键发现（2026-08-10 验证 21/21 命中）：WPS 的 ClientAnchor 中 rd[6:8]（dy1 字段）
    = 图片所在数据行号（xlrd 0-based），而非位置映射的"存储顺序"。
    MSODRAWING 记录顺序 = 图片提取顺序（第 i 条 anchor ↔ 第 i 张图）。
    """
    import olefile
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
    blob_all = b"".join(bytes(b) for b in target)  # 完整 OfficeArt 流

    def walk_oa(dd):
        out = []
        j = 0
        while j + 8 <= len(dd):
            rt = int.from_bytes(dd[j + 2:j + 4], 'little')   # rt 在 rh 之后
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
            dy1 = int.from_bytes(rd[6:8], 'little')
            anchors.append(dy1)
    return anchors


def hstack_png(imgs_bdata, gap=8):
    """一行多图：横向无损拼接成一张 PNG（统一最高高度、白底、保留原始像素）"""
    pil_imgs = []
    for b in imgs_bdata:
        im = PILImage.open(io.BytesIO(b))
        if im.mode != 'RGB':
            im = im.convert('RGB')
        pil_imgs.append(im)
    h = max(im.size[1] for im in pil_imgs)
    w_total = sum(im.size[0] for im in pil_imgs) + gap * (len(pil_imgs) - 1)
    canvas = PILImage.new('RGB', (w_total, h), (255, 255, 255))
    x = 0
    for im in pil_imgs:
        canvas.paste(im, (x, 0))
        x += im.size[0] + gap
    buf = io.BytesIO()
    canvas.save(buf, 'PNG', optimize=False)
    return buf.getvalue()


def collect():
    """收集：all_records=[{date, vals, file, imgs:[bdata,...]}]
    图片归属 = WPS ClientAnchor dy1（真实视觉行号，21/21 验证命中），一行可多图（拼接嵌入）。
    图数守恒断言：提取图数 == 嵌入图数（杜绝丢图）。"""
    files = sorted(glob.glob(os.path.join(ORIG_DIR, '*.xls')))
    all_records = []
    warnings = []
    total_imgs = 0
    total_anchors = 0
    for f in files:
        base = os.path.basename(f)
        wb = xlrd.open_workbook(f)
        if '组装' not in wb.sheet_names():
            continue
        ws = wb.sheet_by_name('组装')
        rows = read_sheet_rows(ws)
        row_set = {r for r, _ in rows}
        res = X.analyze_file(f, outdir=r'E:\workaaa\shengchanguanli\temp\mimg\%s' % base[:10])
        asm = [s for s in res['sheets'] if s['name'] == '组装'][0]
        imgs = asm['images']

        # ★ 权威归属：第 i 张图 → dy1[i] 行号（MSODRAWING 顺序 = 提取顺序）
        anchors = get_anchors(f, sheet_no=1)
        if len(anchors) != len(imgs):
            warnings.append('%s 锚点数(%d) != 图片数(%d)，跳过该文件图片归属！' % (base, len(anchors), len(imgs)))
            anchors = []
        row_img = {}
        for i, im in enumerate(imgs):
            r = anchors[i] if i < len(anchors) else None
            if r is None or r not in row_set:
                warnings.append('%s 图#%d 锚点行%d 不在数据行中' % (base, i, r))
                continue
            bdata = open(im['file'], 'rb').read()
            row_img.setdefault(r, []).append(bdata)
            total_imgs += 1
        total_anchors += len(anchors)

        for r, vals in rows:
            d = parse_date(vals[0])
            if d is None:
                warnings.append('%s 行%d 日期解析失败: %s' % (base, r, vals[0]))
                continue
            rec = {'date': d, 'vals': vals, 'file': base}
            if r in row_img and row_img[r]:
                rec['imgs'] = row_img[r]   # 可能 2 张（多图行）
            all_records.append(rec)
        wb.release_resources()

    all_records.sort(key=lambda x: x['date'])
    print('记录总数:', len(all_records), '| 图片总数:', total_imgs, '| 锚点总数:', total_anchors)
    if total_imgs != total_anchors:
        raise SystemExit('图数守恒失败: 提取 %d != 锚点 %d —— 中止，防丢图' % (total_imgs, total_anchors))
    if warnings:
        print('--- 警告 ---')
        for w in warnings:
            print(' ', w)
    return all_records


def to_png_bytes(bdata):
    """无损转 PNG（不重采样，保留原始像素）"""
    im = PILImage.open(io.BytesIO(bdata))
    if im.mode not in ('RGB', 'RGBA'):
        im = im.convert('RGB' if im.mode == 'P' else im.mode)
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=False)
    return buf.getvalue()


def img_disp_h_pt(w, h):
    """图片在 G 列宽内、保持纵横比的显示高度（pt）"""
    if not w:
        return 20.0
    h_px = G_COL_PX * h / w
    return max(20.0, h_px * 0.75) + ROW_PAD_PT


def build_table(all_records, tmp_xlsx):
    """openpyxl 生成完整表格（无浮动图），G 列占位空格保留边框，有图行行高适配"""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
    tcell = ws.cell(1, 1, '生产不良履历汇总')
    tcell.font = Font(bold=True, size=14)
    tcell.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 26

    HEAD_FILL = PatternFill('solid', fgColor='DDEBF7')
    BORDER = Border(*[Side(style='thin', color='9CA3AF')] * 4)
    for c, h in enumerate(HEADERS, start=1):
        cell = ws.cell(2, c, h)
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = BORDER
    ws.row_dimensions[2].height = 20

    for c, w in enumerate([14, 10, 18, 44, 12, 12, 16, 14], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w

    cell_imgs = []  # (row_1based, png_bytes)
    for i, rec in enumerate(all_records):
        xr = 3 + i
        vals = rec['vals']
        ws.cell(xr, 1, rec['date']).number_format = 'yyyy-mm-dd'
        for c in range(2, 9):
            v = vals[c - 1] if c - 1 < len(vals) else ''
            ws.cell(xr, c, v if v not in ('', None) else None)
        for c in range(1, 9):
            ws.cell(xr, c).border = BORDER
        # G 列占位（空格）→ 保证 G 列单元格存在且有边框样式
        ws.cell(xr, 7, ' ')
        if rec.get('imgs'):
            imgs_list = rec['imgs']
            if len(imgs_list) > 1:
                # 一行多图（如 2025-12 行6 YCWA16NCWQ 两张）→ 横向无损拼接为一张，两图都保留
                merged = hstack_png([to_png_bytes(b) for b in imgs_list])
            else:
                merged = to_png_bytes(imgs_list[0])
            png = merged
            w_px, h_px = PILImage.open(io.BytesIO(png)).size
            ws.row_dimensions[xr].height = img_disp_h_pt(w_px, h_px)
            cell_imgs.append((xr, png))
    ws.freeze_panes = 'A3'
    os.makedirs(os.path.dirname(tmp_xlsx), exist_ok=True)
    wb.save(tmp_xlsx)
    wb.close()
    print('表格已生成:', tmp_xlsx, '| 行数:', len(all_records) + 2, '| 待嵌图:', len(cell_imgs))
    return cell_imgs


# ---------------- in-cell image 注入 ----------------

def _inject_xml(draw_from, entries):
    """sheet1.xml 中把 G 列有图行占位单元格替换为 t="e" vm="N"（保留 s 边框样式）。

    vm 为 1-based value-metadata 索引，必须逐图递增（指向各自 rich value）。
    """
    for vm, (row_1b, _) in enumerate(entries, start=1):
        r = row_1b
        pat = re.compile(r'<c r="G%d"[^>]*?(?:\/>|>.*?<\/c>)' % r, re.S)
        m = pat.search(draw_from)
        if not m:
            raise RuntimeError('sheet XML 中未找到 G%d 单元格' % r)
        old = m.group(0)
        s_attr = re.search(r's="(\d+)"', old)
        s = (' s="%s"' % s_attr.group(1)) if s_attr else ''
        new = '<c r="G%d"%s t="e" vm="%d"><v>#VALUE!</v></c>' % (r, s, vm)
        draw_from = draw_from.replace(old, new, 1)
    return draw_from


def _content_types_with(ct, n_local):
    add = []
    if '<Default Extension="png"' not in ct:
        add.append('  <Default Extension="png" ContentType="image/png"/>')
    for part, cttype in [
        ('/xl/metadata.xml', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml'),
        ('/xl/richData/rdrichvalue.xml', 'application/vnd.ms-excel.rdrichvalue+xml'),
        ('/xl/richData/rdrichvaluestructure.xml', 'application/vnd.ms-excel.rdrichvaluestructure+xml'),
        ('/xl/richData/rdRichValueTypes.xml', 'application/vnd.ms-excel.rdrichvaluetypes+xml'),
        ('/xl/richData/richValueRel.xml', 'application/vnd.ms-excel.richvaluerel+xml'),
    ]:
        if ('PartName="%s"' % part) not in ct:
            add.append('  <Override PartName="%s" ContentType="%s"/>' % (part, cttype))
    if not add:
        return ct
    return ct.replace('</Types>', '\n'.join(add) + '\n</Types>')


def _workbook_rels_with(rels, n_local):
    add = [
        ('http://schemas.openxmlformats.org/officeDocument/2006/relationships/sheetMetadata', 'metadata.xml'),
        ('http://schemas.microsoft.com/office/2017/06/relationships/rdRichValue', 'richData/rdrichvalue.xml'),
        ('http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueStructure', 'richData/rdrichvaluestructure.xml'),
        ('http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueTypes', 'richData/rdRichValueTypes.xml'),
        ('http://schemas.microsoft.com/office/2022/10/relationships/richValueRel', 'richData/richValueRel.xml'),
    ]
    used = [int(x) for x in re.findall(r'Id="rId(\d+)"', rels)]
    nxt = max(used) + 1 if used else 1
    lines = []
    for typ, target in add:
        lines.append('  <Relationship Id="rId%d" Type="%s" Target="%s"/>' % (nxt, typ, target))
        nxt += 1
    return rels.replace('</Relationships>', '\n'.join(lines) + '\n</Relationships>')


def inject_in_cell(xlsx_path, cell_imgs):
    """把 (row_1based, png_bytes) 注入为 G 列 in-cell image"""
    n = len(cell_imgs)
    entries = sorted(cell_imgs, key=lambda x: x[0])  # 按行升序，vm 顺序

    tmp = xlsx_path + '.tmp.zip'
    zin = zipfile.ZipFile(xlsx_path, 'r')
    zout = zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED)
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == 'xl/worksheets/sheet1.xml':
            data = _inject_xml(data.decode('utf-8'), entries).encode('utf-8')
        elif item.filename == '[Content_Types].xml':
            data = _content_types_with(data.decode('utf-8'), n).encode('utf-8')
        elif item.filename == 'xl/_rels/workbook.xml.rels':
            data = _workbook_rels_with(data.decode('utf-8'), n).encode('utf-8')
        zout.writestr(item, data)
    # 新部件
    zout.writestr('xl/metadata.xml', _metadata_xml(n))
    zout.writestr('xl/richData/rdrichvaluestructure.xml', _rich_struct_xml())
    zout.writestr('xl/richData/rdRichValueTypes.xml', _RICH_TYPES)
    zout.writestr('xl/richData/rdrichvalue.xml', _rich_values_xml(n))
    zout.writestr('xl/richData/richValueRel.xml', _rich_value_rel_xml(n))
    zout.writestr('xl/richData/_rels/richValueRel.xml.rels', _rich_value_rel_rels_xml(n))
    for i, (_, png) in enumerate(entries, start=1):
        zout.writestr('xl/media/image%d.png' % i, png)
    zin.close()
    zout.close()
    shutil.move(tmp, xlsx_path)
    print('in-cell image 注入完成:', n, '张')


def _metadata_xml(n):
    future = '\n'.join(
        '    <bk><extLst><ext uri="{3e2802c4-a4d2-4d8b-9148-e3be6c30e623}">'
        '<xlrd:rvb i="%d" xmlns:xlrd="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"/>'
        '</ext></extLst></bk>' % i for i in range(n))
    values = '\n'.join('    <bk><rc t="1" v="%d"/></bk>' % i for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:xlrd="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata">\n'
            '  <metadataTypes count="1">\n'
            '    <metadataType name="XLRICHVALUE" minSupportedVersion="120000" '
            'copy="1" pasteAll="1" pasteValues="1" merge="1" splitFirst="1" '
            'rowColShift="1" clearFormats="1" clearComments="1" assign="1" coerce="1"/>\n'
            '  </metadataTypes>\n'
            '  <futureMetadata name="XLRICHVALUE" count="%d">\n%s\n  </futureMetadata>\n'
            '  <valueMetadata count="%d">\n%s\n  </valueMetadata>\n'
            '</metadata>' % (n, future, n, values))


def _rich_struct_xml():
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<rvStructures xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata" count="1">\n'
            '  <s t="_localImage">\n'
            '    <k n="_rvRel:LocalImageIdentifier" t="i"/>\n'
            '    <k n="CalcOrigin" t="i"/>\n'
            '  </s>\n'
            '</rvStructures>')


_RICH_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
               '<rvTypesInfo xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata2" '
               'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
               'mc:Ignorable="x" xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n'
               '  <global>\n'
               '    <keyFlags>\n'
               '      <key name="_Self"><flag name="ExcludeFromFile" value="1"/><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_DisplayString"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_Flags"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_Format"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_SubLabel"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_Attribution"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_Icon"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_Display"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_CanonicalPropertyNames"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '      <key name="_ClassificationId"><flag name="ExcludeFromCalcComparison" value="1"/></key>\n'
               '    </keyFlags>\n'
               '  </global>\n'
               '</rvTypesInfo>')


def _rich_values_xml(n):
    body = '\n'.join('  <rv s="0"><v>%d</v><v>5</v></rv>' % i for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<rvData xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata" count="%d">\n'
            '%s\n</rvData>' % (n, body))


def _rich_value_rel_xml(n):
    rels = '\n'.join('  <rel r:id="rId%d"/>' % (i + 1) for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<richValueRels xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
            '%s\n</richValueRels>' % rels)


def _rich_value_rel_rels_xml(n):
    lines = '\n'.join(
        '  <Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image%d.png"/>'
        % (i + 1, i + 1) for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '%s\n</Relationships>' % lines)


def main():
    all_records = collect()
    if not all_records:
        print('无数据，退出')
        return
    cell_imgs = build_table(all_records, OUT_BOOK)
    if cell_imgs:
        inject_in_cell(OUT_BOOK, cell_imgs)
    print('完成:', OUT_BOOK)


if __name__ == '__main__':
    main()
