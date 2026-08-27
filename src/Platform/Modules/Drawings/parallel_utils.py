# -*- coding: utf-8 -*-
"""
并行处理工具模块 - 性能优化
===========================
为批量操作提供并行处理支持，提升性能。

功能：
1. 并行OCR处理
2. 并行文件操作
3. 智能任务分片
4. 错误处理和重试机制

使用方法：
    from parallel_utils import parallel_ocr, parallel_process
    
    # 并行OCR
    results = parallel_ocr(image_paths, ocr_func, max_workers=4)
    
    # 通用并行处理
    results = parallel_process(items, process_func, max_workers=4)
"""

import os
import time
import concurrent.futures
from typing import List, Callable, Any, Dict, Optional, Tuple
from functools import partial
import traceback

def parallel_ocr(
    image_paths: List[str], 
    ocr_func: Callable[[str], Any],
    max_workers: Optional[int] = None,
    show_progress: bool = False,
    retry_failed: bool = True,
    max_retries: int = 2
) -> Dict[str, Any]:
    """
    并行执行OCR处理
    
    Args:
        image_paths: 图片路径列表
        ocr_func: OCR函数，签名为 func(image_path) -> result
        max_workers: 最大并行工作线程数，None则自动确定
        show_progress: 是否显示进度信息
        retry_failed: 是否重试失败的任务
        max_retries: 最大重试次数
    
    Returns:
        字典：{image_path: ocr_result}
    """
    if not image_paths:
        return {}
    
    # 自动确定工作线程数（不超过CPU核心数和任务数的一半）
    if max_workers is None:
        import multiprocessing
        cpu_count = multiprocessing.cpu_count()
        max_workers = min(cpu_count, max(2, len(image_paths) // 2))
    
    results = {}
    failed_items = []
    
    def process_single_image(image_path: str, attempt: int = 0) -> Tuple[str, Any, bool]:
        """处理单个图片的包装函数"""
        try:
            result = ocr_func(image_path)
            return (image_path, result, True)  # (path, result, success)
        except Exception as e:
            if show_progress:
                print(f"OCR失败 (尝试{attempt + 1}/{max_retries + 1}): {image_path} - {str(e)}")
            return (image_path, {"error": str(e), "success": False}, False)
    
    # 第一轮处理
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(process_single_image, path): path 
            for path in image_paths
        }
        
        completed = 0
        for future in concurrent.futures.as_completed(future_to_path):
            path = future_to_path[future]
            try:
                image_path, result, success = future.result()
                results[image_path] = result
                if not success:
                    failed_items.append(image_path)
                
                completed += 1
                if show_progress and completed % 10 == 0:
                    print(f"进度: {completed}/{len(image_paths)}")
                    
            except Exception as e:
                print(f"处理异常: {path} - {str(e)}")
                failed_items.append(path)
                results[path] = {"error": str(e), "success": False}
    
    # 重试失败的任务
    if retry_failed and failed_items:
        retry_results = {}
        for attempt in range(max_retries):
            if not failed_items:
                break
                
            print(f"重试失败的OCR任务 (第{attempt + 1}轮，剩余{len(failed_items)}个)")
            
            current_failed = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(failed_items))) as executor:
                future_to_path = {
                    executor.submit(process_single_image, path, attempt + 1): path 
                    for path in failed_items
                }
                
                for future in concurrent.futures.as_completed(future_to_path):
                    path = future_to_path[future]
                    try:
                        image_path, result, success = future.result()
                        retry_results[image_path] = result
                        if success:
                            results[image_path] = result  # 更新结果
                        else:
                            current_failed.append(image_path)
                    except Exception as e:
                        current_failed.append(path)
            
            failed_items = current_failed
    
    if show_progress:
        print(f"OCR完成: 成功{len(results) - len(failed_items)}/{len(image_paths)}")
        if failed_items:
            print(f"失败: {len(failed_items)}个图片")
    
    return results

def parallel_process(
    items: List[Any],
    process_func: Callable[[Any], Any],
    max_workers: Optional[int] = None,
    show_progress: bool = False,
    chunk_size: Optional[int] = None
) -> List[Any]:
    """
    通用并行处理函数
    
    Args:
        items: 要处理的项目列表
        process_func: 处理函数，签名为 func(item) -> result
        max_workers: 最大并行工作线程数
        show_progress: 是否显示进度信息
        chunk_size: 分块大小，None则自动确定
    
    Returns:
        处理结果列表
    """
    if not items:
        return []
    
    # 自动确定工作线程数
    if max_workers is None:
        import multiprocessing
        cpu_count = multiprocessing.cpu_count()
        max_workers = min(cpu_count, max(2, len(items) // 10))
    
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {
            executor.submit(process_func, item): item 
            for item in items
        }
        
        completed = 0
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                result = future.result()
                results.append(result)
                
                completed += 1
                if show_progress and completed % 10 == 0:
                    print(f"进度: {completed}/{len(items)}")
                    
            except Exception as e:
                print(f"处理异常: {item} - {str(e)}")
                results.append(None)  # 失败的项目返回None
    
    if show_progress:
        print(f"处理完成: {len([r for r in results if r is not None])}/{len(items)}成功")
    
    return results

def parallel_batch_process(
    items: List[Any],
    batch_process_func: Callable[[List[Any]], List[Any]],
    max_workers: Optional[int] = None,
    show_progress: bool = False,
    batch_size: int = 10
) -> List[Any]:
    """
    批量并行处理（适用于批量API调用等场景）
    
    Args:
        items: 要处理的项目列表
        batch_process_func: 批量处理函数，签名为 func(batch_items) -> batch_results
        max_workers: 最大并行工作线程数
        show_progress: 是否显示进度信息
        batch_size: 每批的大小
    
    Returns:
        处理结果列表
    """
    if not items:
        return []
    
    # 分批
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    
    # 自动确定工作线程数
    if max_workers is None:
        import multiprocessing
        cpu_count = multiprocessing.cpu_count()
        max_workers = min(cpu_count, len(batches))
    
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_batch = {
            executor.submit(batch_process_func, batch): batch 
            for batch in batches
        }
        
        completed = 0
        for future in concurrent.futures.as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                batch_results = future.result()
                results.extend(batch_results)
                
                completed += 1
                if show_progress:
                    print(f"批次进度: {completed}/{len(batches)}")
                    
            except Exception as e:
                print(f"批次处理异常: {len(batch)}个项目 - {str(e)}")
                results.extend([None] * len(batch))  # 失败的批次返回None
    
    if show_progress:
        print(f"批量处理完成: 总计{len(items)}个项目")
    
    return results

def parallel_file_operation(
    file_paths: List[str],
    operation_func: Callable[[str], Any],
    max_workers: Optional[int] = None,
    show_progress: bool = False,
    skip_errors: bool = True
) -> Dict[str, Any]:
    """
    并行文件操作
    
    Args:
        file_paths: 文件路径列表
        operation_func: 文件操作函数，签名为 func(file_path) -> result
        max_workers: 最大并行工作线程数
        show_progress: 是否显示进度信息
        skip_errors: 是否跳过错误继续处理
    
    Returns:
        字典：{file_path: operation_result}
    """
    if not file_paths:
        return {}
    
    # 过滤不存在的文件
    valid_paths = [path for path in file_paths if os.path.exists(path)]
    
    if len(valid_paths) < len(file_paths):
        missing = len(file_paths) - len(valid_paths)
        if show_progress:
            print(f"警告: {missing}个文件不存在，将被跳过")
    
    return parallel_ocr(
        valid_paths,  # 使用parallel_ocr的实现逻辑
        operation_func,
        max_workers=max_workers,
        show_progress=show_progress,
        retry_failed=False,
        max_retries=0
    )

# 性能监控装饰器
import time
from functools import wraps

def monitor_performance(func_name: str = "操作"):
    """性能监控装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start_time
                print(f"{func_name}完成，耗时: {elapsed:.2f}秒")
                return result
            except Exception as e:
                elapsed = time.time() - start_time
                print(f"{func_name}失败，耗时: {elapsed:.2f}秒，错误: {str(e)}")
                raise
        return wrapper
    return decorator

if __name__ == "__main__":
    # 测试代码
    print("并行处理工具模块测试")
    
    # 模拟处理函数
    def mock_process(item):
        time.sleep(0.1)  # 模拟耗时操作
        return f"处理结果_{item}"
    
    # 测试并行处理
    items = list(range(20))
    print(f"开始并行处理{len(items)}个项目...")
    
    @monitor_performance("并行处理")
    def test_parallel():
        return parallel_process(items, mock_process, max_workers=4, show_progress=True)
    
    results = test_parallel()
    print(f"处理完成，获得{len(results)}个结果")
    
    # 测试批量并行处理
    def mock_batch_process(batch):
        time.sleep(0.2)  # 模拟批量操作
        return [f"批量结果_{item}" for item in batch]
    
    @monitor_performance("批量并行处理")
    def test_batch_parallel():
        return parallel_batch_process(items, mock_batch_process, batch_size=5, show_progress=True)
    
    batch_results = test_batch_parallel()
    print(f"批量处理完成，获得{len(batch_results)}个结果")