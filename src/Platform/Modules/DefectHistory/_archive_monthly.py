# -*- coding: utf-8 -*-
import os, sys, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
root = r'E:\生产不良履历'
arch = os.path.join(root, '月度备份')
os.makedirs(arch, exist_ok=True)
n = 0
for f in sorted(glob.glob(os.path.join(root, '*.xlsx'))):
    base = os.path.basename(f)
    if base == '不良履历汇总.xlsx':
        continue
    dst = os.path.join(arch, base)
    if os.path.exists(dst):
        os.remove(dst)
    os.replace(f, dst)
    n += 1
old_img = os.path.join(root, '_images')
if os.path.isdir(old_img):
    os.replace(old_img, os.path.join(root, '_images_月度归档'))
print('归档 %d 个月度文件 -> 月度备份' % n)
print('目录现状:', sorted(os.listdir(root)))
