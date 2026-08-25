# -*- coding: utf-8 -*-
"""生成「图片归属核对报告」：原始 xls 每张图 + 建议归属行 + 数据行内容，供用户在 WPS 中核对。"""
import sys, os, glob, base64, hashlib, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
import xls_img_extract as X
import xlrd

out = r'E:\workaaa\shengchanguanli\data\image_audit.html'

files = sorted(glob.glob(r'E:\生产不良履历_迁移前备份\*.xls'))
parts = []
parts.append('''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>原始文件图片归属核对报告</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#f8fafc;color:#1e293b;margin:0;padding:20px;}
h1{text-align:center;color:#0f172a;}
.summary{max-width:1100px;margin:0 auto 20px;padding:14px 20px;background:#fef3c7;border:1px solid #fbbf24;border-radius:10px;}
.file{max-width:1100px;margin:16px auto;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px;}
.file h2{margin:0 0 6px;font-size:16px;color:#0f172a;}
.sub{margin:2px 0 10px;color:#475569;font-size:13px;}
.item{display:flex;gap:14px;align-items:flex-start;border-top:1px solid #f1f5f9;padding:10px 0;}
.item .idx{width:56px;flex:none;font-weight:700;color:#0ea5e9;font-size:14px;}
.item .pic{flex:none;}
.item .pic img{max-width:150px;max-height:200px;border:1px solid #e2e8f0;border-radius:6px;cursor:pointer;transition:transform .15s;background:#fff;}
.item .pic img:hover{transform:scale(2.2);box-shadow:0 10px 30px rgba(0,0,0,.25);position:relative;z-index:10;}
.item .info{flex:1;font-size:13px;line-height:1.7;color:#334155;}
.item .info b{color:#0f172a;}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11.5px;margin-left:6px;}
.tag.warn{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;}
.tag.ok{background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;}
</style></head><body>''')
parts.append('<h1>原始文件（迁移前备份）图片归属核对报告</h1>')
parts.append('''<div class="summary"><b>说明</b>：程序提取原始 xls 时采用「位置映射」（第 i 张图 → 数据行 2+i），但已证明该映射在 WPS 文件中不可靠（OCR 检出图#0 为 PC-P1HVQA 包装盒却被放到 YCWA15NCWQ 行）。<br/>
请在 WPS 中打开对应原始 xls，逐张核对「图中内容」应归属哪个「日期/型号」行，把错误的行号记下反馈。</div>''')

for fi, f in enumerate(files):
    base = os.path.basename(f)
    res = X.analyze_file(f, outdir=r'E:\workaaa\shengchanguanli\temp\audit_%d' % fi)
    wb = xlrd.open_workbook(f)
    parts.append('<div class="file">')
    parts.append('<h2>📄 %s</h2>' % base)
    for si, s in enumerate(res['sheets']):
        sname = s['name']
        if sname not in ('组装', 'SMT'):
            continue
        ws = wb.sheet_by_name(sname)
        data_rows = []
        for r in range(2, ws.nrows):
            vals = [str(ws.cell_value(r, c)).strip() for c in range(8)]
            if all(not v for v in vals):
                continue
            data_rows.append((r, vals))
        parts.append('<div class="sub">Sheet「%s」：%d 行数据，%d 张图</div>' % (sname, len(data_rows), len(s['images'])))
        for i, im in enumerate(s['images']):
            # 位置映射行
            mapped_row = im['row']
            mapped_vals = None
            for r, vals in data_rows:
                if r == mapped_row:
                    mapped_vals = vals
                    break
            b64 = base64.b64encode(open(im['file'], 'rb').read()).decode('ascii')
            parts.append('<div class="item">')
            parts.append('<div class="idx">图#%d</div>' % i)
            parts.append('<div class="pic"><img src="data:image/png;base64,%s"/></div>' % b64)
            parts.append('<div class="info">')
            if mapped_vals:
                parts.append('<b>位置映射 → 行%d</b>：%s ｜ %s ｜ %s %s <span class="tag warn">请核对</span><br/>' %
                             (mapped_row, mapped_vals[0], mapped_vals[1], mapped_vals[2], mapped_vals[3][:30]))
            else:
                parts.append('<b>位置映射 → 行%d（无数据）</b><span class="tag warn">请核对</span><br/>' % mapped_row)
            parts.append('</div>')
            parts.append('</div>')
    parts.append('</div>')
    wb.release_resources()

parts.append('</body></html>')
with open(out, 'w', encoding='utf-8') as fh:
    fh.write('\n'.join(parts))
print('saved:', out, '| size:', os.path.getsize(out))
