# -*- coding: utf-8 -*-
"""_fix_header_row.py —— 修复"表头在表格底部"：把表头行移回 row2，保留全部数据与图片（in-cell 重建）。
用法: python _fix_header_row.py <ROOT>   （ROOT 默认生产；测试传临时 root）
"""
import sys, os, shutil
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import defect_history as dh

ROOT = sys.argv[1] if len(sys.argv) > 1 else r'E:\生产不良履历'


def is_header_rec(rec):
    return str(rec[0] or "").strip() == "日期" and str(rec[2] or "").strip() == "型号"


def fix(root):
    def mutate(wb):
        ws = wb[dh.SHEET_NAME]
        rows = dh.read_rows(ws)
        imgs, is_in_cell = dh.load_imgs_for_write(root, ws)
        header = None
        data = []
        for rec in rows:
            if is_header_rec(rec):
                header = rec
            else:
                data.append(rec)
        if header is None:
            raise ValueError("未找到表头行（c1=日期 c3=型号）——无需修复或结构异常")
        n_header_old = rows.index(header)          # 表头当前 idx
        new_rows = [header] + data
        # 图片重映射：原表头 idx 无图；原数据 idx < 表头idx 的 +1（表头插到 0 前移），> 表头idx 的 不变
        kept = []
        for d, bdata, fmt in imgs:
            if d == n_header_old:
                continue
            kept.append((d + 1 if d < n_header_old else d, bdata, fmt))
        dh._rewrite(ws, new_rows)
        dh.apply_imgs(wb, ws, kept, is_in_cell)
        print("表头原 idx=%d → 新 idx=0 | 数据 %d 行 | 图片重映射 %d 张" % (n_header_old, len(data), len(kept)))

    dh.write_book_locked(root, mutate)
    # 验证
    wb = dh.load_workbook(dh.book_path(root))
    ws = wb[dh.SHEET_NAME]
    r1, r2 = [ws.cell(r, 1).value for r in (1, 2)]
    last = ws.cell(ws.max_row, 1).value
    ic = dh.in_cell_images(dh.book_path(root), dh.SHEET_NAME)
    print("修复后: row1=%r | row2=%r | max_row=%d | 末行=%r | in-cell 图=%d" % (r1, r2, ws.max_row, last, len(ic)))
    wb.close()
    q = dh.cmd_query({"root": root})
    fake = [x for x in q.get("rows", []) if x.get("date") == "日期"]
    print("H5 无筛选 total=%d | 表头假行=%d" % (q.get("total"), len(fake)))
    return 0 if r2 == "日期" and not fake else 1


if __name__ == "__main__":
    sys.exit(fix(ROOT))
