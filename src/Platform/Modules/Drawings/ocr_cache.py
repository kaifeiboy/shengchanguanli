# -*- coding: utf-8 -*-
"""
OCR 缓存模块 - 性能优化
=====================
为 OCR 操作添加缓存机制，避免重复计算相同图片的 OCR 结果。

缓存策略：
1. 基于图片路径和修改时间的缓存键
2. 内存缓存 + 可选的磁盘持久化
3. LRU 缓存淘汰策略
4. 线程安全的缓存访问

使用方法：
    from ocr_cache import get_ocr_result, clear_cache
    
    # 首次调用会执行 OCR
    result1 = get_ocr_result("path/to/image.jpg")
    
    # 相同图片的第二次调用从缓存返回
    result2 = get_ocr_result("path/to/image.jpg")
"""

import os
import hashlib
import time
import json
import threading
from functools import lru_cache
from typing import Dict, Tuple, Optional, Any
from pathlib import Path

# 缓存配置
CACHE_MAX_SIZE = 100  # 最大缓存条目数
CACHE_TTL = 3600      # 缓存过期时间（秒）
ENABLE_DISK_CACHE = True  # 是否启用磁盘缓存
DISK_CACHE_DIR = "data/ocr_cache"  # 磁盘缓存目录

# 内存缓存
_memory_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.RLock()

import threading

def _get_cache_key(image_path: str) -> str:
    """生成缓存键：基于文件路径、修改时间和文件大小的哈希"""
    try:
        stat = os.stat(image_path)
        key_data = f"{image_path}|{stat.st_mtime}|{stat.st_size}"
        return hashlib.md5(key_data.encode()).hexdigest()
    except (OSError, IOError):
        # 如果文件状态获取失败，使用路径作为简单键
        return hashlib.md5(image_path.encode()).hexdigest()

def _get_disk_cache_path(cache_key: str) -> str:
    """获取磁盘缓存文件路径"""
    os.makedirs(DISK_CACHE_DIR, exist_ok=True)
    return os.path.join(DISK_CACHE_DIR, f"{cache_key}.json")

def _load_from_disk(cache_key: str) -> Optional[Any]:
    """从磁盘缓存加载数据"""
    if not ENABLE_DISK_CACHE:
        return None
    
    cache_file = _get_disk_cache_path(cache_key)
    if not os.path.exists(cache_file):
        return None
    
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 检查是否过期
            if time.time() - data.get('timestamp', 0) > CACHE_TTL:
                os.remove(cache_file)  # 删除过期缓存
                return None
            return data.get('result')
    except (json.JSONDecodeError, IOError, OSError):
        return None

def _save_to_disk(cache_key: str, result: Any):
    """保存数据到磁盘缓存"""
    if not ENABLE_DISK_CACHE:
        return
    
    try:
        cache_file = _get_disk_cache_path(cache_key)
        data = {
            'timestamp': time.time(),
            'result': result
        }
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (IOError, OSError, TypeError):
        # 如果无法序列化或写入，忽略磁盘缓存
        pass

def clear_cache():
    """清空所有缓存（内存 + 磁盘）"""
    global _memory_cache
    with _cache_lock:
        _memory_cache.clear()
    
    # 清空磁盘缓存
    if ENABLE_DISK_CACHE and os.path.exists(DISK_CACHE_DIR):
        try:
            for cache_file in os.listdir(DISK_CACHE_DIR):
                cache_path = os.path.join(DISK_CACHE_DIR, cache_file)
                if os.path.isfile(cache_path):
                    os.remove(cache_path)
        except (IOError, OSError):
            pass

def get_cache_stats() -> Dict[str, Any]:
    """获取缓存统计信息"""
    with _cache_lock:
        memory_count = len(_memory_cache)
    
    disk_count = 0
    if ENABLE_DISK_CACHE and os.path.exists(DISK_CACHE_DIR):
        try:
            disk_count = len([f for f in os.listdir(DISK_CACHE_DIR) 
                            if f.endswith('.json')])
        except (IOError, OSError):
            pass
    
    return {
        'memory_cache_count': memory_count,
        'disk_cache_count': disk_count,
        'max_cache_size': CACHE_MAX_SIZE,
        'cache_ttl_seconds': CACHE_TTL,
        'disk_cache_enabled': ENABLE_DISK_CACHE
    }

def get_ocr_result(image_path: str, ocr_func, force_refresh: bool = False) -> Any:
    """
    获取 OCR 结果（带缓存）
    
    Args:
        image_path: 图片路径
        ocr_func: OCR 函数，签名为 func(image_path) -> result
        force_refresh: 是否强制刷新缓存
    
    Returns:
        OCR 结果
    """
    cache_key = _get_cache_key(image_path)
    
    # 如果不强制刷新，尝试从缓存获取
    if not force_refresh:
        # 检查内存缓存
        with _cache_lock:
            if cache_key in _memory_cache:
                timestamp, result = _memory_cache[cache_key]
                if time.time() - timestamp < CACHE_TTL:
                    return result  # 缓存命中
                else:
                    del _memory_cache[cache_key]  # 删除过期缓存
        
        # 检查磁盘缓存
        disk_result = _load_from_disk(cache_key)
        if disk_result is not None:
            # 将磁盘缓存结果加载到内存缓存
            with _cache_lock:
                _memory_cache[cache_key] = (time.time(), disk_result)
                # 检查缓存大小，必要时清理
                if len(_memory_cache) > CACHE_MAX_SIZE:
                    # 删除最旧的缓存条目
                    oldest_key = min(_memory_cache.keys(), 
                                   key=lambda k: _memory_cache[k][0])
                    del _memory_cache[oldest_key]
            return disk_result
    
    # 缓存未命中，执行 OCR
    result = ocr_func(image_path)
    
    # 保存到缓存
    with _cache_lock:
        _memory_cache[cache_key] = (time.time(), result)
        # 检查缓存大小，必要时清理
        if len(_memory_cache) > CACHE_MAX_SIZE:
            # 删除最旧的缓存条目
            oldest_key = min(_memory_cache.keys(), 
                           key=lambda k: _memory_cache[k][0])
            del _memory_cache[oldest_key]
    
    # 保存到磁盘缓存
    _save_to_disk(cache_key, result)
    
    return result

# 预留：批量OCR缓存支持
def get_batch_ocr_results(image_paths: list, ocr_func, force_refresh: bool = False) -> Dict[str, Any]:
    """
    批量获取 OCR 结果（带缓存）
    
    Args:
        image_paths: 图片路径列表
        ocr_func: OCR 函数
        force_refresh: 是否强制刷新缓存
    
    Returns:
        字典：{image_path: ocr_result}
    """
    results = {}
    for image_path in image_paths:
        results[image_path] = get_ocr_result(image_path, ocr_func, force_refresh)
    return results

if __name__ == "__main__":
    # 测试代码
    print("OCR 缓存模块测试")
    
    # 模拟 OCR 函数
    def mock_ocr(image_path):
        print(f"执行 OCR: {image_path}")
        return {"text": f"模拟OCR结果_{os.path.basename(image_path)}", "confidence": 0.95}
    
    # 测试缓存功能
    test_image = "test_image.jpg"
    
    # 第一次调用（执行OCR）
    result1 = get_ocr_result(test_image, mock_ocr)
    print(f"结果1: {result1}")
    
    # 第二次调用（从缓存）
    result2 = get_ocr_result(test_image, mock_ocr)
    print(f"结果2: {result2}")
    
    # 查看缓存统计
    stats = get_cache_stats()
    print(f"缓存统计: {stats}")
    
    # 清空缓存
    clear_cache()
    print("缓存已清空")
    
    stats = get_cache_stats()
    print(f"清空后统计: {stats}")