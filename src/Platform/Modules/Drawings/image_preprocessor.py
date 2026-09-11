#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图像预处理模块 - OCR严格分工架构的核心组件

功能：
1. 白平衡处理：解决色偏问题，提高文字与背景对比度
2. CLAHE增强：提高对比度，改善光照不均
3. 质量验证：确保预处理不降低图像质量
4. 性能优化：处理时间<100ms/图

设计原则：
- 保守预处理：避免过度处理
- 质量优先：预处理后质量不能下降
- 性能可控：处理时间有明确上限
- 可验证性：每步都有质量检查
"""

import os
import sys
import time
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

# 复用现有的图像处理函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 质量阈值
QUALITY_THRESHOLDS = {
    "min_contrast": 0.1,        # 最小对比度
    "max_brightness_loss": 0.2,  # 最大亮度损失
    "min_sharpness": 0.05,       # 最小锐度
    "max_processing_time": 0.1   # 最大处理时间（秒）
}


def calculate_image_quality(image_array):
    """计算图像质量指标

    返回:
        dict: 包含对比度、亮度、锐度等质量指标
    """
    if image_array.ndim == 3:
        gray = np.asarray(Image.fromarray(image_array.astype(np.uint8)).convert("L"))
    else:
        gray = image_array.astype(np.uint8)

    # 对比度（标准差）
    contrast = float(gray.std()) / 255.0

    # 亮度（均值）
    brightness = float(gray.mean()) / 255.0

    # 锐度（拉普拉斯方差）
    laplacian = ndimage.laplace(gray.astype(np.float32))
    sharpness = float(laplacian.var()) / 1000.0

    return {
        "contrast": contrast,
        "brightness": brightness,
        "sharpness": sharpness
    }


def white_balance(rgb):
    """白平衡处理（复用现有函数）

    Args:
        rgb: RGB图像数组，float32类型，范围[0,255]

    Returns:
        白平衡后的图像数组
    """
    flat = rgb.reshape(-1, 3)
    means = flat.mean(0)
    means = np.maximum(means, 1.0)
    scale = np.clip(128.0 / means, 0.6, 1.8)
    return np.clip(rgb * scale, 0, 255).astype(np.uint8)


def clahe(gray, tile=16, lo_p=2.0, hi_p=98.0):
    """CLAHE对比度增强（复用现有函数）

    Args:
        gray: 灰度图像数组，float32类型
        tile: CLAHE块大小
        lo_p: 低百分位
        hi_p: 高百分位

    Returns:
        CLAHE增强后的图像数组
    """
    h, w = gray.shape
    ph, pw = (tile - h % tile) % tile, (tile - w % tile) % tile
    work = gray if (ph == 0 and pw == 0) else np.pad(gray, ((0, ph), (0, pw)), mode="edge")
    out = np.empty_like(work, dtype=np.float32)

    for y in range(0, work.shape[0], tile):
        for x in range(0, work.shape[1], tile):
            blk = work[y:y + tile, x:x + tile]
            lo, hi = np.percentile(blk, [lo_p, hi_p])
            out[y:y + tile, x:x + tile] = np.clip((blk - lo) / (hi - lo) * 255.0, 0, 255) if hi > lo else blk

    return out[:h, :w]


def preprocess_image(image_path, output_path=None, quality_check=True):
    """统一的图像预处理入口

    Args:
        image_path: 输入图像路径
        output_path: 输出图像路径（可选，默认自动生成）
        quality_check: 是否进行质量验证

    Returns:
        output_path: 处理后的图像路径

    Raises:
        ValueError: 质量验证失败
        RuntimeError: 处理时间超限
    """
    start_time = time.time()

    # 加载图像
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        raise ValueError(f"无法加载图像 {image_path}: {e}")

    # 计算原始质量指标
    original_arr = np.asarray(img).astype(np.float32)
    original_quality = calculate_image_quality(original_arr)

    # 1. 白平衡处理
    balanced = white_balance(original_arr)

    # 2. 转换为灰度进行CLAHE
    balanced_gray = np.asarray(Image.fromarray(balanced).convert("L")).astype(np.float32)

    # 3. CLAHE对比度增强（保守参数）
    enhanced_gray = clahe(balanced_gray, tile=16, lo_p=2.0, hi_p=98.0)

    # 4. 转换回RGB（保持3通道）
    enhanced_rgb = np.stack([enhanced_gray] * 3, axis=-1)

    # 质量验证
    if quality_check:
        processed_quality = calculate_image_quality(enhanced_rgb)

        # 检查对比度不降低
        if processed_quality["contrast"] < original_quality["contrast"] * 0.9:
            raise ValueError(
                f"预处理降低了对比度: {processed_quality['contrast']:.3f} < "
                f"{original_quality['contrast'] * 0.9:.3f}"
            )

        # 检查亮度变化在合理范围内
        brightness_change = abs(processed_quality["brightness"] - original_quality["brightness"])
        if brightness_change > QUALITY_THRESHOLDS["max_brightness_loss"]:
            raise ValueError(
                f"预处理亮度变化过大: {brightness_change:.3f} > "
                f"{QUALITY_THRESHOLDS['max_brightness_loss']}"
            )

    # 生成输出路径
    if output_path is None:
        base, ext = os.path.splitext(image_path)
        output_path = f"{base}_preprocessed{ext}"

    # 保存处理后的图像
    try:
        Image.fromarray(enhanced_rgb.astype(np.uint8)).save(output_path, quality=95)
    except Exception as e:
        raise RuntimeError(f"无法保存处理后的图像 {output_path}: {e}")

    # 检查处理时间
    processing_time = time.time() - start_time
    if processing_time > QUALITY_THRESHOLDS["max_processing_time"]:
        raise RuntimeError(
            f"预处理时间超限: {processing_time:.3f}s > "
            f"{QUALITY_THRESHOLDS['max_processing_time']}s"
        )

    return output_path


def batch_preprocess_images(image_paths, output_dir=None):
    """批量预处理图像

    Args:
        image_paths: 图像路径列表
        output_dir: 输出目录（可选）

    Returns:
        dict: 处理结果统计
    """
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    results = {
        "success": 0,
        "failed": 0,
        "total_time": 0.0,
        "errors": []
    }

    for img_path in image_paths:
        try:
            start_time = time.time()

            if output_dir:
                filename = os.path.basename(img_path)
                output_path = os.path.join(output_dir, f"preprocessed_{filename}")
            else:
                output_path = None

            preprocess_image(img_path, output_path)
            results["success"] += 1
            results["total_time"] += time.time() - start_time

        except Exception as e:
            results["failed"] += 1
            results["errors"].append({
                "image": img_path,
                "error": str(e)
            })

    return results


def preprocess_image_simple(image_array):
    """简单版本的预处理（直接处理数组，不涉及文件I/O）

    Args:
        image_array: 输入图像数组（RGB，float32，范围[0,255]）

    Returns:
        处理后的图像数组
    """
    # 1. 白平衡
    balanced = white_balance(image_array)

    # 2. CLAHE增强
    balanced_gray = np.asarray(Image.fromarray(balanced.astype(np.uint8)).convert("L")).astype(np.float32)
    enhanced_gray = clahe(balanced_gray, tile=16, lo_p=2.0, hi_p=98.0)

    # 3. 转换回RGB
    enhanced_rgb = np.stack([enhanced_gray] * 3, axis=-1)

    return enhanced_rgb


if __name__ == "__main__":
    # 简单测试
    import sys

    if len(sys.argv) < 2:
        print("用法: python image_preprocessor.py <input_image> [output_image]")
        sys.exit(1)

    input_image = sys.argv[1]
    output_image = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        result = preprocess_image(input_image, output_image)
        print(f"预处理成功: {result}")

        # 显示质量指标
        original = Image.open(input_image)
        processed = Image.open(result)

        orig_quality = calculate_image_quality(np.asarray(original).astype(np.float32))
        proc_quality = calculate_image_quality(np.asarray(processed).astype(np.float32))

        print(f"原始质量 - 对比度: {orig_quality['contrast']:.3f}, "
              f"亮度: {orig_quality['brightness']:.3f}, "
              f"锐度: {orig_quality['sharpness']:.3f}")

        print(f"处理后质量 - 对比度: {proc_quality['contrast']:.3f}, "
              f"亮度: {proc_quality['brightness']:.3f}, "
              f"锐度: {proc_quality['sharpness']:.3f}")

    except Exception as e:
        print(f"预处理失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)