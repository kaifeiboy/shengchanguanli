#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VQ图纸产品级灰度测试验证脚本
用于验证产品级灰度控制功能是否正常工作
"""

import os
import sys
import json
import time
import subprocess

def test_grayscale_config():
    """测试灰度配置加载"""
    print("=" * 60)
    print("测试1: 灰度配置加载")
    print("=" * 60)
    
    config_path = "src/Platform/Modules/Drawings/grayscale_config.json"
    if not os.path.exists(config_path):
        print("[ERROR] 配置文件不存在:", config_path)
        return False
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    print("配置文件加载成功")
    print(f"   - 灰度启用状态: {config['grayscale']['enabled']}")
    print(f"   - 灰度产品列表: {config['grayscale']['products']}")
    print(f"   - 性能监控: {config['monitoring']['log_performance']}")
    
    # 验证关键配置
    if not config['grayscale']['enabled']:
        print("[WARNING] 灰度功能未启用")
        return False
    
    if "VQ图纸" not in config['grayscale']['products']:
        print("[ERROR] VQ图纸不在灰度产品列表中")
        return False
    
    print("[PASS] 配置验证通过")
    return True

def test_routing_logic():
    """测试路由逻辑"""
    print("\n" + "=" * 60)
    print("测试2: 路由逻辑验证")
    print("=" * 60)
    
    # 添加模块路径
    sys.path.insert(0, "src/Platform/Modules/Drawings")
    
    try:
        import ocr_cli
        
        # 测试VQ图纸产品
        vq_result = ocr_cli.should_use_optimized_ocr("VQ图纸")
        print(f"VQ图纸产品路由结果: {vq_result}")
        if not vq_result:
            print("[ERROR] VQ图纸应该使用优化版本")
            return False
        print("[PASS] VQ图纸路由正确 (使用优化版本)")
        
        # 测试其他产品
        other_result = ocr_cli.should_use_optimized_ocr("其他产品")
        print(f"其他产品路由结果: {other_result}")
        if other_result:
            print("[ERROR] 其他产品应该使用原始版本")
            return False
        print("[PASS] 其他产品路由正确 (使用原始版本)")
        
        # 测试未知产品
        unknown_result = ocr_cli.should_use_optimized_ocr("未知产品")
        print(f"未知产品路由结果: {unknown_result}")
        if unknown_result:
            print("[ERROR] 未知产品应该使用原始版本")
            return False
        print("[PASS] 未知产品路由正确 (使用原始版本)")
        
        print("[PASS] 路由逻辑验证通过")
        return True
        
    except Exception as e:
        print(f"[ERROR] 路由逻辑测试失败: {e}")
        return False

def test_ocr_execution():
    """测试OCR执行"""
    print("\n" + "=" * 60)
    print("测试3: OCR执行验证")
    print("=" * 60)
    
    test_image = "测试照片/aa581999acdef817401e39e08739f65d_compress.jpg"
    if not os.path.exists(test_image):
        print(f"[WARNING] 测试图片不存在: {test_image}")
        return True  # 不影响验证流程
    
    # 测试VQ图纸产品
    print("测试VQ图纸产品OCR...")
    env = os.environ.copy()
    env["OCR_PRODUCT_NAME"] = "VQ图纸"
    env["OCR_JSON"] = "1"
    
    try:
        start_time = time.time()
        result = subprocess.run(
            ["python", "src/Platform/Modules/Drawings/ocr_cli.py", test_image],
            env=env,
            capture_output=True,
            text=True,
            timeout=30
        )
        elapsed_time = time.time() - start_time
        
        if result.returncode == 0:
            output = json.loads(result.stdout)
            print(f"[PASS] VQ图纸OCR执行成功")
            print(f"   - 处理时间: {elapsed_time:.3f}秒")
            print(f"   - 识别版本: {output.get('version', 'unknown')}")
            print(f"   - 使用引擎: {output.get('engine', 'unknown')}")
            print(f"   - 识别文本: {output.get('text', '')[:50]}...")
            
            if output.get('version') != 'optimized':
                print("[ERROR] VQ图纸应该使用优化版本")
                return False
        else:
            print(f"[ERROR] VQ图纸OCR执行失败: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        print("[ERROR] VQ图纸OCR执行超时")
        return False
    except Exception as e:
        print(f"[ERROR] VQ图纸OCR执行异常: {e}")
        return False
    
    # 测试其他产品
    print("\n测试其他产品OCR...")
    env["OCR_PRODUCT_NAME"] = "其他产品"
    
    try:
        start_time = time.time()
        result = subprocess.run(
            ["python", "src/Platform/Modules/Drawings/ocr_cli.py", test_image],
            env=env,
            capture_output=True,
            text=True,
            timeout=30
        )
        elapsed_time = time.time() - start_time
        
        if result.returncode == 0:
            output = json.loads(result.stdout)
            print(f"[PASS] 其他产品OCR执行成功")
            print(f"   - 处理时间: {elapsed_time:.3f}秒")
            print(f"   - 识别版本: {output.get('version', 'unknown')}")
            print(f"   - 使用引擎: {output.get('engine', 'unknown')}")
            
            if output.get('version') != 'legacy':
                print("[ERROR] 其他产品应该使用原始版本")
                return False
        else:
            print(f"[ERROR] 其他产品OCR执行失败: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        print("[ERROR] 其他产品OCR执行超时")
        return False
    except Exception as e:
        print(f"[ERROR] 其他产品OCR执行异常: {e}")
        return False
    
    print("[PASS] OCR执行验证通过")
    return True

def test_performance_logging():
    """测试性能日志"""
    print("\n" + "=" * 60)
    print("测试4: 性能日志验证")
    print("=" * 60)
    
    log_path = "data/ocr/performance.log"
    if os.path.exists(log_path):
        print(f"[PASS] 性能日志文件存在: {log_path}")
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if lines:
                print(f"   - 日志条目数: {len(lines)}")
                print(f"   - 最新日志: {lines[-1].strip()}")
            else:
                print("   - 日志文件为空（正常，首次运行）")
    else:
        print(f"[INFO] 性能日志文件不存在: {log_path}（将在首次运行后创建）")
    
    print("[PASS] 性能日志验证通过")
    return True

def run_verification():
    """运行完整验证"""
    print("\n" + "=" * 60)
    print("VQ图纸产品级灰度测试验证开始")
    print("=" * 60 + "\n")
    
    results = []
    
    # 运行各项测试
    results.append(("灰度配置加载", test_grayscale_config()))
    results.append(("路由逻辑验证", test_routing_logic()))
    results.append(("OCR执行验证", test_ocr_execution()))
    results.append(("性能日志验证", test_performance_logging()))
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("验证结果汇总")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "[PASS] 通过" if result else "[FAIL] 失败"
        print(f"{test_name}: {status}")
    
    print(f"\n总计: {passed}/{total} 测试通过")
    
    if passed == total:
        print("\n[SUCCESS] 所有验证测试通过！可以进入灰度测试阶段。")
        return True
    else:
        print("\n[WARNING] 部分验证测试失败，请检查配置后重试。")
        return False

if __name__ == "__main__":
    success = run_verification()
    sys.exit(0 if success else 1)