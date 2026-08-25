# -*- coding: utf-8 -*-
"""Spike 验证：对 data/defecthistory_test 下全部 5 个 .xls 运行提取器，汇总提取率/定位率。"""
import sys, os, json, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xls_img_extract as X

TEST_DIR = r'E:\workaaa\shengchanguanli\data\defecthistory_test'
OUT_DIR = r'E:\workaaa\shengchanguanli\data\defecthistory_test_img'
os.makedirs(OUT_DIR, exist_ok=True)

total_imgs = 0
ok_files = 0
all_warnings = []
for f in sorted(glob.glob(os.path.join(TEST_DIR, '*.xls'))):
    res = X.analyze_file(f, outdir=OUT_DIR)
    if not res.get('success'):
        print('FAIL', os.path.basename(f), res.get('error'))
        continue
    imgs = [im for s in res['sheets'] for im in s['images']]
    total_imgs += len(imgs)
    rows_info = []
    for s in res['sheets']:
        n = len(s['images'])
        rows_info.append('%s:%d图' % (s['name'], n))
    status = 'OK' if len(imgs) == res['totalBlips'] else 'MISMATCH(shape=%d blips=%d)' % (len(imgs), res['totalBlips'])
    if status == 'OK':
        ok_files += 1
    print('%-40s blips=%-3d %-18s %s' % (os.path.basename(f), res['totalBlips'], ' '.join(rows_info), status))
    for w in res['warnings']:
        all_warnings.append(os.path.basename(f) + ': ' + w)

print('----')
print('文件数 OK:', ok_files, '/ 5, 提取图片总数:', total_imgs)
print('warnings:', len(all_warnings))
for w in all_warnings[:20]:
    print('  ', w)
