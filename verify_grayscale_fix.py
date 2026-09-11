#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证灰度测试修复是否生效
测试 ocr_worker.py 是否正确接收产品名称并路由到对应版本
"""

import subprocess
import json
import os
import sys
import time

def test_worker_with_product():
    """测试 ocr_worker 是否正确处理产品名称"""
    print("=" * 70)
    print("验证 ocr_worker.py 产品级灰度控制")
    print("=" * 70)
    
    test_image = "test_vq_real.jpg"
    if not os.path.exists(test_image):
        print(f"[ERROR] 测试图片不存在: {test_image}")
        return False
    
    env = os.environ.copy()
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
    print(f"Worker ready: {ready_line.strip()}")
    
    # 测试1: VQ图纸产品（应使用优化版本）
    print("\n测试1: VQ图纸产品")
    print("-" * 70)
    
    request1 = {
        "id": "test_vq_001",
        "image": os.path.abspath(test_image),
        "json": True,
        "product": "VQ图纸"
    }
    
    proc.stdin.write(json.dumps(request1, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    
    response1_line = proc.stdout.readline()
    print(f"Response: {response1_line.strip()}")
    
    try:
        resp1 = json.loads(response1_line)
        print(f"  版本: {resp1.get('version')}")
        print(f"  产品: {resp1.get('product')}")
        print(f"  引擎: {resp1.get('engine')}")
        
        if resp1.get('version') == 'optimized':
            print("  [PASS] VQ图纸正确路由到优化版本")
            test1_pass = True
        else:
            print(f"  [FAIL] 版本错误，期望 optimized，实际 {resp1.get('version')}")
            test1_pass = False
    except Exception as e:
        print(f"  [ERROR] 解析失败: {e}")
        test1_pass = False
    
    # 测试2: JQ图纸产品（应使用原始版本）
    print("\n测试2: JQ图纸产品")
    print("-" * 70)
    
    request2 = {
        "id": "test_jq_001",
        "image": os.path.abspath(test_image),
        "json": True,
        "product": "JQ图纸"
    }
    
    proc.stdin.write(json.dumps(request2, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    
    response2_line = proc.stdout.readline()
    print(f"Response: {response2_line.strip()}")
    
    try:
        resp2 = json.loads(response2_line)
        print(f"  版本: {resp2.get('version')}")
        print(f"  产品: {resp2.get('product')}")
        print(f"  引擎: {resp2.get('engine')}")
        
        if resp2.get('version') == 'legacy':
            print("  [PASS] JQ图纸正确路由到原始版本")
            test2_pass = True
        else:
            print(f"  [FAIL] 版本错误，期望 legacy，实际 {resp2.get('version')}")
            test2_pass = False
    except Exception as e:
        print(f"  [ERROR] 解析失败: {e}")
        test2_pass = False
    
    # 关闭 worker
    proc.stdin.close()
    proc.wait(timeout=5)
    
    # 总结
    print("\n" + "=" * 70)
    print("测试结果总结")
    print("=" * 70)
    
    if test1_pass and test2_pass:
        print("[SUCCESS] 所有测试通过！")
        print("\n灰度控制已修复：")
        print("  - VQ图纸 → 优化版本（含智能预处理）")
        print("  - JQ图纸 → 原始版本（稳定逻辑）")
        print("\n现在需要：")
        print("  1. 编译 .NET 项目")
        print("  2. 在真实环境中测试 H5 调用")
        print("  3. 验证匹配标示准确率是否提升")
        return True
    else:
        print("[FAIL] 部分测试失败")
        return False

if __name__ == "__main__":
    success = test_worker_with_product()
    sys.exit(0 if success else 1)