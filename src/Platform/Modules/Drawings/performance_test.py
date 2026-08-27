# -*- coding: utf-8 -*-
"""
性能对比测试脚本
================
对比优化前后的性能差异。

测试项目：
1. OCR缓存效果测试
2. 并行处理效果测试
3. 综合性能对比
"""

import os
import sys
import time
import tempfile
from PIL import Image
import numpy as np

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ocr_cache import get_ocr_result, clear_cache, get_cache_stats
from parallel_utils import parallel_process, parallel_ocr

def create_test_images(count: int = 10, size: tuple = (800, 600)) -> list:
    """创建测试图片"""
    test_dir = tempfile.mkdtemp(prefix="ocr_perf_test_")
    test_images = []
    
    print(f"创建{count}张测试图片...")
    for i in range(count):
        # 创建随机图片
        img_array = np.random.randint(0, 255, (*size, 3), dtype=np.uint8)
        img = Image.fromarray(img_array)
        
        # 添加一些文本
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        try:
            # 尝试使用系统字体
            font = ImageFont.truetype("arial.ttf", 20)
        except:
            font = ImageFont.load_default()
        
        text = f"Test Image {i+1}\\nPerformance Test\\nOCR Sample"
        draw.text((10, 10), text, fill=(255, 255, 255), font=font)
        
        # 保存图片
        img_path = os.path.join(test_dir, f"test_image_{i+1}.jpg")
        img.save(img_path, quality=85)
        test_images.append(img_path)
    
    print(f"测试图片创建完成，保存在: {test_dir}")
    return test_images, test_dir

def mock_ocr_function(image_path: str) -> dict:
    """模拟OCR函数"""
    # 模拟OCR处理时间
    time.sleep(0.2)  # 模拟200ms的OCR处理时间
    
    # 读取图片基本信息
    try:
        with Image.open(image_path) as img:
            width, height = img.size
    except:
        width, height = 0, 0
    
    return {
        "text": f"OCR result for {os.path.basename(image_path)}",
        "confidence": 0.95,
        "width": width,
        "height": height,
        "processing_time": 0.2
    }

def test_cache_performance(test_images: list):
    """测试缓存性能"""
    print("\n" + "="*50)
    print("OCR缓存性能测试")
    print("="*50)
    
    # 清空缓存
    clear_cache()
    
    # 第一次执行（无缓存）
    print("\n第一次执行（无缓存）:")
    start_time = time.time()
    for img_path in test_images:
        result = get_ocr_result(img_path, mock_ocr_function, force_refresh=True)
    first_run_time = time.time() - start_time
    print(f"总耗时: {first_run_time:.2f}秒")
    print(f"平均每张: {first_run_time/len(test_images):.2f}秒")
    
    # 第二次执行（有缓存）
    print("\n第二次执行（有缓存）:")
    start_time = time.time()
    for img_path in test_images:
        result = get_ocr_result(img_path, mock_ocr_function, force_refresh=False)
    cached_run_time = time.time() - start_time
    print(f"总耗时: {cached_run_time:.2f}秒")
    print(f"平均每张: {cached_run_time/len(test_images):.2f}秒")
    
    # 计算性能提升
    speedup = first_run_time / cached_run_time if cached_run_time > 0 else 0
    time_saved = first_run_time - cached_run_time
    print(f"\n性能提升: {speedup:.1f}x")
    print(f"时间节省: {time_saved:.2f}秒 ({(time_saved/first_run_time*100):.1f}%)")
    
    # 显示缓存统计
    stats = get_cache_stats()
    print(f"\n缓存统计: {stats}")

def test_parallel_performance(test_images: list):
    """测试并行处理性能"""
    print("\n" + "="*50)
    print("并行处理性能测试")
    print("="*50)
    
    # 串行处理
    print("\n串行处理:")
    start_time = time.time()
    serial_results = []
    for img_path in test_images:
        result = mock_ocr_function(img_path)
        serial_results.append(result)
    serial_time = time.time() - start_time
    print(f"总耗时: {serial_time:.2f}秒")
    print(f"平均每张: {serial_time/len(test_images):.2f}秒")
    
    # 并行处理（不同工作线程数）
    for workers in [2, 4, 8]:
        print(f"\n并行处理 ({workers}个工作线程):")
        start_time = time.time()
        parallel_results = parallel_ocr(
            test_images, 
            mock_ocr_function, 
            max_workers=workers,
            show_progress=False
        )
        parallel_time = time.time() - start_time
        print(f"总耗时: {parallel_time:.2f}秒")
        print(f"平均每张: {parallel_time/len(test_images):.2f}秒")
        
        # 计算性能提升
        speedup = serial_time / parallel_time if parallel_time > 0 else 0
        time_saved = serial_time - parallel_time
        print(f"性能提升: {speedup:.1f}x")
        print(f"时间节省: {time_saved:.2f}秒 ({(time_saved/serial_time*100):.1f}%)")

def test_combined_performance(test_images: list):
    """测试综合性能（缓存 + 并行）"""
    print("\n" + "="*50)
    print("综合性能测试（缓存 + 并行）")
    print("="*50)
    
    # 清空缓存
    clear_cache()
    
    # 基准：串行 + 无缓存
    print("\n基准: 串行 + 无缓存")
    start_time = time.time()
    for img_path in test_images:
        result = mock_ocr_function(img_path)
    baseline_time = time.time() - start_time
    print(f"总耗时: {baseline_time:.2f}秒")
    
    # 清空缓存，准备下一轮测试
    clear_cache()
    
    # 第一次：并行 + 无缓存
    print("\n第一次: 并行(4线程) + 无缓存")
    start_time = time.time()
    parallel_ocr(test_images, mock_ocr_function, max_workers=4, show_progress=False)
    first_parallel_time = time.time() - start_time
    print(f"总耗时: {first_parallel_time:.2f}秒")
    
    # 第二次：并行 + 有缓存
    print("\n第二次: 并行(4线程) + 有缓存")
    start_time = time.time()
    for img_path in test_images:
        result = get_ocr_result(img_path, mock_ocr_function, force_refresh=False)
    cached_parallel_time = time.time() - start_time
    print(f"总耗时: {cached_parallel_time:.2f}秒")
    
    # 综合性能提升
    total_speedup = baseline_time / cached_parallel_time if cached_parallel_time > 0 else 0
    total_time_saved = baseline_time - cached_parallel_time
    print(f"\n综合性能提升: {total_speedup:.1f}x")
    print(f"总时间节省: {total_time_saved:.2f}秒 ({(total_time_saved/baseline_time*100):.1f}%)")

def run_performance_test():
    """运行完整的性能测试"""
    print("打标首件对比模块 - 性能优化对比测试")
    print("="*50)
    
    try:
        # 创建测试图片
        test_images, test_dir = create_test_images(count=10)
        
        try:
            # 运行各项测试
            test_cache_performance(test_images)
            test_parallel_performance(test_images)
            test_combined_performance(test_images)
            
            print("\n" + "="*50)
            print("性能测试完成")
            print("="*50)
            
        finally:
            # 清理测试文件
            import shutil
            print(f"\n清理测试文件: {test_dir}")
            shutil.rmtree(test_dir, ignore_errors=True)
            
    except Exception as e:
        print(f"测试过程中发生错误: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_performance_test()