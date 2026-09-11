#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接测试 ocr_worker 处理中文产品名称"""

import subprocess
import json
import os
import sys

# 设置环境变量
env = os.environ.copy()
env["OCR_PRODUCT_NAME"] = "VQ图纸"
env["PYTHONIOENCODING"] = "utf-8"

# 启动 worker
proc = subprocess.Popen(
    ["python", "src/Platform/Modules/Drawings/ocr_worker.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    env=env
)

# 读取 ready
ready_line = proc.stdout.readline()
print(f"Ready: {ready_line}")

# 发送请求（使用中文产品名称）
request = {
    "id": "test_vq",
    "image": os.path.abspath("test_vq_real.jpg"),
    "json": True,
    "product": "VQ图纸"
}
request_json = json.dumps(request, ensure_ascii=False)
print(f"Request: {request_json}")

proc.stdin.write(request_json + "\n")
proc.stdin.flush()

# 读取响应
response_line = proc.stdout.readline()
print(f"Response: {response_line}")

try:
    response = json.loads(response_line)
    print(f"\n解析结果:")
    print(f"  版本: {response.get('version')}")
    print(f"  产品: {response.get('product')}")
    
    if response.get('version') == 'optimized':
        print("\n[SUCCESS] VQ图纸正确使用了优化版本！")
    else:
        print(f"\n[FAIL] 版本错误: {response.get('version')}")
except Exception as e:
    print(f"[ERROR] 解析失败: {e}")

proc.stdin.close()
proc.wait(timeout=5)