# -*- coding: utf-8:utf-8 -*-
"""诊断：把导出 xlsx 中每行的图片存出来，肉眼验证图片内容与文字描述是否一致。
关键：用 PIL 把图保存为文件，然后打印每行 + 对应图片的视觉特征（尺寸、md5、EXIF），
便于后续视觉对比（用户可双击 PNG 查看）。"""
import sys, os, hashlib
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from openpyxl import load_workbook

path = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\Administrator\Desktop\不良履历导出_20260807_190906.xlsx'
out_dir = r'E:\workaaa\shengchanguanli\temp\user_export_imgs'
os.makedirs(out_dir, exist_ok=True)

wb = load_workbook(path)
print('Sheet:', wb.sheetnames, '| max_row:', max(ws.max_row for ws in wb.worksheets))
ws = wb.active
print('实际 sheet:', ws.title, '| max_row:', ws.max_row)

# 数据行：列 A(日期) B(线别) C(型号) D(原因) E(数量) F(工程) G(图) H(处理)
# 锚点：openpyxl 0-based row；1-based row = anchor.row + 1
img_per_row = {}
for im in ws._images:
    # 立即读取 bytes 到内存（避免 _data() 多次调用因 ref 关闭而失败）
    data_bytes = im._data()
    img_per_row.setdefault(im.anchor._from.row + 1, []).append((data_bytes, im.format or 'png'))

print()
print('=== 每行数据 + 该行图片（保存以便肉眼对比） ===')
for r in range(2, ws.max_row + 1):
    vals = [ws.cell(r, c).value for c in range(1, 9)]
    if all(v is None or str(v).strip() == "" for v in vals):
        continue
    date_v = vals[0]
    line_v = vals[1] or ""
    model_v = vals[2] or ""
    reason_v = (vals[3] or "")[:30]
    qty_v = vals[4] or ""
    stage_v = vals[5] or ""
    handle_v = vals[7] or ""
    imgs = img_per_row.get(r, [])
    md5s = [hashlib.md5(d).hexdigest()[:10] for d, _ in imgs]
    print('  r%-3d | %-12s | %-6s | %-12s | %-5s | %-5s | 图=%d %s'
          % (r, str(date_v)[:10], str(line_v), str(model_v)[:12],
             str(qty_v), str(stage_v), len(imgs), md5s))
    print('    reason: %s | handle: %s' % (reason_v, handle_v))
    for k, (bdata, fmt) in enumerate(imgs):
        ext = fmt.lower()
        if ext == 'jpeg': ext = 'jpg'
        out = os.path.join(out_dir, 'row%d_%d.%s' % (r, k, ext))
        with open(out, 'wb') as fh:
            fh.write(bdata)
        print('    saved: %s' % out)
wb.close()