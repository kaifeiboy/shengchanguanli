# -*- coding: utf-8 -*-
"""验证 resize_photo.py 修复：路径存在性 + C# 路径解析模拟 + 功能实测"""
import subprocess, shutil, os
from PIL import Image

PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
HERE = os.path.dirname(os.path.abspath(__file__))

expected = os.path.join(HERE, "resize_photo.py")
print(f"[1] 脚本存在: {os.path.exists(expected)}")
print(f"    {expected}")

for name in ["Debug", "Release"]:
    asm = os.path.join(r"E:\workaaa\shengchanguanli\src\Platform\bin", name, "net8.0")
    p = os.path.abspath(os.path.join(asm, "..", "..", "..", "Modules", "Drawings", "resize_photo.py"))
    print(f"[2] 模拟 C# 解析({name}): 存在={os.path.exists(p)}")

SRC = r"E:\workaaa\shengchanguanli\data\seg\116\_user_vq_photo.jpg"
TMP = r"E:\workaaa\shengchanguanli\data\seg\116\_resize_test.jpg"
shutil.copy(SRC, TMP)
before = Image.open(TMP).size
r = subprocess.run([PY, expected, TMP], capture_output=True, text=True)
after = Image.open(TMP).size
print(f"[3] 功能实测: {before} -> {after}  rc={r.returncode}")
if r.stderr.strip():
    print("    stderr:", r.stderr.strip()[:300])
print(f"[3] 判定: {'OK 降采样生效(max<=960)' if max(after) <= 960 else 'FAIL 未生效'}")
os.remove(TMP)
