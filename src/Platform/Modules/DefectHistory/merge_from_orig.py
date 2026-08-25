# -*- coding: utf-8 -*-
"""
merge_from_orig.py —— 从原始 .xls（迁移前备份）重建汇总工作簿（v3.1，OCR 增强图片归属）

规则（不改变既有合并规则）：
  1. 数据源 = E:\生产不良履历_迁移前备份\*.xls（原始文件，唯一标准）
  2. SMT 工作表数据一律舍弃
  3. 日期标准化 yyyy-mm-dd（含 5 位年份容错），全表按日期升序
  4. 表头 8 列 + 全格框线 + 列宽 + 标题行（完整表格样式）
  5. **每个数据行一条记录**（无论有无图片）——v3.1 修复
  6. 图片归属：OCR 识别图内型号 → 命中该文件数据行型号 → 归该行；
     OCR 无法判定 → 保持位置映射（第 i 张 → 数据行 2+i）
  7. 图片锚定 G 列对应数据行（openpyxl 原生嵌入，PNG 原图不损画质）
输出：E:\生产不良履历\不良履历汇总.xlsx
"""
import sys, os, io, glob, json, re, datetime
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xls_img_extract as X
import xlrd
import numpy as np
from PIL import Image as PILImage
from rapidocr_onnxruntime import RapidOCR
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.drawing.image import Image as XlImage
from openpyxl.utils import get_column_letter

ORIG_DIR = r'E:\生产不良履历_迁移前备份'
OUT_BOOK = r'E:\生产不良履历\不良履历汇总.xlsx'
SHEET_NAME = '不良履历'
HEADERS = ['日期', '线别', '型号', '问题原因', '数量', '发生工程', '不良图片', '处理方式']

ocr = RapidOCR()


def norm(s):
    return (s or '').upper().replace(' ', '').replace('_', '').replace('-', '')


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
    # 5 位年份容错：20226 → 20 + "26" = 2026（丢弃第 3 位冗余数字）
    if len(ys) == 5 and ys.startswith('20') and ys[2] == '2':
        ys = ys[:2] + ys[3:]
    try:
        y, mo, d = int(ys), int(mo_s), int(d_s)
        return datetime.date(y, mo, d)
    except (ValueError, OverflowError):
        return None


def ocr_text(bdata):
    try:
        im = PILImage.open(io.BytesIO(bdata))
        arr = np.array(im.convert('RGB'))
        res, _ = ocr(arr)
        return [l[1] for l in res] if res else []
    except Exception:
        return []


def read_sheet_rows(ws):
    """组装 sheet 数据行：[(xlrd行号, [8值])]"""
    rows = []
    for r in range(2, ws.nrows):
        vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
        if all(not v for v in vals):
            continue
        if any(v.startswith('合计') for v in vals):
            continue
        rows.append((r, vals))
    return rows


def main():
    files = sorted(glob.glob(os.path.join(ORIG_DIR, '*.xls')))
    print('原始文件:', len(files))

    all_records = []  # {date, vals, file, img(可空)}
    warnings = []
    total_imgs = 0
    ocr_assigned = 0

    for f in files:
        base = os.path.basename(f)
        wb = xlrd.open_workbook(f)
        if '组装' not in wb.sheet_names():
            print('  跳过(无组装sheet):', base)
            continue
        ws = wb.sheet_by_name('组装')
        rows = read_sheet_rows(ws)

        # 提取图片（位置映射给出初始行）
        res = X.analyze_file(f, outdir=r'E:\workaaa\shengchanguanli\temp\mimg\%s' % base[:10])
        asm = [s for s in res['sheets'] if s['name'] == '组装'][0]
        imgs = asm['images']

        # 每张图 OCR
        img_ocr = []
        for i, im in enumerate(imgs):
            bdata = open(im['file'], 'rb').read()
            texts = ocr_text(bdata)
            joined = norm(' '.join(texts))
            img_ocr.append((i, im, bdata, joined))

        # 决策图片归属（支持一行多图）：
        # 核心思路：OCR 图内出现某数据行"完整型号"时，视为该图属于该型号行；
        # 同型号多图按顺序分配；无 OCR 型号的图走位置映射兜底。
        row_model = {r: norm(vals[2]) for r, vals in rows}
        # 每个型号对应的数据行列表（保留原始顺序）
        model_rows = {}
        for r, vals in rows:
            model_rows.setdefault(row_model[r], []).append(r)

        # 先 OCR 全部图，得到每张图的型号候选
        # 匹配规则：
        #  A) 完整型号出现在图内（如 PCP1HVQA）
        #  B) 型号前缀 4 字符出现在图内（如 YCWA → 处理 OCR 误读 YCWASHCWO），且该型号在本文件唯一
        ocr_model_of = {}  # 图索引 -> 命中的型号(norm)
        for i, im, bdata, joined in img_ocr:
            best = None
            best_len = 0
            for r, vals in rows:
                mn = row_model[r]
                if not mn or len(mn) < 5:
                    continue
                # A) 完整匹配
                if mn in joined and len(mn) > best_len:
                    best, best_len = mn, len(mn)
            # B) 前缀匹配（4字符），候选唯一才采用
            if best is None:
                prefix_candidates = set()
                for r, vals in rows:
                    mn = row_model[r]
                    if mn and len(mn) >= 8 and mn[:4] in joined:
                        prefix_candidates.add(mn)
                if len(prefix_candidates) == 1:
                    best = prefix_candidates.pop()
            if best:
                ocr_model_of[i] = best

        # 分配：型号匹配优先（每个型号行池按顺序吃图）
        assigned = {}      # 图索引 -> xlrd行号
        model_used = {}    # 型号 -> 已用行索引
        for i, im, bdata, joined in img_ocr:
            mn = ocr_model_of.get(i)
            if mn and mn in model_rows:
                pool = model_rows[mn]
                k = model_used.get(mn, 0)
                if k < len(pool):
                    assigned[i] = pool[k]
                    model_used[mn] = k + 1
                    ocr_assigned += 1
                    continue
            # 兜底：位置映射
            pm = 2 + i
            if pm in row_model:
                assigned[i] = pm
            elif i < len(rows):
                assigned[i] = rows[i][0]

        # 每行图片映射：xlrd行号 -> [[bdata, fmt], ...]
        row_img = {}
        for i, (idx, im, bdata, joined) in enumerate(img_ocr):
            r = assigned.get(idx)
            if r is not None:
                row_img.setdefault(r, []).append([bdata, im['fmt']])

        # 组装：每个数据行一条记录
        for r, vals in rows:
            d = parse_date(vals[0])
            if d is None:
                warnings.append('%s 行%d 日期解析失败: %s' % (base, r, vals[0]))
                continue
            rec = {'date': d, 'vals': vals, 'file': base}
            if r in row_img and row_img[r]:
                rec['imgs'] = row_img[r]
                total_imgs += 1
            all_records.append(rec)
        wb.release_resources()

    # 按日期升序
    all_records.sort(key=lambda x: x['date'])
    print('记录总数:', len(all_records), '| 图片总数:', total_imgs, '| OCR指派:', ocr_assigned)

    # ---------- 写汇总工作簿 ----------
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

    for c, w in enumerate([14, 10, 18, 44, 12, 12, 20, 14], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w

    used_imgs = 0
    for i, rec in enumerate(all_records):
        xr = 3 + i
        vals = rec['vals']
        ws.cell(xr, 1, rec['date']).number_format = 'yyyy-mm-dd'
        for c in range(2, 9):
            v = vals[c - 1] if c - 1 < len(vals) else ''
            ws.cell(xr, c, v if v not in ('', None) else None)
        for c in range(1, 9):
            ws.cell(xr, c).border = BORDER
        if rec.get('imgs'):
            for k, (bdata, fmt) in enumerate(rec['imgs']):
                try:
                    im = XlImage(io.BytesIO(bdata))
                    if im.width > 240:
                        ratio = 240.0 / im.width
                        im.width = int(im.width * ratio)
                        im.height = int(im.height * ratio)
                    # 手动构造锚点（openpyxl 3.1.5 Image 无 drawing 属性，用 XDRCoordinate 像素 EMU）
                    from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker, XDRPositiveSize2D
                    marker = AnchorMarker(col=6, row=xr - 1, colOff=k * 60 * 9525, rowOff=0)
                    size = XDRPositiveSize2D(cx=int(im.width * 9525), cy=int(im.height * 9525))
                    im.anchor = OneCellAnchor(_from=marker, ext=size)
                    ws.add_image(im)
                    used_imgs += 1
                except Exception as e:
                    warnings.append('嵌入失败 行%d: %s' % (xr, e))

    os.makedirs(os.path.dirname(OUT_BOOK), exist_ok=True)
    wb.save(OUT_BOOK)
    wb.close()
    print('已生成:', OUT_BOOK)
    print('数据行:', len(all_records), '| 已嵌入图片:', used_imgs, '| 警告:', len(warnings))
    if warnings:
        print('--- 警告 ---')
        for w in warnings[:15]:
            print(' ', w)


if __name__ == '__main__':
    main()
