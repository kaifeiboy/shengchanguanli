#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能图像预处理 - 性能优化版本
在保证识别质量的前提下，优化处理速度
"""

import os
import time
from PIL import Image


def smart_resize_for_ocr(image_path, max_dim=1280, quality=90):
    """智能缩放：平衡质量和速度

    策略：
    - 只在图片过大时缩放
    - 使用高质量的LANCZOS算法
    - 适度压缩文件大小
    - 保留原始文件

    Args:
        image_path: 原始图片路径
        max_dim: 最大边长（默认1280）
        quality: JPEG质量（默认90）

    Returns:
        处理后的图片路径（如果需要缩放）或原始路径（如果不需要）
    """
    try:
        # 获取图片信息
        img = Image.open(image_path)
        w, h = img.size
        current_max = max(w, h)
        file_size = os.path.getsize(image_path) / 1024  # KB

        # 判断是否需要缩放
        need_resize = False

        # 条件1：图片尺寸过大
        if current_max > max_dim:
            need_resize = True
            reason = f"尺寸过大 ({current_max} > {max_dim})"

        # 条件2：文件过大（>2MB）
        elif file_size > 2048:
            need_resize = True
            reason = f"文件过大 ({file_size:.0f}KB > 2048KB)"

        if not need_resize:
            # 不需要缩放，直接返回原路径
            return image_path

        # 计算缩放比例
        scale = max_dim / current_max
        new_w, new_h = int(w * scale), int(h * scale)

        # 执行缩放
        start_time = time.time()
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)
        resize_time = time.time() - start_time

        # 生成新文件名
        base, ext = os.path.splitext(image_path)
        resized_path = f"{base}_resized{ext}"

        # 保存缩放后的图片
        img_resized.save(resized_path, quality=quality, optimize=True)

        # 显示缩放信息
        original_size = os.path.getsize(image_path) / 1024  # KB
        resized_size = os.path.getsize(resized_path) / 1024  # KB
        compression_ratio = (1 - resized_size / original_size) * 100

        print(f"[智能缩放] {os.path.basename(image_path)}: {reason}")
        print(f"  尺寸: {w}x{h} -> {new_w}x{new_h}")
        print(f"  文件: {original_size:.0f}KB -> {resized_size:.0f}KB (压缩{compression_ratio:.1f}%)")
        print(f"  耗时: {resize_time*1000:.0f}ms")

        return resized_path

    except Exception as e:
        print(f"[智能缩放失败] {os.path.basename(image_path)}: {e}")
        # 缩放失败，返回原路径
        return image_path


def batch_smart_resize(image_paths, max_dim=1280, quality=90):
    """批量智能缩放

    Args:
        image_paths: 图片路径列表
        max_dim: 最大边长
        quality: JPEG质量

    Returns:
        处理结果统计
    """
    results = {
        "total": len(image_paths),
        "resized": 0,
        "skipped": 0,
        "failed": 0,
        "total_time": 0.0,
        "saved_space": 0.0  # KB
    }

    start_time = time.time()

    for image_path in image_paths:
        try:
            original_size = os.path.getsize(image_path) / 1024 if os.path.exists(image_path) else 0

            resized_path = smart_resize_for_ocr(image_path, max_dim, quality)

            if resized_path == image_path:
                results["skipped"] += 1
            else:
                results["resized"] += 1
                if os.path.exists(resized_path):
                    resized_size = os.path.getsize(resized_path) / 1024
                    results["saved_space"] += (original_size - resized_size)

        except Exception as e:
            results["failed"] += 1
            print(f"[批量缩放失败] {os.path.basename(image_path)}: {e}")

    results["total_time"] = time.time() - start_time

    return results


def optimize_image_pipeline(image_path):
    """完整的图像处理流水线（性能优化版）

    包含：
    1. 智能缩放
    2. 格式转换（如果需要）
    3. 质量检查

    Args:
        image_path: 输入图片路径

    Returns:
        处理后的图片路径
    """
    try:
        # 步骤1：智能缩放
        processed_path = smart_resize_for_ocr(image_path)

        # 步骤2：确保是RGB格式
        img = Image.open(processed_path)
        if img.mode != 'RGB':
            rgb_path = processed_path.replace(os.path.splitext(processed_path)[1], "_rgb.jpg")
            img.convert('RGB').save(rgb_path, quality=90)
            return rgb_path

        return processed_path

    except Exception as e:
        print(f"[图像处理失败] {os.path.basename(image_path)}: {e}")
        return image_path


if __name__ == "__main__":
    print("=== 智能图像预处理测试 ===\n")

    # 测试单张图片处理
    test_image = r"e:\workaaa\shengchanguanli\vq_closeup.jpg"

    if os.path.exists(test_image):
        print(f"测试图片: {os.path.basename(test_image)}")
        print(f"原始大小: {os.path.getsize(test_image)/1024:.0f}KB")

        start = time.time()
        processed = optimize_image_pipeline(test_image)
        process_time = time.time() - start

        print(f"\n处理完成:")
        print(f"  处理后路径: {os.path.basename(processed)}")
        print(f"  处理后大小: {os.path.getsize(processed)/1024:.0f}KB")
        print(f"  处理时间: {process_time*1000:.0f}ms")
    else:
        print(f"测试图片不存在: {test_image}")

    # 测试批量处理
    print("\n=== 批量处理测试 ===")
    test_images = [
        r"e:\workaaa\shengchanguanli\vq_closeup.jpg",
        r"e:\workaaa\shengchanguanli\jq_closeup.jpg"
    ]

    existing_images = [img for img in test_images if os.path.exists(img)]

    if existing_images:
        print(f"找到 {len(existing_images)} 张测试图片")

        results = batch_smart_resize(existing_images)

        print(f"\n批量处理结果:")
        print(f"  总数: {results['total']}")
        print(f"  已缩放: {results['resized']}")
        print(f"  已跳过: {results['skipped']}")
        print(f"  失败: {results['failed']}")
        print(f"  节省空间: {results['saved_space']:.0f}KB")
        print(f"  总耗时: {results['total_time']*1000:.0f}ms")
    else:
        print("没有找到测试图片")

    print("\n=== 测试完成 ===")