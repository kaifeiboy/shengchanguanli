# -*- coding: utf-8 -*-
"""
rebuild_images_mapping.py —— 图片归属重建工具（待用户确认映射后执行）

背景：原始 xls 的 MSODRAWING 存储顺序 ≠ 用户视觉行顺序，位置映射（第 i 张→行2+i）错配。
方案：用户对照审计报告 image_audit.html + WPS 打开原始文件，在 mapping.json 里给出每张图的正确归属行；
      本工具按 mapping 重建汇总 xlsx 的图片位置。

用法：
  python rebuild_images_mapping.py --mapping <json> --book E:\\生产不良履历\\不良履历汇总.xlsx
  mapping.json 格式：
  {
    "2026年3月份IPQC及QA发现检验问题点.xls": {
      "组装": {"0": 6, "1": 3, ...}   # 图索引 -> 该文件内数据行(1-based, xlrd 行号)
    },
    ...
  }
"""
import sys, os, json, io, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xls_img_extract as X
import xlrd
from openpyxl import load_workbook, Workbook
from openpyxl.drawing.image import Image as XlImage

BOOK = r'E:\生产不良履历\不良履历汇总.xlsx'
ORIG_DIR = r'E:\生产不良履历_迁移前备份'


def build_correct_mapping(mapping):
    """按 mapping 把每张图字节重新分配到目标数据行（返回 data_idx -> [(bytes, fmt)]）。"""
    result = {}  # 汇总工作簿 data_idx -> [(bdata, fmt)]
    for fname, sheets in mapping.items():
        fpath = os.path.join(ORIG_DIR, fname)
        if not os.path.exists(fpath):
            print('缺失文件:', fpath); continue
        res = X.analyze_file(fpath)
        wb = xlrd.open_workbook(fpath)
        for sname, img_map in sheets.items():
            ws = wb.sheet_by_name(sname)
            if sname == 'SMT':
                print('  SMT 舍弃:', fname); continue
            # 该文件内数据行（xlrd 行号，从 2 起非空）
            data_rows = []
            for r in range(2, ws.nrows):
                vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
                if all(not v for v in vals):
                    continue
                data_rows.append(r)
            # 该文件图片
            s = [x for x in res['sheets'] if x['name'] == sname][0]
            for img_idx_str, target_row in img_map.items():
                img_idx = int(img_idx_str)
                if img_idx >= len(s['images']):
                    print('  越界图索引:', img_idx); continue
                bdata = open(s['images'][img_idx]['file'], 'rb').read()
                fmt = s['images'][img_idx]['fmt']
                # 该文件数据行 -> 汇总全局 data_idx（按日期升序，需要模拟合并逻辑）
                # 简化：先按文件累计 + 行内顺序（合并逻辑在 merge_to_single_book 中按日期排序，
                # 这里要求 mapping 已经给出"汇总中的 data_idx"，或按 merge 的排序规则推算）
                # 为稳妥：mapping 中 target_row 若为负数，表示"汇总全局 data_idx"；否则为文件内行号
                if target_row < 0:
                    gidx = -target_row
                else:
                    # 文件内行号 -> 需要全局索引：先排序该文件数据行
                    # 此处简化：调用 merge 的排序（读汇总表按日期匹配该行日期）
                    gidx = _resolve_global_idx(target_row, fname, ws, sname)
                result.setdefault(gidx, []).append((bdata, fmt))
        wb.release_resources()
    return result


def _resolve_global_idx(file_row, fname, ws, sname):
    """按日期排序规则把文件内行号映射到汇总全局 data_idx（模拟 merge 逻辑）。"""
    from openpyxl import load_workbook as _lw
    # 读汇总表，找该文件该行日期对应的全局行
    date_val = ws.cell_value(file_row, 0)
    # 日期可能是 '2026.3.2' 或 '20226.3.14'
    # 解析为 yyyy-mm-dd
    import re, datetime
    def parse_d(s):
        s = str(s).strip()
        m = re.match(r'^(\d{4,5})\.(\d{1,2})\.(\d{1,2})$', s)
        if not m: return None
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y > 2100: y -= 10000 if y > 10000 else 0
        if y > 2100: y = y - 10000 if y >= 10000 else y
        # 5位年份容错
        if y >= 10000: y -= 10000
        try: return datetime.date(y, mo, d)
        except: return None
    dval = parse_d(date_val)
    if dval is None:
        print('  无法解析日期行 %d: %s' % (file_row, date_val))
        return -1
    wb2 = _lw(BOOK)
    ws2 = wb2.active
    for r in range(2, ws2.max_row + 1):
        v = ws2.cell(r, 1).value
        if isinstance(v, datetime.datetime) and v.date() == dval:
            wb2.close()
            return r - 2
        if isinstance(v, str):
            dd = parse_d(v)
            if dd == dval:
                wb2.close()
                return r - 2
    wb2.close()
    print('  汇总表未找到日期 %s' % dval)
    return -1


def rebuild(book, mapping, backup=True):
    if backup:
        shutil_backup(book)
    wb = load_workbook(book)
    ws = wb.active
    # 读当前图片（原图字节按锚点）
    from openpyxl.drawing.image import Image as _Img
    cur_imgs = {}
    for im in ws._images:
        try:
            b = im._data()
            r0 = im.anchor._from.row
            cur_imgs.setdefault(r0, []).append(b)
        except Exception:
            pass
    correct = build_correct_mapping(mapping)
    # 重建：所有图片清空，按 correct 重嵌
    ws._images = []
    for gidx, blist in sorted(correct.items()):
        for k, (bdata, fmt) in enumerate(blist):
            try:
                img = _Img(io.BytesIO(bdata))
                if img.width > 240:
                    ratio = 240.0 / img.width
                    img.width = int(img.width * ratio)
                    img.height = int(img.height * ratio)
                ws.add_image(img, 'G%d' % (gidx + 2))
                if k > 0:
                    img.anchor._from.colOff = k * 40 * 9525
            except Exception as e:
                print('  嵌入失败 row%d: %s' % (gidx, e))
    wb.save(book)
    wb.close()
    print('重建完成:', book, '| 图总数:', sum(len(v) for v in correct.values()))


def shutil_backup(book):
    import shutil
    bak = book + '.bak'
    if os.path.exists(bak):
        os.replace(bak, bak + '.old')
    shutil.copy2(book, bak)
    print('备份:', bak)


def main(argv):
    args = {}
    i = 0
    while i < len(argv):
        if argv[i] in ('--mapping', '--book'):
            args[argv[i][2:]] = argv[i + 1] if i + 1 < len(argv) else ''
            i += 1
        i += 1
    if not args.get('mapping'):
        print('用法: python rebuild_images_mapping.py --mapping <json> [--book <xlsx>]')
        return 1
    mapping = json.load(open(args['mapping'], encoding='utf-8'))
    rebuild(args.get('book') or BOOK, mapping)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
