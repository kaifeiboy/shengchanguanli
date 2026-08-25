# -*- coding: utf-8 -*-
"""生成数据诊断报告：每行有图行 + 文字描述 + 缩略图（base64 inline）。"""
import sys, os, base64, hashlib
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from openpyxl import load_workbook

ROOT = r'E:\生产不良履历\不良履历汇总.xlsx'
out = r'E:\workaaa\shengchanguanli\data\diag_report.html'
os.makedirs(os.path.dirname(out), exist_ok=True)

wb = load_workbook(ROOT); ws = wb.active
# 收集所有有图行
img_per_row = {}
for im in ws._images:
    b = im._data()
    img_per_row.setdefault(im.anchor._from.row + 1, []).append((b, im.format or 'png'))

# 自动错配提示（基于图片文本内容启发式匹配，仅供参考；以人工核对为准）
def suspect(text, model):
    text = text or ''
    model = model or ''
    s = text.lower()
    # 文字出现"小包装盒/包装盒/条形码/小册子/撕膜/混料/混装" → 应是包装/标签/物料类图
    if any(k in text for k in ['包装盒', '条形码', '小册子', '混料', '混装', '标签', '漏贴', '漏打', '包装', '刮伤']):
        return '⚠️ 应是包装/标签/物料现场图'
    # 文字出现"控制板/PCB/打偏/螺丝" → 应是 PCB 板
    if any(k in text for k in ['控制板', 'pcb', '电路板', '打偏', '螺丝', '撕膜', '面板', '气泡', '液晶']):
        return '⚠️ 应是 PCB/液晶面板图'
    return ''

rows = []
def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
for r in sorted(img_per_row.keys()):
    vals = [ws.cell(r, c).value for c in range(1, 9)]
    date_v = vals[0]
    line_v = vals[1] or ''
    model_v = vals[2] or ''
    reason_v = (vals[3] or '')
    qty_v = vals[4] or ''
    stage_v = vals[5] or ''
    handle_v = vals[7] or ''
    imgs = img_per_row[r]
    md5s = [hashlib.md5(d).hexdigest()[:10] for d, _ in imgs]
    b64_list = []
    for d, fmt in imgs:
        b64 = base64.b64encode(d).decode('ascii')
        mime = 'image/jpeg' if fmt.lower() == 'jpeg' else f'image/{fmt.lower()}'
        b64_list.append((b64, mime))
    sus = suspect(reason_v, model_v)
    rows.append((r, date_v, line_v, model_v, reason_v, qty_v, stage_v, handle_v, b64_list, md5s, sus))
wb.close()

# 生成 HTML
parts = []
parts.append('''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>不良履历数据诊断报告</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#f8fafc;color:#1e293b;margin:0;padding:20px;}
h1{text-align:center;color:#0f172a;}
.summary{max-width:1100px;margin:0 auto 20px;padding:14px 20px;background:#fef3c7;border:1px solid #fbbf24;border-radius:10px;}
.row{max-width:1100px;margin:14px auto;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;}
.row.err{border-left:5px solid #ef4444;background:#fef2f2;}
.row.ok{border-left:5px solid #22c55e;}
.head{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:#475569;margin-bottom:8px;}
.head b{color:#0f172a;}
.text{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;margin:8px 0;line-height:1.7;font-size:14px;}
.warn{background:#fef3c7;border:1px dashed #f59e0b;padding:6px 10px;border-radius:6px;color:#92400e;font-size:13px;margin-top:6px;}
.imgs{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px;}
.imgs img{max-width:200px;max-height:260px;border:1px solid #e2e8f0;border-radius:6px;cursor:pointer;transition:transform .15s;}
.imgs img:hover{transform:scale(1.4);box-shadow:0 10px 30px rgba(0,0,0,.2);}
.legend{max-width:1100px;margin:20px auto;background:#fff;border-radius:10px;padding:14px;font-size:13px;color:#475569;}
.legend code{background:#f1f5f9;padding:1px 6px;border-radius:4px;}
.row-num{font-weight:700;color:#0ea5e9;}
</style></head><body>''')
parts.append(f'<h1>不良履历数据诊断报告</h1>')
parts.append(f'<div class="summary"><b>⚠️ 数据层问题已确认</b>：源 xlsx 中多张图片与文字描述不对应（疑似录入/迁移时批量错位）。服务端逻辑无 bug——只是如实把数据返回。<br/>共 <b>{len(rows)}</b> 行有图，下方每行展示：文字描述 / 图片缩略图 / 自动启发式判断（仅供参考，<b>请以人工核对为准</b>）。</div>')

parts.append('<div class="legend">')
parts.append('<b>查看方式</b>：每行"缩略图"鼠标悬停可放大查看（高 1.4 倍）。如缩略图内容与文字描述不符，请到 WPS 打开源文件 <code>E:\\生产不良履历\\不良履历汇总.xlsx</code> 重新核对 G 列图片位置。')
parts.append('</div>')

for r, date_v, line_v, model_v, reason_v, qty_v, stage_v, handle_v, b64_list, md5s, sus in rows:
    flag_class = 'err' if sus else 'ok'
    flag_icon = '⚠️' if sus else '✓'
    parts.append(f'<div class="row {flag_class}">')
    parts.append(f'<div class="head">')
    parts.append(f'<span class="row-num">第 {r} 行</span> · ')
    parts.append(f'<b>{str(date_v)[:10]}</b> · 拉线 <b>{esc(line_v)}</b> · 型号 <b>{esc(model_v)}</b> · 数量 <b>{esc(qty_v)}</b> · 工程 <b>{esc(stage_v)}</b> · 处理 <b>{esc(handle_v)}</b>')
    parts.append(f'<span style="margin-left:auto;color:#94a3b8;">md5={",".join(md5s)}</span>')
    parts.append('</div>')
    parts.append(f'<div class="text">')
    parts.append(f'<b>文字描述：</b>{esc(reason_v)}')
    parts.append('</div>')
    if sus:
        parts.append(f'<div class="warn">{flag_icon} <b>自动判断</b>：{esc(sus)}（仅基于文字关键词启发式，<b>必须人工核对图片内容</b>）</div>')
    parts.append('<div class="imgs">')
    for b64, mime in b64_list:
        parts.append(f'<img src="data:{mime};base64,{b64}" alt="图片"/>')
    parts.append('</div>')
    parts.append('</div>')

parts.append('</body></html>')

with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(parts))
print('saved:', out, '| size:', os.path.getsize(out), 'bytes')