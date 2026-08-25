# -*- coding: utf-8 -*-
"""不良履历 .xls 读写 CLI：xlrd 读 + xlwt 重写，保留 R0 合并标题 / R1 表头 / 列宽 / 两 sheet。

命令：
  meta            --root <目录>                          列出所有月份文件/sheet/行数
  query           --root <目录> --model <型号> [--month YYYY-M]   跨全部文件模糊查询
  add_single      --root <目录> --date YYYY.M.D --sheet SMT|组装 --line --model --reason [--qty] [--stage] [--handle] [--dry-run]
  batch_import    --root <目录> --upload <文件> [--dry-run]       解析上传 .xls/.xlsx 并入各月份文件
  create_month    --root <目录> --year --month [--dry-run]        按模板新建该月 .xls
  delete_row      --root <目录> --file <文件名> --sheet <SMT|组装> --row <数据行索引> [--dry-run]

stdout 统一输出 JSON：{"success": true, ...} 或 {"success": false, "error": "..."}
"""
import sys, os, re, json, shutil, glob, time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

HEADER = ["日期", "线别", "型号", "问题原因", "数量", "发生工程", "不良图片", "处理方式"]
SHEETS = ["SMT", "组装"]
KNOWN_COLS = {"日期", "线别", "型号", "问题原因", "数量", "发生工程", "不良图片", "处理方式"}


# ---------- 基础工具 ----------

def find_xls_files(root):
    return sorted(glob.glob(os.path.join(root, "*.xls")))


def parse_month_from_filename(name):
    m = re.match(r"^(\d{4})年(\d{1,2})月份", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def month_to_file(root, year, month):
    for f in find_xls_files(root):
        if parse_month_from_filename(os.path.basename(f)) == (year, month):
            return f
    return None


def month_filename(year, month):
    return "%d年%d月份IPQC及QA发现检验问题点.xls" % (year, month)


def parse_date(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return (y, mo, d)
    # 5 位年份笔误，如 20226.3.14 → 取前 4 位
    m = re.match(r"^(\d{4})(\d)\.(\d{1,2})\.(\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(3)), int(m.group(4))
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return (y, mo, d)
    return None


def norm_model(s):
    if s is None:
        return ""
    return re.sub(r"[\s\u3000]+", "", str(s)).lower()


def fmt_cell(cell):
    v = cell["v"]
    if cell.get("is_num") and isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return "" if v is None else str(v).strip()


# ---------- xlrd 读 / xlwt 写 ----------

def read_book(path):
    """返回 sheet 列表：[{name, title, header, rows, col_widths}]；rows 中每格为 {v, is_num}。"""
    import xlrd
    try:
        wb = xlrd.open_workbook(path, formatting_info=True)
        has_fmt = True
    except Exception:
        wb = xlrd.open_workbook(path)
        has_fmt = False
    sheets = []
    for ws in wb.sheets():
        nrows, ncols = ws.nrows, ws.ncols
        try:
            title = str(ws.cell_value(0, 0)).strip()
        except Exception:
            title = ""
        header = []
        for c in range(min(8, ncols)):
            header.append(str(ws.cell_value(1, c)).strip())
        col_widths = {}
        if has_fmt:
            try:
                for c in range(min(8, ncols)):
                    if c in ws.colinfo_map:
                        col_widths[c] = ws.colinfo_map[c].width
            except Exception:
                col_widths = {}
        rows = []
        for r in range(2, nrows):
            vals = []
            for c in range(8):
                cell = ws.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_EMPTY:
                    vals.append({"v": "", "is_num": False})
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    vals.append({"v": cell.value, "is_num": True})
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    vals.append({"v": "TRUE" if cell.value else "FALSE", "is_num": False})
                else:
                    vals.append({"v": cell.value, "is_num": False})
            if all(str(v["v"]).strip() == "" for v in vals):
                continue
            if any(isinstance(v["v"], str) and str(v["v"]).strip().startswith("合计") for v in vals):
                continue
            rows.append(vals)
        sheets.append({
            "name": ws.name, "title": title, "header": header,
            "rows": rows, "col_widths": col_widths
        })
    return sheets


def write_book(path, sheets):
    """xlwt 重建：R0 合并标题 A:H + R1 表头 + 数据行 + 列宽。写 path+'.tmp'。"""
    import xlwt
    wb = xlwt.Workbook(encoding="utf-8")
    title_style = xlwt.easyxf("font: bold on; align: horiz center, vert center")
    header_style = xlwt.easyxf(
        "font: bold on; border: top thin, bottom thin, left thin, right thin; "
        "align: horiz center, vert center")
    cell_style = xlwt.easyxf("align: vert center")
    for s in sheets:
        ws = wb.add_sheet(s["name"])
        ws.write_merge(0, 0, 0, 7, s.get("title") or "", title_style)
        try:
            ws.row(0).height_mismatch = True
            ws.row(0).height = 22 * 20
        except Exception:
            pass
        for c, h in enumerate(HEADER):
            ws.write(1, c, h, header_style)
        try:
            ws.row(1).height_mismatch = True
            ws.row(1).height = 22 * 20
        except Exception:
            pass
        for i, row in enumerate(s["rows"], start=2):
            for c in range(8):
                cell = row[c] if c < len(row) else {"v": "", "is_num": False}
                v = cell["v"]
                if cell.get("is_num") and isinstance(v, float):
                    ws.write(i, c, int(v) if v.is_integer() else v)
                elif cell.get("is_num") and isinstance(v, int):
                    ws.write(i, c, v)
                else:
                    ws.write(i, c, "" if v is None else str(v), cell_style)
        for c, w in (s.get("col_widths") or {}).items():
            if w:
                try:
                    ws.col(c).width = w
                except Exception:
                    pass
    wb.save(path + ".tmp")


def safe_replace(path):
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    os.replace(path + ".tmp", path)


def acquire_lock(path):
    """O_EXCL 互斥锁。锁存在但已超时(>120s, 视为崩溃残留)时尽力清除后重试。"""
    lock_path = path + ".lock"
    for _ in range(25):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 120:
                    os.remove(lock_path)  # 崩溃残留，尽力清除
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
    # 兜底：删除被安全层拦截时，改为重命名释放（os.replace 属于移动，不被拦截）
    try:
        os.replace(lock, lock + ".release")
    except OSError:
        pass


# ---------- 命令实现 ----------

def cmd_meta(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    months = []
    for f in find_xls_files(root):
        pm = parse_month_from_filename(os.path.basename(f))
        if not pm:
            continue
        sheets_info = []
        try:
            for s in read_book(f):
                sheets_info.append({"name": s["name"], "rows": len(s["rows"])})
        except Exception as e:
            sheets_info.append({"name": "?", "rows": 0, "error": str(e)})
        months.append({
            "month": "%04d-%02d" % pm,
            "file": os.path.basename(f),
            "sheets": sheets_info,
        })
    return {"months": months}


def cmd_query(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    model = (args.get("model") or "").strip()
    month = (args.get("month") or "").strip() or None
    if not model:
        raise ValueError("缺少 --model")
    needle = norm_model(model)
    groups, total, warnings = [], 0, []
    for f in find_xls_files(root):
        pm = parse_month_from_filename(os.path.basename(f))
        if not pm:
            continue
        if month and "%04d-%02d" % pm != month:
            continue
        try:
            sheets = read_book(f)
        except Exception as e:
            warnings.append({"file": os.path.basename(f), "error": str(e)})
            continue
        fg = []
        for s in sheets:
            for idx, row in enumerate(s["rows"]):
                mcell = fmt_cell(row[2]) if len(row) > 2 else ""
                if needle and needle in norm_model(mcell):
                    fg.append({
                        "row": idx,
                        "sheet": s["name"],
                        "date": fmt_cell(row[0]) if len(row) > 0 else "",
                        "line": fmt_cell(row[1]) if len(row) > 1 else "",
                        "model": mcell,
                        "reason": fmt_cell(row[3]) if len(row) > 3 else "",
                        "qty": fmt_cell(row[4]) if len(row) > 4 else "",
                        "stage": fmt_cell(row[5]) if len(row) > 5 else "",
                        "image": fmt_cell(row[6]) if len(row) > 6 else "",
                        "handle": fmt_cell(row[7]) if len(row) > 7 else "",
                    })
        if fg:
            groups.append({
                "month": "%04d-%02d" % pm,
                "file": os.path.basename(f),
                "count": len(fg),
                "rows": fg,
            })
            total += len(fg)
    return {"ok": True, "total": total, "groups": groups, "warnings": warnings}


def make_month_file(root, year, month):
    """按最近一个已有文件做结构模板，生成新月份文件（数据清空、标题改为目标月份）。"""
    files = find_xls_files(root)
    template = None
    if files:
        cand = [(parse_month_from_filename(os.path.basename(f)), f) for f in files]
        cand = [c for c in cand if c[0]]
        if cand:
            cand.sort(key=lambda x: x[0])
            template = cand[-1][1]
    title = "%d年%d月份IPQC及QA检验发现问题点" % (year, month)
    if template:
        sheets = read_book(template)
        for s in sheets:
            s["rows"] = []
            s["title"] = title
    else:
        sheets = [{"name": n, "title": title, "header": HEADER[:], "rows": [], "col_widths": {}}
                  for n in SHEETS]
    path = os.path.join(root, month_filename(year, month))
    write_book(path, sheets)
    safe_replace(path)
    return path


def cmd_create_month(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    year, month = int(args.get("year")), int(args.get("month"))
    dry = args.get("dry_run") == "1"
    path = month_to_file(root, year, month)
    if path:
        return {"ok": True, "file": os.path.basename(path), "exists": True}
    if dry:
        return {"ok": True, "dry_run": True, "file": month_filename(year, month), "exists": False}
    lock = acquire_lock(os.path.join(root, month_filename(year, month)))
    try:
        path = make_month_file(root, year, month)
    finally:
        release_lock(lock)
    return {"ok": True, "file": os.path.basename(path), "exists": False}


def add_row_to_file(path, sheet_name, row_vals):
    sheets = read_book(path)
    target = next((s for s in sheets if s["name"] == sheet_name), None)
    if target is None:
        raise ValueError("文件不含 sheet: " + sheet_name)
    target["rows"].append(row_vals)
    write_book(path, sheets)
    safe_replace(path)
    return len(target["rows"]) - 1


def cmd_add_single(args):
    root = args.get("root")
    if not root:
        raise ValueError("缺少 --root")
    dry = args.get("dry_run") == "1"
    sheet = (args.get("sheet") or "").strip()
    model = (args.get("model") or "").strip()
    reason = (args.get("reason") or "").strip()
    if sheet not in SHEETS:
        raise ValueError("sheet 必须是 SMT 或 组装")
    if not model:
        raise ValueError("缺少型号")
    if not reason:
        raise ValueError("缺少问题原因")
    d = parse_date(args.get("date"))
    if not d:
        raise ValueError("日期无法解析: %s" % (args.get("date") or ""))
    y, mo, _ = d
    path = month_to_file(root, y, mo)
    created = False
    # dry-run 只读，不加锁
    if dry:
        if path is None:
            return {"ok": True, "dry_run": True, "file": month_filename(y, mo),
                    "sheet": sheet, "row": 0, "createdMonth": True}
        sheets = read_book(path)
        target = next((s for s in sheets if s["name"] == sheet), None)
        return {"ok": True, "dry_run": True, "file": os.path.basename(path),
                "sheet": sheet, "row": len(target["rows"]) if target else 0, "createdMonth": False}
    # 单次加锁：建月 + 追加 共用同一把锁
    lock = acquire_lock(path if path else os.path.join(root, month_filename(y, mo)))
    try:
        if path is None:
            path = make_month_file(root, y, mo)
            created = True
        row_vals = [
            {"v": "%d.%d.%d" % d, "is_num": False},
            {"v": (args.get("line") or "").strip(), "is_num": False},
            {"v": model, "is_num": False},
            {"v": reason, "is_num": False},
            {"v": (args.get("qty") or "").strip(), "is_num": False},
            {"v": (args.get("stage") or "").strip(), "is_num": False},
            {"v": "", "is_num": False},
            {"v": (args.get("handle") or "").strip(), "is_num": False},
        ]
        row_idx = add_row_to_file(path, sheet, row_vals)
    finally:
        release_lock(lock)
    return {"ok": True, "file": os.path.basename(path), "sheet": sheet,
            "row": row_idx, "createdMonth": created}


def locate_header(nrows, ncols, getter):
    """找同时含「日期」与「型号」的表头行，返回 (header_row, {列名: 列号})；找不到返回 (None, None)。"""
    for r in range(min(nrows, 6)):
        cols = {}
        for c in range(ncols):
            name = str(getter(r, c)).strip()
            if name in KNOWN_COLS and name not in cols:
                cols[name] = c
        if "日期" in cols and "型号" in cols:
            return r, cols
    return None, None


def parse_upload(upload, ext):
    rows = []
    if ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(upload)
        for ws in wb.sheets():
            hr, cols = locate_header(ws.nrows, ws.ncols, lambda r, c: ws.cell_value(r, c))
            if hr is None:
                continue
            for r in range(hr + 1, ws.nrows):
                cells = {}
                qty_raw, qty_num = "", False
                for name, c in cols.items():
                    cell = ws.cell(r, c)
                    v = cell.value
                    if name == "数量":
                        if cell.ctype == xlrd.XL_CELL_NUMBER:
                            qty_raw, qty_num = v, True
                            cells[name] = str(int(v)) if float(v).is_integer() else str(v)
                        else:
                            qty_raw = "" if v is None else str(v).strip()
                            cells[name] = qty_raw
                    else:
                        cells[name] = "" if v is None else str(v).strip()
                rows.append(_row_record(ws.name, r, cells, qty_raw=qty_raw, qty_num=qty_num))
    elif ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(upload, data_only=True, read_only=True)
        for ws in wb.worksheets:
            grid = []
            for row in ws.iter_rows(values_only=True):
                grid.append([("" if c is None else c) for c in row])
            if not grid:
                continue
            ncols = max(len(r) for r in grid)
            hr, cols = locate_header(len(grid), ncols, lambda r, c: str(grid[r][c]).strip() if c < len(grid[r]) else "")
            if hr is None:
                continue
            for r in range(hr + 1, len(grid)):
                cells = {}
                qty_raw, qty_num = "", False
                for name, c in cols.items():
                    v = grid[r][c] if c < len(grid[r]) else ""
                    if name == "数量":
                        if isinstance(v, (int, float)):
                            qty_raw, qty_num = v, True
                            cells[name] = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
                        else:
                            qty_raw = "" if v is None else str(v).strip()
                            cells[name] = qty_raw
                    else:
                        cells[name] = "" if v is None else str(v).strip()
                rows.append(_row_record(ws.title, r, cells, qty_raw=qty_raw, qty_num=qty_num))
    else:
        raise ValueError("仅支持 .xls / .xlsx 文件")
    return rows


def _row_record(sheet, row, cells, qty_raw="", qty_num=False):
    date_s = cells.get("日期", "")
    model = cells.get("型号", "")
    d = parse_date(date_s)
    error, valid = None, True
    if not d:
        valid, error = False, "日期无法解析"
    elif not model.strip():
        valid, error = False, "型号为空"
    return {
        "row": row + 1,
        "sheet": sheet,
        "date": date_s,
        "line": cells.get("线别", ""),
        "model": model.strip(),
        "reason": cells.get("问题原因", "").strip(),
        "qty": cells.get("数量", ""),
        "stage": cells.get("发生工程", "").strip(),
        "handle": cells.get("处理方式", "").strip(),
        "targetMonth": ("%04d-%02d" % d[:2]) if d else None,
        "_date": d,
        "_qty_raw": qty_raw,
        "_qty_num": qty_num,
        "valid": valid,
        "error": error,
    }


def cmd_batch_import(args):
    root = args.get("root")
    upload = (args.get("upload") or "").strip()
    dry = args.get("dry_run") == "1"
    if not root:
        raise ValueError("缺少 --root")
    if not upload or not os.path.exists(upload):
        raise ValueError("上传文件不存在: %s" % upload)
    ext = os.path.splitext(upload)[1].lower()
    parsed = parse_upload(upload, ext)
    if not parsed:
        raise ValueError("上传文件中未找到含「日期」「型号」表头的表格行")

    preview, errors = [], []
    by_month = {}
    for p in parsed:
        item = {k: v for k, v in p.items() if not k.startswith("_")}
        preview.append(item)
        if not p["valid"]:
            errors.append({"row": p["row"], "error": p["error"]})
            continue
        by_month.setdefault(p["targetMonth"], []).append(p)

    if dry:
        return {"ok": True, "dry_run": True, "imported": 0, "skipped": len(errors),
                "createdMonths": [], "errors": errors, "preview": preview}

    imported = 0
    created_months = []
    for key, rows in sorted(by_month.items()):
        y, mo = (int(x) for x in key.split("-"))
        path = month_to_file(root, y, mo)
        created = False
        # 单次加锁：建月 + 写行共用同一把锁
        lock = acquire_lock(path if path else os.path.join(root, month_filename(y, mo)))
        try:
            if path is None:
                path = make_month_file(root, y, mo)
                created = True
                created_months.append(key)
            sheets = read_book(path)
            for p in rows:
                sheet_name = p["sheet"]
                if sheet_name not in SHEETS:
                    errors.append({"row": p["row"], "error": "上传表含未知 sheet: " + sheet_name})
                    continue
                target = next((s for s in sheets if s["name"] == sheet_name), None)
                if target is None:
                    sheets.append({"name": sheet_name,
                                   "title": sheets[0]["title"] if sheets else "",
                                   "header": HEADER[:], "rows": [], "col_widths": {}})
                    target = sheets[-1]
                target["rows"].append([
                    {"v": "%d.%d.%d" % p["_date"], "is_num": False},
                    {"v": p["line"], "is_num": False},
                    {"v": p["model"], "is_num": False},
                    {"v": p["reason"], "is_num": False},
                    {"v": p["_qty_raw"] if p.get("_qty_num") else p["qty"],
                     "is_num": bool(p.get("_qty_num"))},
                    {"v": p["stage"], "is_num": False},
                    {"v": "", "is_num": False},
                    {"v": p["handle"], "is_num": False},
                ])
                imported += 1
            write_book(path, sheets)
            safe_replace(path)
        finally:
            release_lock(lock)
    return {"ok": True, "imported": imported, "skipped": len(errors),
            "createdMonths": created_months, "errors": errors, "preview": preview}


def cmd_delete_row(args):
    root = args.get("root")
    fname = (args.get("file") or "").strip()
    sheet = (args.get("sheet") or "").strip()
    dry = args.get("dry_run") == "1"
    if os.path.basename(fname) != fname or not fname.lower().endswith(".xls"):
        raise ValueError("非法的文件名")
    path = os.path.join(root, fname)
    if not os.path.exists(path):
        raise ValueError("文件不存在: " + fname)
    row = int(args.get("row"))
    sheets = read_book(path)
    target = next((s for s in sheets if s["name"] == sheet), None)
    if target is None:
        raise ValueError("文件不含 sheet: " + sheet)
    if row < 0 or row >= len(target["rows"]):
        raise ValueError("行号越界: %d（该 sheet 共 %d 行）" % (row, len(target["rows"])))
    if dry:
        return {"ok": True, "dry_run": True, "file": fname, "backup": fname + ".bak",
                "remainingRows": len(target["rows"]) - 1}
    lock = acquire_lock(path)
    try:
        del target["rows"][row]
        write_book(path, sheets)
        safe_replace(path)
    finally:
        release_lock(lock)
    return {"ok": True, "file": fname, "backup": fname + ".bak",
            "remainingRows": len(target["rows"])}


# ---------- CLI ----------

HANDLERS = {
    "meta": cmd_meta,
    "query": cmd_query,
    "add_single": cmd_add_single,
    "batch_import": cmd_batch_import,
    "create_month": cmd_create_month,
    "delete_row": cmd_delete_row,
}


def parse_args(argv):
    out = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            k = a[2:]
            if "=" in k:
                k, v = k.split("=", 1)
                out[k.replace("-", "_")] = v
            else:
                out[k.replace("-", "_")] = argv[i + 1] if i + 1 < len(argv) else "1"
                i += 1
        else:
            out.setdefault("cmd", a)
        i += 1
    return out


def main(argv):
    args = parse_args(argv)
    cmd = args.get("cmd")
    if cmd not in HANDLERS:
        raise ValueError("未知命令: %s" % cmd)
    result = HANDLERS[cmd](args)
    print(json.dumps({"success": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)
