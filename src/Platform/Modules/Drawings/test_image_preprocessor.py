#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图像预处理模块单元测试

测试覆盖：
1. 基本功能测试
2. 质量验证测试
3. 性能测试
4. 边界情况测试
5. 异常处理测试
"""

import os
import sys
import time
import tempfile
import numpy as np
from PIL import Image
import pytest

# 添加模块路径
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from image_preprocessor import (
    preprocess_image,
    batch_preprocess_images,
    preprocess_image_simple,
    calculate_image_quality,
    white_balance,
    clahe,
    QUALITY_THRESHOLDS
)


class TestBasicFunctionality:
    """基本功能测试"""

    def test_preprocess_image_creates_output(self):
        """测试预处理能创建输出文件"""
        # 创建测试图像
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name
            Image.new("RGB", (800, 600), color=(128, 128, 128)).save(test_image)

        try:
            output_path = preprocess_image(test_image)
            assert os.path.exists(output_path), "输出文件不存在"
            assert output_path.endswith("_preprocessed.jpg"), "输出文件名格式不正确"

        finally:
            # 清理
            if os.path.exists(test_image):
                os.remove(test_image)
            if os.path.exists(output_path):
                os.remove(output_path)

    def test_preprocess_image_output_specified(self):
        """测试指定输出路径的预处理"""
        # 创建测试图像
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_out:
            output_image = tmp_out.name

        try:
            Image.new("RGB", (800, 600), color=(128, 128, 128)).save(test_image)
            result = preprocess_image(test_image, output_image)
            assert result == output_image, "返回的输出路径不正确"
            assert os.path.exists(output_image), "指定的输出文件不存在"

        finally:
            # 清理
            for path in [test_image, output_image]:
                if os.path.exists(path):
                    os.remove(path)

    def test_preprocess_image_simple(self):
        """测试数组版本的预处理"""
        # 创建测试图像数组
        test_array = np.random.randint(0, 255, (600, 800, 3), dtype=np.uint8).astype(np.float32)

        result = preprocess_image_simple(test_array)
        assert result.shape == test_array.shape, "输出数组形状不正确"
        assert result.dtype == np.float32, "输出数组类型不正确"
        assert np.all((result >= 0) & (result <= 255)), "输出值范围不正确"


class TestQualityValidation:
    """质量验证测试"""

    def test_quality_not_degraded(self):
        """测试预处理不降低图像质量"""
        # 创建测试图像
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            # 创建具有一定对比度的测试图像
            img = Image.new("RGB", (800, 600))
            pixels = []
            for y in range(600):
                for x in range(800):
                    pixels.append((x % 256, y % 256, (x + y) % 256))
            img.putdata(pixels)
            img.save(test_image)

            # 计算原始质量
            original = Image.open(test_image)
            original_quality = calculate_image_quality(np.asarray(original).astype(np.float32))

            # 预处理
            output_path = preprocess_image(test_image)
            processed = Image.open(output_path)
            processed_quality = calculate_image_quality(np.asarray(processed).astype(np.float32))

            # 检查对比度不降低
            assert processed_quality["contrast"] >= original_quality["contrast"] * 0.9, \
                f"对比度降低: {processed_quality['contrast']:.3f} < {original_quality['contrast'] * 0.9:.3f}"

        finally:
            # 清理
            if os.path.exists(test_image):
                os.remove(test_image)
            if os.path.exists(output_path):
                os.remove(output_path)

    def test_quality_calculation(self):
        """测试质量指标计算"""
        # 创建高对比度图像
        high_contrast = np.zeros((100, 100), dtype=np.uint8)
        high_contrast[50:, :] = 255

        quality = calculate_image_quality(high_contrast.astype(np.float32))
        assert quality["contrast"] > 0.3, "高对比度图像对比度指标过低"
        assert 0 <= quality["brightness"] <= 1, "亮度指标超出范围"

        # 创建低对比度图像
        low_contrast = np.full((100, 100), 128, dtype=np.uint8)
        quality_low = calculate_image_quality(low_contrast.astype(np.float32))
        assert quality_low["contrast"] < 0.1, "低对比度图像对比度指标过高"


class TestPerformance:
    """性能测试"""

    def test_processing_time_limit(self):
        """测试处理时间符合要求"""
        # 创建测试图像
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            Image.new("RGB", (800, 600), color=(128, 128, 128)).save(test_image)

            start_time = time.time()
            output_path = preprocess_image(test_image)
            processing_time = time.time() - start_time

            assert processing_time < QUALITY_THRESHOLDS["max_processing_time"], \
                f"处理时间超限: {processing_time:.3f}s > {QUALITY_THRESHOLDS['max_processing_time']}s"

        finally:
            # 清理
            if os.path.exists(test_image):
                os.remove(test_image)
            if os.path.exists(output_path):
                os.remove(output_path)

    def test_batch_processing_performance(self):
        """测试批量处理性能"""
        # 创建多个测试图像
        test_images = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            for i in range(5):
                img_path = os.path.join(tmp_dir, f"test_{i}.jpg")
                Image.new("RGB", (800, 600), color=(128, 128, 128)).save(img_path)
                test_images.append(img_path)

            # 批量处理
            start_time = time.time()
            results = batch_preprocess_images(test_images, tmp_dir)
            total_time = time.time() - start_time

            assert results["success"] == 5, f"批量处理成功数量不正确: {results['success']}"
            assert results["failed"] == 0, f"批量处理失败数量不正确: {results['failed']}"
            assert total_time < 5 * QUALITY_THRESHOLDS["max_processing_time"], \
                f"批量处理时间超限: {total_time:.3f}s"


class TestEdgeCases:
    """边界情况测试"""

    def test_small_image(self):
        """测试小图像处理"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            Image.new("RGB", (100, 100), color=(128, 128, 128)).save(test_image)
            output_path = preprocess_image(test_image)
            assert os.path.exists(output_path), "小图像处理失败"

        finally:
            if os.path.exists(test_image):
                os.remove(test_image)
            if os.path.exists(output_path):
                os.remove(output_path)

    def test_large_image(self):
        """测试大图像处理"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            Image.new("RGB", (2000, 1500), color=(128, 128, 128)).save(test_image)
            output_path = preprocess_image(test_image)
            assert os.path.exists(output_path), "大图像处理失败"

        finally:
            if os.path.exists(test_image):
                os.remove(test_image)
            if os.path.exists(output_path):
                os.remove(output_path)

    def test_extreme_brightness(self):
        """测试极端亮度图像"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            # 极亮图像
            Image.new("RGB", (800, 600), color=(250, 250, 250)).save(test_image)
            output_path = preprocess_image(test_image)
            assert os.path.exists(output_path), "极亮图像处理失败"
            os.remove(output_path)

            # 极暗图像
            Image.new("RGB", (800, 600), color=(5, 5, 5)).save(test_image)
            output_path = preprocess_image(test_image)
            assert os.path.exists(output_path), "极暗图像处理失败"

        finally:
            if os.path.exists(test_image):
                os.remove(test_image)
            if os.path.exists(output_path):
                os.remove(output_path)


class TestErrorHandling:
    """异常处理测试"""

    def test_nonexistent_input(self):
        """测试不存在的输入文件"""
        with pytest.raises(ValueError, match="无法加载图像"):
            preprocess_image("/nonexistent/image.jpg")

    def test_invalid_output_path(self):
        """测试无效的输出路径"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            Image.new("RGB", (800, 600), color=(128, 128, 128)).save(test_image)

            # 无效的输出路径
            invalid_output = "/nonexistent directory/output.jpg"
            with pytest.raises(RuntimeError, match="无法保存处理后的图像"):
                preprocess_image(test_image, invalid_output)

        finally:
            if os.path.exists(test_image):
                os.remove(test_image)

    def test_corrupted_image(self):
        """测试损坏的图像文件"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            # 写入无效的图像数据
            with open(test_image, 'wb') as f:
                f.write(b'This is not a valid image file')

            with pytest.raises(ValueError, match="无法加载图像"):
                preprocess_image(test_image)

        finally:
            if os.path.exists(test_image):
                os.remove(test_image)


class TestIntegration:
    """集成测试"""

    def test_end_to_end_preprocessing(self):
        """测试端到端预处理流程"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            test_image = tmp.name

        try:
            # 创建模拟真实场景的测试图像（有光照不均）
            img = Image.new("RGB", (800, 600))
            pixels = []
            for y in range(600):
                for x in range(800):
                    # 模拟光照不均：左侧较暗，右侧较亮
                    brightness_factor = 0.5 + 0.5 * (x / 800)
                    r = int(100 * brightness_factor + np.random.randint(-20, 20))
                    g = int(100 * brightness_factor + np.random.randint(-20, 20))
                    b = int(120 * brightness_factor + np.random.randint(-20, 20))
                    pixels.append((
                        max(0, min(255, r)),
                        max(0, min(255, g)),
                        max(0, min(255, b))
                    ))
            img.putdata(pixels)
            img.save(test_image)

            # 预处理
            output_path = preprocess_image(test_image)

            # 验证输出
            assert os.path.exists(output_path), "预处理输出不存在"

            # 验证质量改善
            original = Image.open(test_image)
            processed = Image.open(output_path)

            original_quality = calculate_image_quality(np.asarray(original).astype(np.float32))
            processed_quality = calculate_image_quality(np.asarray(processed).astype(np.float32))

            # 预期：对比度应该提升
            assert processed_quality["contrast"] >= original_quality["contrast"], \
                "预处理后对比度没有提升"

        finally:
            if os.path.exists(test_image):
                os.remove(test_image)
            if os.path.exists(output_path):
                os.remove(output_path)


if __name__ == "__main__":
    # 运行测试
    pytest.main([__file__, "-v", "--tb=short"])