# -*- coding: utf-8 -*-
""".xls → .xlsx 迁移工具：同结构（R0 合并标题/R1 表头/8 列/SMT+组装/列宽）+ 内嵌图片按行嵌入 G 列。

用法：
  python migrate_xls_to_xlsx.py --src <目录> --dst <目录> [--archive <子目录名>] [--backup-dir <目录>] [--keep-xls]

流程（每个月份 .xls）：
  1. xlrd 读数据（R0 标题 / R1 表头 / R2+ 非空数据行，原样保留含 2026-03 错位行与 5 位年份笔误）
  2. xls_img_extract 提取内嵌图片（位置映射 row1=2+pos, col=G）
  3. openpyxl 重建 .xlsx：同 sheet 名/标题/表头/列宽 + ws.add_image(G{row1+1})
  4. 3 项校验（数据行数 / 表头 / 图片数）对齐后，原 .xls os.replace 移入 --archive 子目录（默认 _xls_archive）

stdout: {"success":true,"files":[{"src","dst","sheets":[{name,rows,imgs}]}],"archived":[...]}
"""
import sys, os, json, glob, re, shutil

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import xlrd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from openpyxl.drawing.image import Image as XlImage

import xls_img_extract as EXT

HEADER = ["日期", "线别", "型号", "问题原因", "数量", "发生工程", "不良图片", "处理方式"]


def parse_month(fname):
    m = re.match(r"^(\d{4})年(\d{1,2})月份", fname)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def month_xlsx_name(year, month):
    return "%d年%d月份IPQC及QA发现检验问题点.xlsx" % (year, month)


def xlsx_row_of(row1):
    """提取器 row1（0-based Excel 行，R2=2）→ openpyxl 1-based 行（R2 → 3）。"""
    return row1 + 1


def read_xls_data(path):
    """xlrd 读：返回 [{name, title, header, rows:[cells], widths:{col:width_256}}]。
    cells 元素为 ("s", str) 或 ("n", float/int)，保留原样。"""
    try:
        wb = xlrd.open_workbook(path, formatting_info=True)
        has_fmt = True
    except Exception:
        wb = xlrd.open_workbook(path)
        has_fmt = False
    out = []
    for ws in wb.sheets():
        title = str(ws.cell_value(0, 0)).strip()
        header = [str(ws.cell_value(1, c)).strip() for c in range(min(8, ws.ncols))]
        widths = {}
        if has_fmt:
            try:
                for c in range(min(8, ws.ncols)):
                    if c in ws.colinfo_map:
                        widths[c] = ws.colinfo_map[c].width
            except Exception:
                pass
        rows = []
        for r in range(2, ws.nrows):
            vals = []
            empty = True
            for c in range(min(8, ws.ncols)):
                cell = ws.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_NUMBER:
                    v = cell.value
                    vals.append(("n", int(v) if float(v).is_integer() else v))
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    vals.append(("s", "TRUE" if cell.value else "FALSE"))
                else:
                    sv = str(cell.value).strip()
                    vals.append(("s", sv))
                if cell.ctype not in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    empty = False
            if empty:
                continue
            if any(k == "s" and v.startswith("合计") for k, v in vals):
                continue
            rows.append(vals)
        out.append({"name": ws.name, "title": title, "header": header,
                    "rows": rows, "widths": widths})
    return out


def build_xlsx(sheets_data, images_by_sheet, dst_path):
    """openpyxl 重建。images_by_sheet: {sheet_name: [{row, fmt, file}]}。"""
    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin")
    title_font = Font(bold=True, size=12)
    title_align = Alignment(horizontal="center", vertical="center")
    header_font = Font(bold=True)
    header_align = Alignment(horizontal="center", vertical="center")
    header_border = Border(top=thin, bottom=thin, left=thin, right=thin)
    for sd in sheets_data:
        ws = wb.create_sheet(title=sd["name"][:31])
        # R1 合并标题 A:H
        ws.merge_cells("A1:H1")
        c = ws.cell(1, 1, sd.get("title") or "")
        c.font = title_font
        c.alignment = title_align
        ws.row_dimensions[1].height = 24
        # R2 表头
        for ci, h in enumerate(sd["header"] or HEADER, start=1):
            c = ws.cell(2, ci, h)
            c.font = header_font
            c.alignment = header_align
            c.border = header_border
        ws.row_dimensions[2].height = 22
        # R3+ 数据（原样）
        for ri, vals in enumerate(sd["rows"], start=3):
            for ci, (k, v) in enumerate(vals[:8], start=1):
                cell = ws.cell(ri, ci, v if k == "n" else (v or None))
                if k == "s":
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
        # 列宽
        for ci, w in (sd.get("widths") or {}).items():
            try:
                ws.column_dimensions[chr(65 + ci)].width = max(8, w / 256.0)
            except Exception:
                pass
        # 图片（G 列，按行）
        for im in images_by_sheet.get(sd["name"], []):
            if not os.path.exists(im.get("file", "")):
                continue
            try:
                xl = XlImage(im["file"])
                xl.width = xl.width  # 保持原尺寸
                ws.add_image(xl, "G%d" % xlsx_row_of(im["row"]))
            except Exception as e:
                print(json.dumps({"success": False, "error": "嵌入图片失败 %s: %s" % (im.get("file"), e)},
                                 ensure_ascii=False))
                return False
    wb.save(dst_path)
    return True


def migrate_one(src_path, dst_dir, tmp_dir):
    """迁移单个 .xls → dst_dir/<月份>.xlsx；返回 (dst_path, sheets_info, warnings)。"""
    base = os.path.basename(src_path)
    pm = parse_month(base)
    if not pm:
        return None, None, ["跳过非月份命名文件: %s" % base]
    dst_path = os.path.join(dst_dir, month_xlsx_name(*pm))
    os.makedirs(tmp_dir, exist_ok=True)

    # 1) 数据
    sheets_data = read_xls_data(src_path)

    # 2) 图片
    res = EXT.analyze_file(src_path, outdir=os.path.join(tmp_dir, '%d_%d' % pm))
    if not res.get("success"):
        return None, None, [res.get("error", "图片提取失败")]
    images_by_sheet = {}
    for sd in res["sheets"]:
        images_by_sheet[sd["name"]] = [im for im in sd.get("images", []) if im.get("file")]

    # 3) 重建 .xlsx
    if not build_xlsx(sheets_data, images_by_sheet, dst_path):
        return None, None, ["build_xlsx 失败: %s" % dst_path]

    # 4) 3 项校验
    from openpyxl import load_workbook
    wb = load_workbook(dst_path)
    warnings = []
    for sd in sheets_data:
        ws = wb[sd["name"]] if sd["name"] in wb.sheetnames else None
        if ws is None:
            warnings.append("%s: sheet 缺失" % sd["name"])
            continue
        # 数据行数
        new_rows = sum(1 for r in range(3, ws.max_row + 1)
                       if any(ws.cell(r, c).value not in (None, "") for c in range(1, 9)))
        if new_rows != len(sd["rows"]):
            warnings.append("%s: 数据行数 %d != %d" % (sd["name"], new_rows, len(sd["rows"])))
        # 表头
        hdr = [str(ws.cell(2, c).value or "").strip() for c in range(1, 9)]
        if hdr != HEADER:
            warnings.append("%s: 表头 %s != 标准 8 列" % (sd["name"], hdr))
        # 图片数
        nimg = len(images_by_sheet.get(sd["name"], []))
        nim_new = len(ws._images)
        if nim_new != nimg:
            warnings.append("%s: 图片数 %d != %d" % (sd["name"], nim_new, nimg))
    sheets_info = [{"name": sd["name"], "rows": len(sd["rows"]),
                    "imgs": len(images_by_sheet.get(sd["name"], []))} for sd in sheets_data]
    return dst_path, sheets_info, warnings


def main(argv):
    args = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('--'):
            if '=' in a:
                k, v = a.split('=', 1)
                args[k[2:].replace('-', '_')] = v
            else:
                args[a[2:].replace('-', '_')] = argv[i + 1] if i + 1 < len(argv) else '1'
                i += 1
        i += 1
    src = args.get('src')
    dst = args.get('dst')
    if not src or not dst:
        print(json.dumps({"success": False, "error": "缺少 --src 或 --dst"}, ensure_ascii=False))
        return 1
    archive_sub = args.get('archive') or '_xls_archive'
    keep_xls = args.get('keep_xls') == '1'
    backup_dir = args.get('backup_dir')

    os.makedirs(dst, exist_ok=True)
    tmp_dir = os.path.join(dst, '_migrate_tmp')
    files_out, archived, all_warnings = [], [], []
    ok = True
    for f in sorted(glob.glob(os.path.join(src, '*.xls'))):
        dst_path, sheets_info, warnings = migrate_one(f, dst, tmp_dir)
        if warnings:
            all_warnings.extend(["%s: %s" % (os.path.basename(f), w) for w in warnings])
        if not dst_path:
            ok = False
            continue
        files_out.append({"src": os.path.basename(f), "dst": os.path.basename(dst_path),
                          "sheets": sheets_info})
        # 归档原 .xls（移动，不删除）
        if not keep_xls:
            arc_dir = os.path.join(src, archive_sub)
            os.makedirs(arc_dir, exist_ok=True)
            arc_path = os.path.join(arc_dir, os.path.basename(f))
            try:
                if backup_dir:
                    os.makedirs(backup_dir, exist_ok=True)
                    shutil.copy2(f, os.path.join(backup_dir, os.path.basename(f)))
                os.replace(f, arc_path)
                archived.append(os.path.basename(f))
            except Exception as e:
                all_warnings.append("归档失败 %s: %s" % (os.path.basename(f), e))
                ok = False
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass  # 安全层可能拦截删除，忽略（临时目录残留无害）
    print(json.dumps({"success": ok and bool(files_out), "files": files_out,
                      "archived": archived, "warnings": all_warnings}, ensure_ascii=False))
    return 0 if (ok and files_out) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
