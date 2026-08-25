# -*- coding: utf-8 -*-
import re, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
s = open(r'E:\workaaa\shengchanguanli\temp\diag_dom.html', encoding='utf-8', errors='replace').read()
m = re.search(r'<section id="moduleRoot"[^>]*>(.*?)</section>', s, re.S)
body = m.group(1) if m else ''
imgs = re.findall(r'<img class="dh-thumb"[^>]*>', body)
print('img count:', len(imgs))
for im in imgs:
    src_m = re.search(r'src="([^"]+)"', im)
    src = src_m.group(1) if src_m else ''
    dr_m = re.search(r'data-row="([^"]*)"', im)
    dr = dr_m.group(1) if dr_m else ''
    di_m = re.search(r'data-i="([^"]*)"', im)
    di = di_m.group(1) if di_m else ''
    row_m = re.search(r'row=(\d+)', src)
    urlr = row_m.group(1) if row_m else '?'
    print('  data-row=%s data-i=%s URL-row=%s URL-tail=%s' % (dr, di, urlr, src[-80:]))
print()
# 找每个 .dh-row 容器的父 .card 包含的 meta-line 顺序
rows = re.findall(r'<div class="dh-row" data-key="([^"]*)">(.*?)</div></div>', body, re.S)
print('dh-row count:', len(rows))
for k, inner in rows:
    date_m = re.search(r'<b>(\d{4}-\d{2}-\d{2})</b>', inner)
    line_m = re.search(r'<b>[^<]+</b> · ([^<·]+) ·', inner)
    model_m = re.search(r'<b>(HYXC[^<]+)</b>', inner)
    img_m = re.search(r'<img class="dh-thumb"[^>]*>', inner)
    print('  key=%s | date=%s line=%s model=%s hasImg=%s' % (
        k[:30], date_m.group(1) if date_m else '-', line_m.group(1) if line_m else '-',
        model_m.group(1) if model_m else '-', bool(img_m)))