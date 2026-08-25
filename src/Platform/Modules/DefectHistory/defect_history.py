# -*- coding: utf-8 -*-
"""
defect_history.py —— 不良履历引擎（底层重构 v2，单工作簿单 Sheet）

固定规则（2026-08-07 起，后续所有调整的基础）：
  1. 数据源 = 一张工作簿 `{root}/不良履历汇总.xlsx`，仅一个 Sheet「不良履历」。
  2. 第 1 行表头 8 列：日期|线别|型号|问题原因|数量|发生工程|不良图片|处理方式；
     第 2 行起数据，按第一列(日期)升序；所有查询基于第一列日期。
  3. SMT 工作表数据一律舍弃（含上传文件中的 SMT）。
  4. 日期自动标准化：任何输入格式解析为 datetime，单元格 yyyy-mm-dd。
  5. 图片锚定 G 列对应数据行（锚点 0-based row = 数据行 idx + 1）。
  6. 写操作：.lock 互斥 + 写前 .bak + .tmp 原子替换 + sidecar 失效。
  7. 分析：按 月份×线别 聚合 IPQC/QA 次数；导出 openpyxl 原生堆叠柱状图 + 数据表(表头+框线)。

命令：
  meta / query / add_single / batch_import / delete_row / export / template /
  extract_images / analysis / analysis_auto
"""
import os, sys, io, json, time, glob, datetime, shutil, zipfile, functools, re
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
try:
    from openpyxl import load_workbook, Workbook
    from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.utils import get_column_letter
    from openpyxl.chart import BarChart, Reference
    from PIL import Image as PILImage
    from _analysis_pdf import _write_analysis_pdf
except Exception as e:
    print(json.dumps({"success": False, "error": "依赖不可用: %s" % e}, ensure_ascii=False)); sys.exit(1)

BOOK_NAME = "不良履历汇总.xlsx"
SHEET_NAME = "不良履历"
HEADERS = ["日期", "线别", "型号", "问题原因", "数量", "发生工程", "不良图片", "处理方式"]
IMG_COL = 6          # G 列（0-based）
THUMB_W = 320
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
THIN = Side(style="thin", color="999999")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# 默认线别选项（H5 单条录入下拉用；数据中出现的线别会动态并入）
DEFAULT_LINES = ["波峰焊", "自动线", "线外包装", "A1-1", "A2-2", "A3-1", "A3-2", "A4-1", "A4-2"]
DEFAULT_STAGES = ["IPQC", "QA"]
DEFAULT_HANDLES = ["作业不良", "来料不良"]


# ---------------- 基础工具 ----------------

def book_path(root):
    return os.path.join(root, BOOK_NAME)


def parse_date(v):
    """日期标准化：→ datetime.date | None（无法解析）。5 位年份容错：20226.3.14→2026-03-14。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, (int, float)):
        try:
            return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(v))
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    for sep in (" ", "T"):
        if sep in s:
            s = s.split(sep)[0]
    s2 = s.replace("/", ".").replace("-", ".").replace("年", ".").replace("月", ".").replace("日", ".")
    parts = [p for p in s2.split(".") if p.strip()]
    if len(parts) < 3:
        return None
    y, mo, d = parts[0], parts[1], parts[2]
    if len(y) == 5 and y.startswith("20") and y[2] == "2":
        y = y[:2] + y[3:]
    try:
        return datetime.date(int(y), int(mo), int(d))
    except ValueError:
        return None


def norm_model(s):
    return "".join(str(s or "").split()).lower()


def guess_fmt(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:2] == b"\xff\xd8":
        return "JPEG"
    if data[:2] == b"BM":
        return "BMP"
    return "PNG"


def read_rows(ws):
    """数据行列表（row2 起、非空、跳过合计行），每行 8 个原始值。
    注意：row2 表头（日期/线别/型号…）会被当作 idx0（历史语义，row 编号与图片 data_idx 依赖它）。
    输出层（query/export）用 is_header_row 过滤表头假行。"""
    rows = []
    for r in range(2, ws.max_row + 1):
        vals = [ws.cell(r, c).value for c in range(1, 9)]
        if all(v is None or str(v).strip() == "" for v in vals):
            continue
        if any(isinstance(v, str) and str(v).strip().startswith("合计") for v in vals):
            continue
        rows.append(vals)
    return rows


def is_header_row(vals):
    """表头行识别：c1='日期' 且 c3='型号'（无论表头位于表格顶部还是底部）。"""
    try:
        return (str(vals[0] or "").strip() == "日期" and str(vals[2] or "").strip() == "型号")
    except Exception:
        return False


def sheet_images(ws):
    """[(data_idx, bytes, fmt)]：锚点 0-based row - 1 = 数据行索引（row2 起）。"""
    out = []
    for im in ws._images:
        try:
            r0 = im.anchor._from.row
        except Exception:
            continue
        if r0 < 1:
            continue
        try:
            data = im._data()
        except Exception:
            continue
        if not data:
            continue
        out.append((r0 - 1, data, guess_fmt(data)))
    out.sort(key=lambda x: x[0])
    return out


def in_cell_images(path, sheet_name):
    """从 xlsx 解析 Excel 365 in-cell image（G 列 t="e" vm 单元格 → metadata
    → rdrichvalue → richValueRel → media），兼容 openpyxl 读不到的单元格内嵌图片。

    返回 [(data_idx, bytes, fmt)]，与 sheet_images 同语义
    （data_idx = xlsx 1-based 行 - 2，因数据从 row2 起、表头 row2 计 idx0）。
    任何一步解析失败返回 []（调用方回退浮动图片）。
    """
    out = []
    try:
        import posixpath
        import zipfile
        z = zipfile.ZipFile(path)
        names = z.namelist()
        # sheet 名 → worksheet 文件
        wbxml = z.read("xl/workbook.xml").decode("utf-8")
        m = re.search(r'<sheet[^>]*name="%s"[^>]*r:id="rId(\d+)"' % re.escape(sheet_name), wbxml)
        if not m:
            z.close()
            return []
        rid = m.group(1)
        wbrels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        m2 = re.search(r'<Relationship[^>]*Id="rId%s"[^>]*>' % rid, wbrels)
        if not m2:
            z.close()
            return []
        m2t = re.search(r'Target="([^"]+)"', m2.group(0))
        if not m2t:
            z.close()
            return []
        tgt = m2t.group(1)
        sheet_file = tgt[1:] if tgt.startswith("/") else posixpath.normpath("xl/" + tgt)
        if sheet_file not in names:
            z.close()
            return []
        sheet_xml = z.read(sheet_file).decode("utf-8")
        cells = re.findall(r'<c r="G(\d+)"[^>]*t="e" vm="(\d+)"[^>]*>', sheet_xml)
        if not cells:
            z.close()
            return []
        # valueMetadata bk → rc v（rdrichvalue 索引）
        meta = z.read("xl/metadata.xml").decode("utf-8")
        rc_vals = [int(v) for v in re.findall(r'<rc t="1" v="(\d+)"', meta)]
        rv = z.read("xl/richData/rdrichvalue.xml").decode("utf-8")
        rv_rels = [int(v1) for v1 in re.findall(r'<rv s="\d+"><v>(\d+)</v>', rv)]
        rvr = z.read("xl/richData/richValueRel.xml").decode("utf-8")
        rel_rids = re.findall(r'<rel r:id="rId(\d+)"', rvr)
        relrels = z.read("xl/richData/_rels/richValueRel.xml.rels").decode("utf-8")
        rel_map = dict(re.findall(r'<Relationship Id="rId(\d+)"[^>]*Target="([^"]+)"', relrels))
        for g_row_s, vm_s in cells:
            g_row = int(g_row_s)
            vm = int(vm_s)
            rv_idx = rc_vals[vm - 1] if vm - 1 < len(rc_vals) else -1
            if rv_idx < 0 or rv_idx >= len(rv_rels):
                continue
            rel_key = str(rv_rels[rv_idx] + 1)
            if rel_key not in rel_map:
                continue
            media_path = posixpath.normpath("xl/richData/" + rel_map[rel_key])
            if media_path not in names:
                continue
            bdata = z.read(media_path)
            out.append((g_row - 2, bdata, guess_fmt(bdata)))
        z.close()
    except Exception as e:
        print("  [in_cell_images] 解析失败:", e)
        return []
    out.sort(key=lambda x: x[0])
    return out


# ---------------- Excel 365 in-cell image 注入（写操作后重建） ----------------

def _inject_in_cell_xml(draw_from, entries):
    """sheet XML：G 列有图行单元格 → t="e" vm="N"（保留 s 边框样式），vm 逐图递增。"""
    for vm, (row_1b, _) in enumerate(entries, start=1):
        r = row_1b
        pat = re.compile(r'<c r="G%d"[^>]*?(?:\/>|>.*?<\/c>)' % r, re.S)
        m = pat.search(draw_from)
        if not m:
            raise RuntimeError("sheet XML 中未找到 G%d 单元格" % r)
        old = m.group(0)
        s_attr = re.search(r's="(\d+)"', old)
        s = (' s="%s"' % s_attr.group(1)) if s_attr else ""
        new = '<c r="G%d"%s t="e" vm="%d"><v>#VALUE!</v></c>' % (r, s, vm)
        draw_from = draw_from.replace(old, new, 1)
    return draw_from


def inject_in_cell(xlsx_path, cell_imgs):
    """把 [(xlsx_1based_row, image_bytes), ...] 注入为 G 列 in-cell image。

    openpyxl 保存会丢弃 richData 部件，写操作后需用本函数重建。
    同一 xlsx 行多图（如 H5 单条上传 2 张图）→ 无损横向拼接为一张（防覆盖丢图）。
    图片统一无损转 PNG（不重采样），逐图 vm 1..N 递增，metadata/richvalue/rel/media 全链对齐。
    """
    # 同行多图聚合拼接
    by_row = {}
    for row_1b, bdata in cell_imgs:
        by_row.setdefault(row_1b, []).append(bdata)
    merged = []
    for row_1b in sorted(by_row):
        imgs = by_row[row_1b]
        if len(imgs) > 1:
            merged.append((row_1b, hstack_png([to_png_bytes(b) for b in imgs])))
        else:
            merged.append((row_1b, to_png_bytes(imgs[0])))
    entries = sorted(merged, key=lambda x: x[0])
    n = len(entries)
    if n == 0:
        return 0
    tmp = xlsx_path + ".tmp.zip"
    zin = zipfile.ZipFile(xlsx_path, "r")
    zout = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "xl/worksheets/sheet1.xml":
            data = _inject_in_cell_xml(data.decode("utf-8"), entries).encode("utf-8")
        elif item.filename == "[Content_Types].xml":
            data = _in_cell_content_types(data.decode("utf-8")).encode("utf-8")
        elif item.filename == "xl/_rels/workbook.xml.rels":
            data = _in_cell_workbook_rels(data.decode("utf-8")).encode("utf-8")
        zout.writestr(item, data)
    zout.writestr("xl/metadata.xml", _in_cell_metadata_xml(n))
    zout.writestr("xl/richData/rdrichvaluestructure.xml", _in_cell_rich_struct_xml())
    zout.writestr("xl/richData/rdRichValueTypes.xml", _IN_CELL_RICH_TYPES)
    zout.writestr("xl/richData/rdrichvalue.xml", _in_cell_rich_values_xml(n))
    zout.writestr("xl/richData/richValueRel.xml", _in_cell_rich_value_rel_xml(n))
    zout.writestr("xl/richData/_rels/richValueRel.xml.rels", _in_cell_rel_rels_xml(n))
    for i, (_, bdata) in enumerate(entries, start=1):
        zout.writestr("xl/media/image%d.png" % i, bdata)
    zin.close()
    zout.close()
    shutil.move(tmp, xlsx_path)
    return n


def _in_cell_content_types(ct):
    add = []
    if "<Default Extension=\"png\"" not in ct:
        add.append('  <Default Extension="png" ContentType="image/png"/>')
    for part, cttype in [
        ("/xl/metadata.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml"),
        ("/xl/richData/rdrichvalue.xml", "application/vnd.ms-excel.rdrichvalue+xml"),
        ("/xl/richData/rdrichvaluestructure.xml", "application/vnd.ms-excel.rdrichvaluestructure+xml"),
        ("/xl/richData/rdRichValueTypes.xml", "application/vnd.ms-excel.rdrichvaluetypes+xml"),
        ("/xl/richData/richValueRel.xml", "application/vnd.ms-excel.richvaluerel+xml"),
    ]:
        if ('PartName="%s"' % part) not in ct:
            add.append('  <Override PartName="%s" ContentType="%s"/>' % (part, cttype))
    if not add:
        return ct
    return ct.replace("</Types>", "\n".join(add) + "\n</Types>")


def _in_cell_workbook_rels(rels):
    add = [
        ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/sheetMetadata", "metadata.xml"),
        ("http://schemas.microsoft.com/office/2017/06/relationships/rdRichValue", "richData/rdrichvalue.xml"),
        ("http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueStructure", "richData/rdrichvaluestructure.xml"),
        ("http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueTypes", "richData/rdRichValueTypes.xml"),
        ("http://schemas.microsoft.com/office/2022/10/relationships/richValueRel", "richData/richValueRel.xml"),
    ]
    used = [int(x) for x in re.findall(r'Id="rId(\d+)"', rels)]
    nxt = max(used) + 1 if used else 1
    lines = []
    for typ, target in add:
        lines.append('  <Relationship Id="rId%d" Type="%s" Target="%s"/>' % (nxt, typ, target))
        nxt += 1
    return rels.replace("</Relationships>", "\n".join(lines) + "\n</Relationships>")


def _in_cell_metadata_xml(n):
    future = "\n".join(
        '    <bk><extLst><ext uri="{3e2802c4-a4d2-4d8b-9148-e3be6c30e623}">'
        '<xlrd:rvb i="%d" xmlns:xlrd="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"/>'
        "</ext></extLst></bk>" % i for i in range(n))
    values = "\n".join('    <bk><rc t="1" v="%d"/></bk>' % i for i in range(n))
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
            "</metadata>" % (n, future, n, values))


def _in_cell_rich_struct_xml():
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<rvStructures xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata" count="1">\n'
            '  <s t="_localImage">\n'
            '    <k n="_rvRel:LocalImageIdentifier" t="i"/>\n'
            '    <k n="CalcOrigin" t="i"/>\n'
            "  </s>\n"
            "</rvStructures>")


_IN_CELL_RICH_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<rvTypesInfo xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata2" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'mc:Ignorable="x" xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n'
    "  <global>\n"
    "    <keyFlags>\n"
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
    "    </keyFlags>\n"
    "  </global>\n"
    "</rvTypesInfo>")


def _in_cell_rich_values_xml(n):
    body = "\n".join('  <rv s="0"><v>%d</v><v>5</v></rv>' % i for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<rvData xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata" count="%d">\n'
            "%s\n</rvData>" % (n, body))


def _in_cell_rich_value_rel_xml(n):
    rels = "\n".join('  <rel r:id="rId%d"/>' % (i + 1) for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<richValueRels xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
            "%s\n</richValueRels>" % rels)


def _in_cell_rel_rels_xml(n):
    lines = "\n".join(
        '  <Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image%d.png"/>'
        % (i + 1, i + 1) for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            "%s\n</Relationships>" % lines)


def to_png_bytes(bdata):
    """无损转 PNG（不重采样，保留原始像素）"""
    try:
        im = PILImage.open(io.BytesIO(bdata))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if im.mode == "P" and "transparency" in im.info else "RGB")
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=False)
        return buf.getvalue()
    except Exception:
        return bdata


def hstack_png(imgs_bdata, gap=8):
    """一行多图：横向无损拼接成一张 PNG（统一最高高度、白底、保留原始像素）"""
    try:
        pil_imgs = []
        for b in imgs_bdata:
            im = PILImage.open(io.BytesIO(b))
            if im.mode != "RGB":
                im = im.convert("RGB")
            pil_imgs.append(im)
        h = max(im.size[1] for im in pil_imgs)
        w_total = sum(im.size[0] for im in pil_imgs) + gap * (len(pil_imgs) - 1)
        canvas = PILImage.new("RGB", (w_total, h), (255, 255, 255))
        x = 0
        for im in pil_imgs:
            canvas.paste(im, (x, 0))
            x += im.size[0] + gap
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=False)
        return buf.getvalue()
    except Exception:
        return to_png_bytes(imgs_bdata[0]) if imgs_bdata else b""


G_COL_PX = 115.0        # G 列近似像素宽（16字符×7px+5px），与 merge_in_cell 一致
ROW_PAD_PT = 3.0        # 行高余量，防止图片被裁切


def img_disp_h_pt(w, h):
    """in-cell 图片在 G 列内显示的适配行高（pt）：显示宽≈G列宽，显示高按纵横比。与 merge_in_cell.img_disp_h_pt 公式一致。"""
    try:
        h_px = G_COL_PX * h / w
        return max(20.0, h_px * 0.75) + ROW_PAD_PT
    except Exception:
        return 20.0


def _resize_if_large(bdata, max_dim=1600):
    """上传图片宽/高 > max_dim → 按纵横比等比缩放至 max_dim 内（LANCZOS 高质量重采样）。
    用于批量上传大图时控制 xlsx 体积；H5 端仍可查看缩放版（足够清晰，可双击/放大查看）。
    解析失败返回原字节。"""
    try:
        im = PILImage.open(io.BytesIO(bdata))
        w, h = im.size
        if max(w, h) <= max_dim:
            return bdata
        if w >= h:
            nw, nh = max_dim, max(1, int(h * max_dim / w))
        else:
            nw, nh = max(1, int(w * max_dim / h)), max_dim
        im = im.resize((nw, nh), PILImage.LANCZOS)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if im.mode == "P" and "transparency" in im.info else "RGB")
        buf = io.BytesIO()
        im.save(buf, im.format or "PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return bdata


def rebuild_images(ws, imgs):
    """清空并重嵌图片到 G 列对应数据行（xlsx 1-based row = data_idx + 2）。"""
    ws._images = []
    for d, bdata, fmt in imgs:
        try:
            img = XlImage(io.BytesIO(bdata))
            if img.width > 220:
                ratio = 220.0 / img.width
                img.width = int(img.width * ratio)
                img.height = int(img.height * ratio)
            ws.add_image(img, "G%d" % (d + 2))
        except Exception:
            pass


def thumb_bytes(bdata):
    """生成 320px JPEG 缩略图字节；失败返回原字节。"""
    try:
        im = PILImage.open(io.BytesIO(bdata))
        im = im.convert("RGB")
        w, h = im.size
        if w > THUMB_W:
            im = im.resize((THUMB_W, max(1, int(h * THUMB_W / w))), PILImage.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        return buf.getvalue()
    except Exception:
        return bdata


def _to_jpeg_bytes(bdata):
    """无损画质重编码为 JPEG：保留原始像素，仅打包为更适合 xlsx 嵌入/打印的格式。
    失败返回 (原字节, 原格式)。"""
    try:
        im = PILImage.open(io.BytesIO(bdata))
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90, optimize=True)
        return buf.getvalue(), "JPEG"
    except Exception:
        fmt = guess_fmt(bdata)
        return bdata, fmt


def apply_header_style(ws, ncols=8):
    ws.row_dimensions[1].height = 22
    for c in range(1, ncols + 1):
        cell = ws.cell(1, c, HEADERS[c - 1])
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    widths = [14, 10, 18, 40, 10, 10, 22, 14]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w


def set_row_border(ws, r, ncols=8):
    for c in range(1, ncols + 1):
        ws.cell(r, c).border = BORDER


# ---------------- 锁 / 备份 / 原子替换 ----------------

def acquire_lock(path):
    lock_path = path + ".lock"
    for _ in range(25):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 120:
                    try:
                        os.remove(lock_path)
                    except OSError:
                        os.replace(lock_path, lock_path + ".release")
                    continue
            except OSError:
                pass
            time.sleep(0.2)
    raise RuntimeError("文件被占用，无法获取写锁: " + path)


def release_lock(lock):
    if not lock:
        return
    try:
        os.remove(lock)
        return
    except OSError:
        pass
    try:
        os.replace(lock, lock + ".release")
    except OSError:
        pass


def safe_replace(path):
    """原子替换 .tmp → path。优先 os.replace + 短暂 sleep（防 EDR/Defender 实时扫描 .tmp 短暂持锁），
    失败 fallback 用 shutil.move（Windows 上走 ReplaceFileW，比 os.replace 更宽容）。
    目标被真正独占锁时清理残留 tmp 并抛清晰错误。"""
    import time as _t
    tmp = path + ".tmp"
    if not os.path.exists(tmp):
        return
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    # 优先 os.replace（原子）；短暂 sleep 给 EDR/Defender 释放 .tmp 句柄
    last_err = None
    for attempt in range(2):
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            last_err = e
            if attempt == 0:
                _t.sleep(0.5)
                continue
            # fallback：shutil.move（Windows ReplaceFileW，能替换被 EDR 短暂持有的文件）
            try:
                shutil.move(tmp, path)
                return
            except Exception:
                pass
            break
    # 全部失败 → 清理残留 tmp + 抛清晰错误
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    hint = "被其他程序（WPS/Excel/预览）打开占用，请关闭后重试" if isinstance(last_err, PermissionError) else str(last_err)
    raise ValueError(f"无法保存数据源文件：{hint}。") from last_err


def sidecar_dir(root):
    return os.path.join(root, "_images", os.path.splitext(BOOK_NAME)[0])


def invalidate_sidecar(root):
    d = sidecar_dir(root)
    stamp = os.path.join(d, ".stamp")
    try:
        os.makedirs(d, exist_ok=True)
        # ⚠️ 失效=删除 stamp 文件（不可用 utime(0,0)：C# 快路径按 mtime 判断，mtime=1970 会导致
        # 每次图片请求都判定 sidecar 过期 → 反复触发 Python 重建 → 并发竞争报 Permission denied）
        if os.path.exists(stamp):
            os.remove(stamp)
    except OSError:
        pass


def write_book(root, mutate_fn):
    """load → mutate → 保存 .tmp → wb.close（释放 .tmp 句柄！）→ 备份 → os.replace → 失效 sidecar。

    关键修复：wb.save 后必须 wb.close()，否则 wb 内部句柄持有 .tmp → Windows 上 os.replace 失败
    （WinError 5 拒绝访问；即使目标 xlsx 本身可 rename 也无济于事——rename 的源 .tmp 被 wb 占用）。
    """
    path = book_path(root)
    wb = load_workbook(path)
    mutate_fn(wb)
    wb.save(path + ".tmp")
    wb.close()   # 释放 .tmp 句柄，让 os.replace 的源文件不被任何句柄持有
    safe_replace(path)
    invalidate_sidecar(root)
    restore = getattr(wb, "_in_cell_restore", None)
    if restore:
        inject_in_cell(path, restore)
        invalidate_sidecar(root)


def write_book_locked(root, mutate_fn):
    path = book_path(root)
    lock = None
    try:
        lock = acquire_lock(path)
        write_book(root, mutate_fn)
    finally:
        release_lock(lock)


# ---------------- 数据访问 ----------------

def collect_rows(root, model=None, month=None):
    """从汇总工作簿选行（model 模糊匹配；month=YYYY-MM 按日期列过滤）。返回 [(data_idx, vals)]。"""
    path = book_path(root)
    if not os.path.exists(path):
        return []
    wb = load_workbook(path, data_only=True)
    ws = wb[SHEET_NAME]
    nm = norm_model(model) if model else None
    out = []
    for idx, vals in enumerate(read_rows(ws)):
        if nm:
            if nm not in norm_model(vals[2]):
                continue
        if month:
            d = parse_date(vals[0])
            if d is None or d.strftime("%Y-%m") != month:
                continue
        out.append((idx, vals))
    wb.close()
    return out


def month_stats(root):
    """{month: count} 基于第一列日期聚合。"""
    path = book_path(root)
    stats = {}
    if not os.path.exists(path):
        return stats
    wb = load_workbook(path, data_only=True)
    ws = wb[SHEET_NAME]
    for vals in read_rows(ws):
        d = parse_date(vals[0])
        if d is None:
            continue
        m = d.strftime("%Y-%m")
        stats[m] = stats.get(m, 0) + 1
    wb.close()
    return stats


def row_has_images(root, ws, data_idx):
    if ws._images:
        return sum(1 for im in ws._images if im.anchor._from.row - 1 == data_idx)
    return sum(1 for d, b, f in _in_cell_cached(_in_cell_key(root)) if d == data_idx)


def img_count_for_row(root, ws, data_idx):
    if ws._images:
        return sum(1 for im in ws._images if im.anchor._from.row - 1 == data_idx)
    return sum(1 for d, b, f in _in_cell_cached(_in_cell_key(root)) if d == data_idx)


def _in_cell_key(root):
    """lru_cache key：(book_path, mtime)，写操作后 mtime 变化自动失效"""
    p = book_path(root)
    return (p, os.path.getmtime(p))


@functools.lru_cache(maxsize=4)
def _in_cell_cached(key):
    """key=(book_path, mtime) → [(data_idx, bytes, fmt)]（in-cell 解析缓存）"""
    path, _mtime = key
    return in_cell_images(path, SHEET_NAME)


def load_imgs_for_write(root, ws):
    """写操作读图：浮动优先，in-cell 兜底。返回 (imgs, is_in_cell)"""
    imgs = sheet_images(ws)
    if imgs:
        return imgs, False
    ic = in_cell_images(book_path(root), SHEET_NAME)
    return ic, bool(ic)


def schedule_in_cell_restore(wb, kept):
    """kept: [(data_idx, bytes, fmt)] → 挂到 wb，保存后由 write_book 注入 in-cell（xlsx 行 = idx + 2）。
    同时按图片纵横比适配行高（与 merge_in_cell 一致），避免图片溢出盖住其它行。"""
    ws = wb[SHEET_NAME]
    for d, b, f in kept:
        if b:
            try:
                pw, ph = PILImage.open(io.BytesIO(b)).size
                ws.row_dimensions[d + 2].height = img_disp_h_pt(pw, ph)
            except Exception:
                pass
    wb._in_cell_restore = [(d + 2, b) for d, b, f in kept if b]


def apply_imgs(wb, ws, kept, is_in_cell):
    """重嵌：浮动版 rebuild_images；in-cell 版挂载保存后注入"""
    if is_in_cell:
        schedule_in_cell_restore(wb, kept)
    else:
        rebuild_images(ws, kept)


# ---------------- 命令：meta ----------------
_meta_cache = {}            # {abs_path: mtime}
_meta_cache_result = {}     # {abs_path: result}


def cmd_meta(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    path = book_path(root)
    if not os.path.exists(path):
        return {"ok": True, "file": BOOK_NAME, "sheet": SHEET_NAME, "totalRows": 0,
                "months": [], "minDate": None, "maxDate": None}
    # 进程内缓存（按 book mtime 失效）：手机端下拉选项高频请求，避免每次全表扫描
    ckey = os.path.abspath(path)
    cmt = os.path.getmtime(path)
    if _meta_cache.get(ckey) == cmt:
        return _meta_cache_result.get(ckey)
    wb = load_workbook(path, data_only=True)
    ws = wb[SHEET_NAME]
    rows = read_rows(ws)
    dates = [parse_date(v[0]) for v in rows]
    dates = [d for d in dates if d is not None]
    stats = {}
    for d in dates:
        m = d.strftime("%Y-%m")
        stats[m] = stats.get(m, 0) + 1
    # 线别 / 发生工程 / 处理方式 选项：数据中去重（新录入的拉线/工程/方式自动出现在下拉列表）；跳过表头假行
    lines = set(DEFAULT_LINES)
    stages = set(DEFAULT_STAGES)
    handles = set(DEFAULT_HANDLES)
    for v in rows:
        if is_header_row(v):
            continue
        ln = str(v[1] or "").strip()
        if ln:
            lines.add(ln)
        st = str(v[5] or "").strip()
        if st:
            stages.add(st)
        hd = str(v[7] or "").strip()
        if hd:
            handles.add(hd)
    wb.close()
    result = {"ok": True, "file": BOOK_NAME, "sheet": SHEET_NAME, "totalRows": len(rows),
              "months": [{"month": k, "count": v} for k, v in sorted(stats.items())],
              "lines": sorted(lines), "stages": sorted(stages), "handles": sorted(handles),
              "minDate": (min(dates).strftime("%Y-%m-%d") if dates else None),
              "maxDate": (max(dates).strftime("%Y-%m-%d") if dates else None)}
    _meta_cache[ckey] = cmt
    _meta_cache_result[ckey] = result
    return result


# ---------------- 命令：query ----------------

def cmd_query(args):
    root, model, month = args.get("root"), (args.get("model") or "").strip(), (args.get("month") or "").strip()
    if not root:
        raise ValueError("缺少 --root")
    path = book_path(root)
    if not os.path.exists(path):
        return {"ok": True, "total": 0, "rows": []}
    wb = load_workbook(path, data_only=True)
    ws = wb[SHEET_NAME]
    picked = collect_rows(root, model or None, month or None)
    rows_out = []
    for idx, vals in picked:
        if is_header_row(vals):
            continue  # 过滤表头假行（历史 row2 表头被 read_rows 当 idx0）
        d = parse_date(vals[0])
        rows_out.append({
            "row": idx,
            "date": d.strftime("%Y-%m-%d") if d else str(vals[0] or ""),
            "line": str(vals[1] or ""),
            "model": str(vals[2] or ""),
            "reason": str(vals[3] or ""),
            "qty": str(vals[4] or ""),
            "stage": str(vals[5] or ""),
            "handle": str(vals[7] or ""),
            "hasImage": img_count_for_row(root, ws, idx) > 0,
            "imgCount": img_count_for_row(root, ws, idx),
        })
    wb.close()
    return {"ok": True, "total": len(rows_out), "rows": rows_out}


# ---------------- 命令：add_single ----------------

def cmd_add_single(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    dval = parse_date(args.get("date"))
    if dval is None:
        raise ValueError("日期无法解析: %s" % (args.get("date") or ""))
    model = (args.get("model") or "").strip()
    if not model:
        raise ValueError("型号不能为空")
    image_files = [p for p in (args.get("images") or "").split(",") if p.strip()]
    new_imgs = []
    for p in image_files:
        try:
            with open(p, "rb") as f:
                bdata = f.read()
            if bdata:
                new_imgs.append(bdata)
        except OSError:
            pass

    def mutate(wb):
        ws = wb[SHEET_NAME]
        rows = read_rows(ws)
        imgs, is_in_cell = load_imgs_for_write(root, ws)
        new_row = [dval, (args.get("line") or "").strip(), model,
                   (args.get("reason") or "").strip(), (args.get("qty") or "").strip(),
                   (args.get("stage") or "").strip(), "", (args.get("handle") or "").strip()]
        pos = 0
        while pos < len(rows):
            dd = parse_date(rows[pos][0])
            if dd is not None and dd > dval:
                break
            pos += 1
        rows.insert(pos, new_row)
        # 重映射既有图片（>= pos 的 +1）
        kept = []
        for d, bdata, fmt in imgs:
            kept.append((d + 1 if d >= pos else d, bdata, fmt))
        for k, bdata in enumerate(new_imgs):
            kept.append((pos, bdata, guess_fmt(bdata)))
        # 重写数据区
        _rewrite(ws, rows)
        apply_imgs(wb, ws, kept, is_in_cell)

    write_book_locked(root, mutate)
    return {"ok": True, "row": pos if False else None, "inserted": 1, "images": len(new_imgs),
            "file": BOOK_NAME}


def _rewrite(ws, rows):
    """清空 row2 起旧数据并重写 rows（保留表头 row1 标题；rows[0] 为表头行）。
    数据行全格 thin 框线；表头行（row2）恢复浅蓝底+加粗+居中（与合并表一致）。"""
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)
    for i, rec in enumerate(rows):
        xr = 2 + i
        is_date = isinstance(rec[0], (datetime.date, datetime.datetime))
        ws.cell(xr, 1, rec[0] if is_date else (rec[0] if rec[0] else None))
        if is_date:
            ws.cell(xr, 1).number_format = "yyyy-mm-dd"
        for c in range(2, 9):
            v = rec[c - 1] if c - 1 < len(rec) else ""
            ws.cell(xr, c, v if v not in (None, "") else None)
        set_row_border(ws, xr)
        if xr == 2:
            # 表头行样式（浅蓝底 + 加粗 + 居中 + 行高）——合并表 row1=标题、row2=表头
            for c in range(1, 9):
                cell = ws.cell(xr, c)
                cell.font = Font(bold=True)
                cell.fill = HEAD_FILL
                cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[xr].height = 20


# ---------------- 命令：batch_import ----------------

def locate_header(nrows, ncols, getter):
    for r in range(min(nrows, 6)):
        cols = {}
        for c in range(ncols):
            name = str(getter(r, c)).strip()
            if name in HEADERS and name not in cols:
                cols[name] = c
        if "日期" in cols and "型号" in cols:
            return r, cols
    return None, None


def _extract_xls_imgs(upload):
    """解析 .xls 全部工作表的图片 → {sheet_index: {xlrd 行号(0-based): [bdata, ...]}}。
    图片归属用 **dy1 权威行号**（WPS ClientAnchor rd[6:8] = 真实视觉行号），
    与合并表 merge_in_cell 同一套根治方案（2026-08-10 验证 21/21 命中）——
    不再丢图（旧行为 xls 上传图片一律舍弃）。"""
    result = {}
    try:
        import tempfile
        import xls_img_extract as X
        import merge_in_cell as MC
        outdir = tempfile.mkdtemp(prefix="xls_up_")
        try:
            res = X.analyze_file(upload, outdir=outdir)
            if not res.get("success"):
                return result
            for si, s in enumerate(res.get("sheets", [])):
                imgs = s.get("images", [])
                if not imgs:
                    continue
                anchors = MC.get_anchors(upload, si)   # dy1 列表，顺序=图片提取顺序
                row_map = {}
                for k, im in enumerate(imgs):
                    dy1 = anchors[k] if k < len(anchors) else None
                    if dy1 is None:
                        continue
                    try:
                        with open(im["file"], "rb") as f:
                            b = f.read()
                        row_map.setdefault(dy1, []).append(_resize_if_large(b))
                    except Exception:
                        continue
                if row_map:
                    result[si] = row_map
        finally:
            try:
                shutil.rmtree(outdir, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass
    return result


def parse_upload(upload, ext):
    """解析上传文件（.xlsx/.xls）。SMT 工作表舍弃。返回 (records, errors, dropped_smt)。"""
    records, errors, dropped_smt = [], [], 0
    if ext == ".xlsx":
        wb = load_workbook(upload, data_only=True)
        for ws in wb.worksheets:
            if ws.title == "SMT":
                for r in range(2, ws.max_row + 1):
                    if any(ws.cell(r, c).value not in (None, "") for c in range(1, 9)):
                        dropped_smt += 1
                continue
            hrow, cols = locate_header(ws.max_row, ws.max_column, lambda r, c: ws.cell(r + 1, c + 1).value)
            if hrow is None:
                errors.append({"sheet": ws.title, "row": 0, "error": "未找到含「日期/型号」的表头行"})
                continue
            imgs_by_row = {}
            for im in ws._images:
                try:
                    imgs_by_row.setdefault(im.anchor._from.row, []).append(_resize_if_large(im._data()))
                except Exception:
                    pass
            if not imgs_by_row:
                # 上传文件若是 Excel 365 in-cell image（单元格内嵌），openpyxl 读不到 →
                # 走标准链解析（vm→metadata→rdrichvalue→richValueRel→media）
                # in_cell data_idx = xlsx1based-2；rec 的 r(0-based) = xlsx1based-1 → r = d+1
                for d, bdata, fmt in in_cell_images(upload, ws.title):
                    imgs_by_row.setdefault(d + 1, []).append(_resize_if_large(bdata))
            for r in range(hrow + 1, ws.max_row):
                vals = [ws.cell(r + 1, c + 1).value for c in range(8)]
                if all(v is None or str(v).strip() == "" for v in vals):
                    continue
                date_col, model_col = cols.get("日期", 0), cols.get("型号", 2)
                dval = parse_date(vals[date_col])
                model = str(vals[model_col] or "").strip()
                rec = {"sheet": ws.title, "row": r + 1,
                       "date": dval.strftime("%Y.%m.%d") if dval else str(vals[date_col] or ""),
                       "model": model, "valid": dval is not None and bool(model),
                       "_imgs": imgs_by_row.get(r, [])}
                if not rec["valid"]:
                    rec["error"] = "日期无法解析" if dval is None else "型号为空"
                    errors.append(rec)
                    continue
                rec["line"] = str(vals[cols.get("线别", 1)] or "").strip()
                rec["reason"] = str(vals[cols.get("问题原因", 3)] or "").strip()
                rec["qty"] = str(vals[cols.get("数量", 4)] or "").strip()
                rec["stage"] = str(vals[cols.get("发生工程", 5)] or "").strip()
                rec["handle"] = str(vals[cols.get("处理方式", 7)] or "").strip()
                rec["targetMonth"] = dval.strftime("%Y-%m")
                records.append(rec)
        wb.close()
    else:
        import xlrd
        wb = xlrd.open_workbook(upload)
        xls_imgs = _extract_xls_imgs(upload)   # {sheet_index: {xlrd行号: [bdata,...]}}（dy1 权威归属）
        for si, ws in enumerate(wb.sheets()):
            if ws.name == "SMT":
                dropped_smt += max(0, ws.nrows - 1)
                continue
            hrow, cols = locate_header(ws.nrows, ws.ncols, lambda r, c: ws.cell_value(r, c))
            if hrow is None:
                errors.append({"sheet": ws.name, "row": 0, "error": "未找到含「日期/型号」的表头行"})
                continue
            imgs_by_row = xls_imgs.get(si, {})
            for r in range(hrow + 1, ws.nrows):
                vals = [ws.cell_value(r, c) for c in range(min(ws.ncols, 8))]
                if all(str(v).strip() == "" for v in vals):
                    continue
                dval = parse_date(vals[cols.get("日期", 0)])
                model = str(vals[cols.get("型号", 2)] or "").strip()
                if dval is None or not model:
                    errors.append({"sheet": ws.name, "row": r + 1,
                                   "error": "日期无法解析" if dval is None else "型号为空"})
                    continue
                records.append({
                    "sheet": ws.name, "row": r + 1, "date": dval.strftime("%Y.%m.%d"), "model": model,
                    "line": str(vals[cols.get("线别", 1)] or "").strip(),
                    "reason": str(vals[cols.get("问题原因", 3)] or "").strip(),
                    "qty": str(vals[cols.get("数量", 4)] or "").strip(),
                    "stage": str(vals[cols.get("发生工程", 5)] or "").strip(),
                    "handle": str(vals[cols.get("处理方式", 7)] or "").strip(),
                    "valid": True, "_imgs": imgs_by_row.get(r, []),
                    "targetMonth": dval.strftime("%Y-%m"),
                })
    return records, errors, dropped_smt, sum(len(r.get("_imgs", [])) for r in records)


def cmd_batch_import(args):
    root = args.get("root")
    upload = args.get("upload")
    if not root or not upload:
        raise ValueError("缺少 --root 或 --upload")
    ext = os.path.splitext(upload)[1].lower()
    if ext not in (".xlsx", ".xls"):
        raise ValueError("仅支持 .xlsx/.xls 上传文件")
    records, errors, dropped_smt, img_total = parse_upload(upload, ext)
    preview = args.get("preview") == "1" or args.get("preview") == "true"
    if preview:
        return {"ok": True, "preview": True, "total": len(records), "valid": sum(1 for r in records if r.get("valid", True)),
                "droppedSmt": dropped_smt, "imgTotal": img_total, "errors": errors,
                "rows": [{"sheet": r["sheet"], "row": r["row"], "date": r["date"], "model": r["model"],
                          "line": r.get("line", ""), "stage": r.get("stage", ""), "valid": r.get("valid", True),
                          "error": r.get("error", "")} for r in records]}
    imported = 0
    to_import = [r for r in records if r.get("valid", True)]

    def mutate(wb):
        nonlocal imported
        ws = wb[SHEET_NAME]
        rows = read_rows(ws)
        imgs, is_in_cell = load_imgs_for_write(root, ws)
        # ★ 表头行识别（c1='日期' c3='型号'），无论当前位于表格哪个位置（可能在底部），恒置首位不参与排序
        header_rec = None
        for rec in rows:
            if str(rec[0] or "").strip() == "日期" and str(rec[2] or "").strip() == "型号":
                header_rec = rec
                break
        new_rows = []
        for r in to_import:
            dval = parse_date(r["date"])
            new_rows.append([dval, r.get("line", ""), r["model"], r.get("reason", ""),
                             r.get("qty", ""), r.get("stage", ""), "", r.get("handle", "")])
        # 合并 + 排序（None 日期排最后；表头行排除在外，最后恒放首位）
        merged = []
        for rec in rows:
            if rec is header_rec:
                continue
            merged.append((parse_date(rec[0]), rec, True))
        for nr in new_rows:
            merged.append((parse_date(nr[0]), nr, False))
        merged.sort(key=lambda t: (t[0] is None, t[0] if t[0] else datetime.date.max))
        if header_rec is not None:
            merged.insert(0, (None, header_rec, True))
        # 单次遍历重建图片：旧行按原索引取图，新行按顺序取图
        old_pos = {id(rec): i for i, rec in enumerate(rows)}
        imgs_by_old = {}
        for d2, bdata, fmt in imgs:
            imgs_by_old.setdefault(d2, []).append((bdata, fmt))
        kept = []
        qi = 0
        for ni, (d, rec, is_old) in enumerate(merged):
            if is_old:
                oi = old_pos[id(rec)]
                for bdata, fmt in imgs_by_old.get(oi, []):
                    kept.append((ni, bdata, fmt))
            else:
                for bdata in to_import[qi].get("_imgs", []):
                    kept.append((ni, bdata, guess_fmt(bdata)))
                qi += 1
        ordered = [rec for d, rec, is_old in merged]
        _rewrite(ws, ordered)
        apply_imgs(wb, ws, kept, is_in_cell)
        imported = len(to_import)

    write_book_locked(root, mutate)
    img_imported = sum(len(r.get("_imgs", [])) for r in to_import)
    return {"ok": True, "imported": imported, "skipped": len(errors), "droppedSmt": dropped_smt,
            "imgTotal": img_total, "imgImported": img_imported, "errors": errors}


# ---------------- 命令：delete_row ----------------

def cmd_delete_row(args):
    root = args.get("root")
    row = int(args.get("row") or -1)
    if not root or row < 0:
        raise ValueError("缺少 --root 或非法的 --row")

    def mutate(wb):
        ws = wb[SHEET_NAME]
        rows = read_rows(ws)
        if row >= len(rows):
            raise ValueError("行号越界: %d（共 %d 行）" % (row, len(rows)))
        imgs, is_in_cell = load_imgs_for_write(root, ws)
        kept = []
        for d, bdata, fmt in imgs:
            if d < row:
                kept.append((d, bdata, fmt))
            elif d > row:
                kept.append((d - 1, bdata, fmt))
        rows.pop(row)
        _rewrite(ws, rows)
        apply_imgs(wb, ws, kept, is_in_cell)

    write_book_locked(root, mutate)
    return {"ok": True, "row": row, "file": BOOK_NAME}


def cmd_batch_delete(args):
    """批量删除多行：一次加载 + 一次写入（避免逐条删除 N 次全文件重写，耗时从 O(N×2.5s) → O(1×3s)）。
    行号集合内部按降序处理 + 图片索引按删除位移批量修正（与 cmd_delete_row 单条语义一致，杜绝误删/错位）。"""
    root = args.get("root")
    raw = args.get("rows")
    if not root:
        raise ValueError("缺少 --root")
    if raw is None:
        raise ValueError("缺少 --rows")
    try:
        rows = sorted({int(r) for r in (json.loads(raw) if isinstance(raw, str) else list(raw))
                       if isinstance(r, (int, float)) or str(r).lstrip('-').isdigit()})
    except Exception:
        raise ValueError("rows 参数不合法")
    if not rows:
        return {"ok": True, "deleted": 0, "file": BOOK_NAME}
    import bisect

    def mutate(wb):
        ws = wb[SHEET_NAME]
        all_rows = read_rows(ws)
        total = len(all_rows)
        if max(rows) >= total:
            raise ValueError("行号越界: %d（共 %d 行）" % (max(rows), total))
        imgs, is_in_cell = load_imgs_for_write(root, ws)
        dels = set(rows)
        # 数据行：保留非删除行（顺序不变）
        kept_rows = [r for i, r in enumerate(all_rows) if i not in dels]
        # 图片索引位移：新索引 = d - (删除行中 < d 的数量)；被删行图片随行丢弃
        dl = sorted(dels)
        kept = []
        for d, bdata, fmt in imgs:
            if d in dels:
                continue
            shift = bisect.bisect_left(dl, d)
            kept.append((d - shift, bdata, fmt))
        _rewrite(ws, kept_rows)
        apply_imgs(wb, ws, kept, is_in_cell)

    write_book_locked(root, mutate)
    return {"ok": True, "deleted": len(rows), "file": BOOK_NAME}


# ---------------- 命令：export ----------------

def cmd_export(args):
    root = args.get("root")
    out = args.get("out")
    if not root or not out:
        raise ValueError("缺少 --root 或 --out")
    model = (args.get("model") or "").strip() or None
    month = (args.get("month") or "").strip() or None
    full = args.get("full") == "1" or args.get("full") == "true"
    path = book_path(root)
    if not os.path.exists(path):
        raise ValueError("数据源不存在: " + path)
    wb_src = load_workbook(path)
    ws = wb_src[SHEET_NAME]
    rows_all = read_rows(ws)
    imgs = sheet_images(ws) or in_cell_images(path, SHEET_NAME)
    if full:
        picked = [(idx, vals) for idx, vals in enumerate(rows_all) if not is_header_row(vals)]
    else:
        picked = [(idx, vals) for idx, vals in enumerate(rows_all)
                  if not is_header_row(vals)
                  and (not model or norm_model(model) in norm_model(vals[2]))
                  and (not month or (parse_date(vals[0]) and parse_date(vals[0]).strftime("%Y-%m") == month))]
    img_map = {}
    for d, bdata, fmt in imgs:
        img_map.setdefault(d, []).append(bdata)

    wb = Workbook()
    out_ws = wb.active
    out_ws.title = SHEET_NAME
    apply_header_style(out_ws)
    for c, w in enumerate([14, 10, 18, 44, 12, 12, 20, 14], start=1):
        out_ws.column_dimensions[get_column_letter(c)].width = w
    EXPORT_G_PX = 140.0  # 导出 G 列图片显示宽上限（≈20 字符）
    for i, (idx, vals) in enumerate(picked):
        # idx 是 read_rows 累加的数据行索引；i 是 picked 子集索引（条件导出时两者不同步）
        # 图片必须锚到 导出 xlsx 的行 xr（xr = 2 + i），而不是 idx + 2
        # —— 之前用 `G{i+2}` 是 bug（条件导出时锚到 picked 子集行）；用 `G{idx+2}` 更错（锚到源 xlsx 行，导出 xlsx 没那行）
        xr = 2 + i
        d = parse_date(vals[0])
        out_ws.cell(xr, 1, d if d else (vals[0] if vals[0] is not None else None))
        if d:
            out_ws.cell(xr, 1).number_format = "yyyy-mm-dd"
        for c in range(2, 9):
            v = vals[c - 1] if c - 1 < len(vals) else ""
            out_ws.cell(xr, c, v if v not in (None, "") else None)
        set_row_border(out_ws, xr)
        # 数据单元格水平+垂直居中 + 超长自动换行（表头已由 apply_header_style 居中+换行）
        for c in range(1, 9):
            out_ws.cell(xr, c).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        row_imgs = img_map.get(idx, [])
        if row_imgs:
            # 行高适配：图片在 G 列宽内按纵横比显示，行高=显示高，避免溢出盖住其它行
            try:
                pw, ph = PILImage.open(io.BytesIO(row_imgs[0])).size
                disp_w = min(EXPORT_G_PX, pw)
                out_ws.row_dimensions[xr].height = disp_w * ph / pw * 0.75 + 3
            except Exception:
                pass
        for k, bdata in enumerate(row_imgs):
            try:
                # 格式转换：PNG/BMP → JPEG（无画质损失：保留原始像素，仅无损编码重排；适合打印/减小体积）
                im_bytes, im_fmt = _to_jpeg_bytes(bdata)
                im = XlImage(io.BytesIO(im_bytes))
                if im.width > EXPORT_G_PX:
                    ratio = EXPORT_G_PX / im.width
                    im.width = int(im.width * ratio)
                    im.height = int(im.height * ratio)
                out_ws.add_image(im, "G%d" % xr)
                if k > 0:
                    im.anchor._from.colOff = k * 40 * 9525
            except Exception:
                pass
    wb_src.close()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.save(out)
    wb.close()
    return {"ok": True, "out": out, "rows": len(picked), "images": sum(len(v) for v in img_map.values())}


# ---------------- 命令：template ----------------

def cmd_template(args):
    root, out = args.get("root"), args.get("out")
    if not out:
        raise ValueError("缺少 --out")
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    apply_header_style(ws)
    example = [datetime.date.today(), "A1-1", "示例型号", "示例问题原因（可留空）", "1台", "IPQC", "", "作业不良"]
    for c, v in enumerate(example, start=1):
        ws.cell(2, c, v)
    ws.cell(2, 1).number_format = "yyyy-mm-dd"
    set_row_border(ws, 2)
    note = "填写说明：日期格式支持 2026.8.7 / 2026-8-7 / 2026/8/7；发生工程填 IPQC 或 QA；处理方式如 作业不良/来料不良；「不良图片」列可插入图片（.xlsx 上传时一并入库）；若文件含 SMT 工作表，上传时将自动舍弃 SMT 数据；示例行可删除。"
    ws.cell(4, 1, note).font = Font(size=10, color="888888")
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=8)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.save(out)
    wb.close()
    return {"ok": True, "out": out}


# ---------------- 命令：extract_images ----------------

def list_sidecar(out_dir):
    """从 sidecar 目录列出每行图片路径（fresh 分支用，不重读工作簿）。"""
    sheets = []
    sdir = os.path.join(out_dir, SHEET_NAME)
    if os.path.isdir(sdir):
        by_row = {}
        for fn in sorted(os.listdir(sdir)):
            m = fn[:-4].split("_")
            if len(m) >= 3 and m[0] == "row":
                try:
                    d = int(m[1])
                except ValueError:
                    continue
                is_thumb = fn.endswith("_thumb." + fn.rsplit(".", 1)[-1]) and "_thumb." in fn
                key = (d, is_thumb)
                by_row.setdefault(key, []).append(os.path.join(sdir, fn))
        images = []
        rows = sorted({k[0] for k in by_row})
        for d in rows:
            files = sorted(by_row.get((d, False), []))
            thumbs = sorted(by_row.get((d, True), []))
            images.append({"row": d, "files": files, "thumbs": thumbs})
        sheets.append({"name": SHEET_NAME, "images": images})
    return sheets


def cmd_extract_images(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    out_override = args.get("out")
    out_dir = out_override or sidecar_dir(root)
    stamp = os.path.join(out_dir, ".stamp")
    xlsx_mtime = os.path.getmtime(book_path(root))
    fresh = os.path.exists(stamp) and os.path.getmtime(stamp) >= xlsx_mtime
    sheets = []
    if fresh:
        sheets = list_sidecar(out_dir)
        return {"ok": True, "files": [{"file": BOOK_NAME, "fresh": True, "sheets": sheets}]}
    # 重建
    os.makedirs(out_dir, exist_ok=True)
    wb = load_workbook(book_path(root))
    ws = wb[SHEET_NAME]
    sdir = os.path.join(out_dir, SHEET_NAME)
    os.makedirs(sdir, exist_ok=True)
    imgs = sheet_images(ws)
    if not imgs:
        # 源工作簿为 Excel 365 in-cell image（单元格内嵌图）时，openpyxl 读不到，
        # 走标准链解析（vm → metadata → rdrichvalue → richValueRel → media）
        imgs = in_cell_images(book_path(root), SHEET_NAME)
    file_imgs = []
    for d, bdata, fmt in imgs:
        ext = ".jpg" if fmt == "JPEG" else (".bmp" if fmt == "BMP" else ".png")
        n_at_row = len([x for x in file_imgs if x[0] == d])
        fname = "row_%d_%d%s" % (d, n_at_row, ext)
        fpath = os.path.join(sdir, fname)
        with open(fpath, "wb") as fh:
            fh.write(bdata)
        tname = "row_%d_%d_thumb.jpg" % (d, n_at_row)
        with open(os.path.join(sdir, tname), "wb") as fh:
            fh.write(thumb_bytes(bdata))
        file_imgs.append((d, fpath, os.path.join(sdir, tname)))
    wb.close()
    with open(stamp, "w", encoding="utf-8") as fh:
        fh.write("%d" % int(xlsx_mtime))
    images = []
    for d, fpath, tpath in file_imgs:
        images.append({"row": d, "files": [fpath], "thumbs": [tpath]})
    sheets.append({"name": SHEET_NAME, "images": images})
    return {"ok": True, "files": [{"file": BOOK_NAME, "fresh": False, "sheets": sheets}]}


# ---------------- 命令：analysis ----------------

# 型号配色板（按型号名排序后稳定分配；导出图表与 H5 预览共用同一顺序）
MODEL_COLORS = ["4472C4", "ED7D31", "70AD47", "FFC000", "7030A0",
                "E84C3D", "2E9BD9", "C00000", "00B0F0", "7F7F7F"]


def _analysis_data(root, month, model=None):
    """按月统计：只取发生工程为 IPQC/QA 的数据，按 拉线×型号 聚合次数。
    返回 (title, lines, models, data)；data=[{line, model, ipqc, qa, total}]，
    lines=拉线出现顺序，models=排序后的型号列表（稳定配色基准）。"""
    y, mo = month.split("-")
    m = int(mo)  # 去前导零（3 月显示为 "3" 而不是 "03"）
    path = book_path(root)
    wb = load_workbook(path, data_only=True)
    ws = wb[SHEET_NAME]
    agg = {}
    line_order, model_set = [], set()
    for vals in read_rows(ws):
        d = parse_date(vals[0])
        if d is None or d.strftime("%Y-%m") != month:
            continue
        line = str(vals[1] or "").strip() or "未填写"
        model_v = str(vals[2] or "").strip()
        stage = str(vals[5] or "").strip().upper()
        if model and norm_model(model) not in norm_model(model_v):
            continue
        if stage not in ("IPQC", "QA"):
            continue
        if line not in line_order:
            line_order.append(line)
        model_set.add(model_v)
        key = (line, model_v)
        a = agg.setdefault(key, {"IPQC": 0, "QA": 0})
        a[stage] += 1
    wb.close()
    models = sorted(model_set)
    # 按 拉线出现顺序 + 型号排序（与配色一致）
    data = []
    for line in line_order:
        for mv in models:
            key = (line, mv)
            if key not in agg:
                continue
            v = agg[key]
            data.append({"line": line, "model": mv, "ipqc": v["IPQC"], "qa": v["QA"],
                         "total": v["IPQC"] + v["QA"]})
    title = "%s年%d月品质数据" % (y, m)
    if model:
        title += "·型号：" + model
    return title, line_order, models, data


def _write_analysis_xlsx(out, month, title, lines, models, data):
    """生成分析报告 xlsx：数据表(拉线|型号|IPQC|QA|合计，表头+框线)
    + openpyxl 原生堆叠柱状图（X=拉线，每型号一段，颜色=型号）
    + 底部文本图例（每条拉线对应的型号与单数）。
    页面设置：A4 竖向，按宽度缩放，标题/表头每页重复打印。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "分析数据"
    # 标题行（主标题 + 副标题：全型号 / 型号：XXX）
    y, mo = month.split("-")
    sub = "全型号汇总" if not title.endswith("·全型号") else ""
    ws.merge_cells("A1:E1")
    c = ws.cell(1, 1, title)
    c.font = Font(bold=True, size=14)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28
    # 表头
    hdr = ["拉线", "型号", "IPQC 次数", "QA 次数", "合计"]
    for ci, h in enumerate(hdr, start=1):
        cell = ws.cell(2, ci, h)
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center")
        cell.border = BORDER
    for i, d in enumerate(data):
        r = 3 + i
        ws.cell(r, 1, d["line"]).border = BORDER
        ws.cell(r, 2, d["model"]).border = BORDER
        ws.cell(r, 3, d["ipqc"]).border = BORDER
        ws.cell(r, 4, d["qa"]).border = BORDER
        ws.cell(r, 5, d["total"]).border = BORDER
        for ci in (3, 4, 5):
            ws.cell(r, ci).alignment = Alignment(horizontal="center")
    n = len(data)
    # 底部文本图例：按拉线分组，标注每条拉线对应的型号与次数（颜色=型号配色）
    lr = 4 + n
    color_of = {mv: MODEL_COLORS[i % len(MODEL_COLORS)] for i, mv in enumerate(models)}
    legend_rows = []
    for line in lines:
        parts = []
        for d in data:
            if d["line"] != line:
                continue
            parts.append((color_of[d["model"]], "%s：%d单" % (d["model"], d["total"])))
        if parts:
            legend_rows.append((line, parts))
    for li, (line, parts) in enumerate(legend_rows):
        rr = lr + li
        ws.cell(rr, 1, "【%s】" % line).font = Font(size=10, bold=True, color="333333")
        for pi, (col, txt) in enumerate(parts):
            cc = ws.cell(rr, 2 + pi, "■ %s" % txt)
            cc.font = Font(size=10, color=col)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 10
    # 原生图表：堆叠柱状图（X=拉线，单柱=该拉线当月不良总单数，柱内按型号分段着色，颜色=型号）
    # 每个型号的值写入辅助列（F 起，表头=型号名），类别=拉线写入最后辅助列
    cat_col = 6 + len(models)
    for si, mv in enumerate(models):
        cc = ws.cell(2, 6 + si, mv)
        cc.font = Font(bold=True, size=10)
        for d in data:
            if d["model"] != mv:
                continue
            li = lines.index(d["line"])
            ws.cell(3 + li, 6 + si, d["total"])
    ws.cell(2, cat_col, "拉线").font = Font(bold=True, size=10)
    for li, line in enumerate(lines):
        ws.cell(3 + li, cat_col, line)
    chart = BarChart()
    chart.type = "col"
    chart.grouping = "stacked"
    chart.overlap = 100
    chart.title = title
    chart.style = 10
    data_ref = Reference(ws, min_col=6, min_row=2, max_col=cat_col - 1, max_row=2 + len(lines))
    # 类别=拉线：与系列数据同基准（数据从第 3 行起，末行 2+len(lines)）。
    # 曾误用 min_row=2/max_row=1+len(lines) → 表头「拉线」串入首类别且丢最后一条，X 轴整体错位。
    cats = Reference(ws, min_col=cat_col, min_row=3, max_row=2 + len(lines))
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats)
    for si in range(len(models)):
        try:
            chart.series[si].graphicalProperties.solidFill = MODEL_COLORS[si % len(MODEL_COLORS)]
            chart.series[si].graphicalProperties.line.noFill = True
        except Exception:
            pass
    try:
        chart.legend.position = "b"
    except Exception:
        pass
    chart.height = 10
    chart.width = 17
    chart.y_axis.title = "单数"
    chart.x_axis.title = "拉线"
    ws.add_chart(chart, "G2")

    # === A4 竖向打印设置 ===
    ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = 0.4
    ws.page_margins.right = 0.4
    ws.page_margins.top = 0.5
    ws.page_margins.bottom = 0.5
    ws.page_margins.header = 0.3
    ws.page_margins.footer = 0.3
    ws.print_options.horizontalCentered = True
    ws.print_title_rows = "1:2"  # 标题+表头每页重复
    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.save(out)
    wb.close()
    return out


def cmd_analysis(args):
    root = args.get("root")
    month = (args.get("month") or "").strip()
    if not root or not month:
        raise ValueError("缺少 --root 或 --month(YYYY-MM)")
    model = (args.get("model") or "").strip() or None
    title, lines, models, data = _analysis_data(root, month, model)
    out = args.get("out")
    out_path = None
    if out:
        # 输出格式由扩展名决定：.xlsx=旧 Excel（openpyxl 原生图表）；.docx=可编辑 Word（A4 打印）；其它=PDF（A4 直接打印）
        if out.lower().endswith(".xlsx"):
            out_path = _write_analysis_xlsx(out, month, title, lines, models, data)
        elif out.lower().endswith(".docx"):
            from _analysis_docx import _write_analysis_docx
            out_path = _write_analysis_docx(out, title, lines, models, data)
        else:
            out_path = _write_analysis_pdf(out, title, lines, models, data)
    return {"ok": True, "month": month, "model": model or None, "title": title,
            "lines": lines, "models": models, "data": data, "out": out_path}


def cmd_analysis_auto(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    today = datetime.date.today()
    last_month = (today.replace(day=1) - datetime.timedelta(days=1))
    month = last_month.strftime("%Y-%m")
    adir = os.path.join(root, "_analysis")
    os.makedirs(adir, exist_ok=True)
    status_path = os.path.join(adir, "latest.json")
    out = os.path.join(adir, "%d年%d月品质数据.pdf" % (last_month.year, last_month.month))
    # 幂等：已生成同月报告则跳过
    if os.path.exists(status_path):
        try:
            with open(status_path, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("month") == month and os.path.exists(st.get("file", "")):
                return {"ok": True, "already": True, "month": month, "file": st["file"]}
        except Exception:
            pass
    title, lines, models, data = _analysis_data(root, month, None)
    if not data:
        return {"ok": True, "month": month, "data": [], "message": "上月无数据", "file": None}
    _write_analysis_pdf(out, title, lines, models, data)
    status = {"month": month, "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "file": out, "title": title, "models": models}
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    return {"ok": True, "already": False, "month": month, "file": out, "models": models, "data": data}


def cmd_analysis_status(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    status_path = os.path.join(root, "_analysis", "latest.json")
    if not os.path.exists(status_path):
        return {"ok": True, "available": False}
    try:
        with open(status_path, encoding="utf-8") as f:
            st = json.load(f)
        return {"ok": True, "available": True, "month": st.get("month"),
                "file": st.get("file"), "title": st.get("title"),
                "generatedAt": st.get("generatedAt"), "models": st.get("models", [])}
    except Exception as e:
        return {"ok": True, "available": False, "error": str(e)}


# ---------------- 入口 ----------------

def parse_args(argv):
    args = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
                args[k[2:].replace("-", "_")] = v
            else:
                args[a[2:].replace("-", "_")] = argv[i + 1] if i + 1 < len(argv) else "1"
                i += 1
        i += 1
    return args


CMDS = {
    "meta": cmd_meta, "query": cmd_query, "add_single": cmd_add_single,
    "batch_import": cmd_batch_import, "delete_row": cmd_delete_row,
    "batch_delete": cmd_batch_delete,
    "export": cmd_export, "template": cmd_template,
    "extract_images": cmd_extract_images, "analysis": cmd_analysis,
    "analysis_auto": cmd_analysis_auto, "analysis_status": cmd_analysis_status,
}


def main(argv):
    if not argv or argv[0] not in CMDS:
        print(json.dumps({"success": False, "error": "未知命令: %s" % (argv[0] if argv else "")},
                         ensure_ascii=False))
        return 1
    args = parse_args(argv[1:])
    try:
        res = CMDS[argv[0]](args)
        print(json.dumps({"success": True, **res}, ensure_ascii=False, default=str))
        return 0
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
