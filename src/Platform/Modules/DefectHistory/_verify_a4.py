# -*- coding: utf-8 -*-
"""验证：标题格式 + A4 竖向打印设置"""
import sys, subprocess
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from openpyxl import load_workbook

out = r'E:\workaaa\shengchanguanli\temp\an_a4.xlsx'

# 1. 全型号
r1 = subprocess.run([r'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe',
                    r'E:\workaaa\shengchanguanli\src\Platform\Modules\DefectHistory\defect_history.py',
                    'analysis', '--root', r'E:\生产不良履历', '--month', '2026-03', '--out', out],
                   capture_output=True, text=True)
# 2. 指定型号（验证带型号后缀）
out2 = r'E:\workaaa\shengchanguanli\temp\an_a4_m.xlsx'
r2 = subprocess.run([r'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe',
                    r'E:\workaaa\shengchanguanli\src\Platform\Modules\DefectHistory\defect_history.py',
                    'analysis', '--root', r'E:\生产不良履历', '--month', '2026-01', '--model', 'FD01', '--out', out2],
                   capture_output=True, text=True)
# 3. 单月份（验证无前导零）
out3 = r'E:\workaaa\shengchanguanli\temp\an_a4_m3.xlsx'
r3 = subprocess.run([r'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe',
                    r'E:\workaaa\shengchanguanli\src\Platform\Modules\DefectHistory\defect_history.py',
                    'analysis', '--root', r'E:\生产不良履历', '--month', '2026-01', '--out', out3],
                   capture_output=True, text=True)

def show(path, label):
    wb = load_workbook(path); ws = wb.active
    title = ws.cell(1, 1).value
    ps = ws.page_setup
    out_lines = []
    out_lines.append('=== %s ===' % label)
    out_lines.append('  标题 (A1): %s' % title)
    out_lines.append('  期望格式: YYYY年M月品质数据[·全型号/·型号：XXX]')
    out_lines.append('  纸张方向: %s' % ('portrait ✓' if ps.orientation == 'portrait' else 'landscape/other(%s) ✗' % ps.orientation))
    out_lines.append('  纸张大小: %s (9=A4)' % ps.paperSize)
    out_lines.append('  fitToWidth/Height: %s / %s' % (ps.fitToWidth, ps.fitToHeight))
    out_lines.append('  fitToPage: %s' % ws.sheet_properties.pageSetUpPr.fitToPage)
    out_lines.append('  打印重复标题行: %s' % (ws.print_title_rows or '(未设)'))
    out_lines.append('  图表宽×高 (cm): %s × %s' % (ws._charts[0].width, ws._charts[0].height))
    out_lines.append('  页边距 L/R/T/B (cm): %s/%s/%s/%s' % (ws.page_margins.left, ws.page_margins.right, ws.page_margins.top, ws.page_margins.bottom))
    # 写到文件，绕过 stderr 编码
    with open(r'E:\workaaa\shengchanguanli\temp\verify_a4.txt', 'a', encoding='utf-8') as f:
        f.write('\n'.join(out_lines) + '\n')
    wb.close()

show(out, '全型号 2026-03')
show(out2, '指定型号 FD01 2026-01')
show(out3, '全型号 2026-01 (验证月份无前导零)')