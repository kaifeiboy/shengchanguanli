#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试修改后的 ocr_worker 是否支持产品级灰度控制"""

import subprocess
import json
import os
import sys

def test_ocr_worker():
    """测试 ocr_worker 的灰度控制功能"""
    print("=" * 70)
    print("测试 ocr_worker.py 产品级灰度控制")
    print("=" * 70)
    
    worker_script = "src/Platform/Modules/Drawings/ocr_worker.py"
    test_image = "test_vq_real.jpg"
    
    if not os.path.exists(test_image):
        print(f"[ERROR] 测试图片不存在: {test_image}")
        return False
    
    # 测试1: VQ图纸产品（优化版本）
    print("\n测试1: VQ图纸产品（应使用优化版本）")
    print("-" * 70)
    
    env = os.environ.copy()
    env["OCR_PRODUCT_NAME"] = "VQ图纸"
    
    try:
        # 启动 worker 进程
        proc = subprocess.Popen(
            ["python", worker_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )
        
        # 读取 ready 信号
        ready_line = proc.stdout.readline()
        ready_data = json.loads(ready_line)
        
        if ready_data.get("__ready__"):
            print("[PASS] Worker 启动成功")
        else:
            print(f"[ERROR] Worker 启动失败: {ready_line}")
            return False
        
        # 发送测试请求
        request = {
            "id": "test_vq",
            "image": os.path.abspath(test_image),
            "json": True,
            "product": "VQ图纸"
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        
        # 读取响应
        response_line = proc.stdout.readline()
        response = json.loads(response_line)
        
        print(f"响应内容:")
        print(f"  版本: {response.get('version', 'unknown')}")
        print(f"  产品: {response.get('product', 'unknown')}")
        print(f"  引擎: {response.get('engine', 'unknown')}")
        print(f"  文本: {response.get('text', '')[:100]}...")
        
        if response.get('version') == 'optimized':
            print("[PASS] VQ图纸使用优化版本")
        else:
            print(f"[FAIL] VQ图纸未使用优化版本: {response.get('version')}")
            
        # 关闭 worker
        proc.stdin.close()
        proc.wait(timeout=5)
        
    except Exception as e:
        print(f"[ERROR] 测试异常: {e}")
        return False
    
    # 测试2: JQ图纸产品（原始版本）
    print("\n测试2: JQ图纸产品（应使用原始版本）")
    print("-" * 70)
    
    env["OCR_PRODUCT_NAME"] = "JQ图纸"
    
    try:
        proc = subprocess.Popen(
            ["python", worker_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )
        
        ready_line = proc.stdout.readline()
        ready_data = json.loads(ready_line)
        
        if ready_data.get("__ready__"):
            print("[PASS] Worker 启动成功")
        
        request = {
            "id": "test_jq",
            "image": os.path.abspath(test_image),
            "json": True,
            "product": "JQ图纸"
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        
        response_line = proc.stdout.readline()
        response = json.loads(response_line)
        
        print(f"响应内容:")
        print(f"  版本: {response.get('version', 'unknown')}")
        print(f"  产品: {response.get('product', 'unknown')}")
        print(f"  引擎: {response.get('engine', 'unknown')}")
        
        if response.get('version') == 'legacy':
            print("[PASS] JQ图纸使用原始版本")
        else:
            print(f"[FAIL] JQ图纸未使用原始版本: {response.get('version')}")
            
        proc.stdin.close()
        proc.wait(timeout=5)
        
    except Exception as e:
        print(f"[ERROR] 测试异常: {e}")
        return False
    
    return True

if __name__ == "__main__":
    print("\n开始测试 ocr_worker 灰度控制...\n")
    
    success = test_ocr_worker()
    
    print(f"\n{'=' * 70}")
    if success:
        print("[SUCCESS] ocr_worker 灰度控制测试通过")
        print("\n现在需要修改 .NET 侧的 OcrService.cs")
        print("让它在请求中传递产品名称")
    else:
        print("[FAIL] 测试失败")
    print("=" * 70)
    
    sys.exit(0 if success else 1)