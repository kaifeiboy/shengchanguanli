#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
性能优化：OCR模型管理器
实现模型预热和单例模式，减少初始化时间
"""

import os
import sys
import time
import glob
from pathlib import Path

# 添加模块路径
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sys

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    RapidOCR = None
    # 输出到 stderr 避免破坏 stdout JSON 协议
    sys.stderr.write("[OCR-MANAGER] 警告: RapidOCR未安装，将使用模拟模式\n")
    sys.stderr.flush()

class OcrModelManager:
    """OCR模型单例管理器"""

    _instance = None
    _model = None
    _initialized = False
    _warmup_completed = False

    def __new__(cls):
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_model(self):
        """获取RapidOCR模型实例（单例）"""
        if not self._initialized:
            self._initialize_model()
        return self._model

    def _initialize_model(self):
        """初始化RapidOCR模型"""
        if RapidOCR is None:
            sys.stderr.write("[OCR-MANAGER] RapidOCR未安装，使用模拟模式\n")
            sys.stderr.flush()
            self._model = MockRapidOCR()
            self._initialized = True
            return

        sys.stderr.write("[OCR-MANAGER] 正在初始化RapidOCR模型...\n")
        sys.stderr.flush()
        start_time = time.time()

        try:
            self._model = RapidOCR()
            self._initialized = True
            init_time = time.time() - start_time
            sys.stderr.write(f"[OCR-MANAGER] RapidOCR模型初始化完成，耗时: {init_time:.2f}秒\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[OCR-MANAGER] RapidOCR初始化失败: {e}\n")
            sys.stderr.write("[OCR-MANAGER] 使用模拟模式\n")
            sys.stderr.flush()
            self._model = MockRapidOCR()
            self._initialized = True

    def warmup_model(self, max_warmup_images=3):
        """预热模型，减少首次推理延迟"""
        if self._warmup_completed:
            sys.stderr.write("[OCR-MANAGER] 模型已预热完成\n")
            sys.stderr.flush()
            return True

        if not self._initialized:
            self._initialize_model()

        sys.stderr.write("[OCR-MANAGER] 开始预热RapidOCR模型...\n")
        sys.stderr.flush()
        warmup_dir = os.path.join(HERE, "..", "..", "..", "..", "data", "ocr")

        # 查找预热图片
        warmup_images = glob.glob(os.path.join(warmup_dir, "warmup_*.png"))

        if not warmup_images:
            sys.stderr.write("[OCR-MANAGER] 未找到预热图片，跳过预热\n")
            sys.stderr.flush()
            self._warmup_completed = True
            return True

        start_time = time.time()
        success_count = 0

        for i, img_path in enumerate(warmup_images[:max_warmup_images]):
            try:
                if os.path.exists(img_path):
                    result = self._model(img_path)
                    success_count += 1
                    sys.stderr.write(f"[OCR-MANAGER]   预热图片 {i+1}/{min(max_warmup_images, len(warmup_images))}: {os.path.basename(img_path)}\n")
                    sys.stderr.flush()
            except Exception as e:
                sys.stderr.write(f"[OCR-MANAGER]   预热图片 {i+1} 失败: {e}\n")
                sys.stderr.flush()

        warmup_time = time.time() - start_time
        self._warmup_completed = True

        sys.stderr.write(f"[OCR-MANAGER] 模型预热完成！成功预热 {success_count}/{min(max_warmup_images, len(warmup_images))} 张图片，耗时: {warmup_time:.2f}秒\n")
        sys.stderr.flush()

        return success_count > 0

    def is_ready(self):
        """检查模型是否就绪"""
        return self._initialized and self._warmup_completed

    def get_status(self):
        """获取模型状态"""
        return {
            "initialized": self._initialized,
            "warmup_completed": self._warmup_completed,
            "model_type": type(self._model).__name__,
            "ready": self.is_ready()
        }


class MockRapidOCR:
    """模拟RapidOCR，用于测试"""

    def __call__(self, image_path):
        """模拟OCR识别"""
        # 模拟处理延迟
        time.sleep(0.1)

        # 返回模拟结果
        return [
            ([[0, 0, 100, 0], [100, 0, 100, 20], [100, 20, 0, 20], [0, 20, 0, 0]],
             "模拟OCR结果", 0.9)
        ]


# 全局单例实例
_model_manager = None

def get_model_manager():
    """获取模型管理器单例"""
    global _model_manager
    if _model_manager is None:
        _model_manager = OcrModelManager()
    return _model_manager


def warmup_ocr_models(max_warmup_images=3):
    """预热OCR模型（应用启动时调用）"""
    manager = get_model_manager()
    return manager.warmup_model(max_warmup_images)


def get_rapidocr_model():
    """获取RapidOCR模型实例"""
    manager = get_model_manager()
    return manager.get_model()


if __name__ == "__main__":
    print("=== OCR模型管理器测试 ===\n")

    # 测试单例模式
    print("测试单例模式...")
    manager1 = get_model_manager()
    manager2 = get_model_manager()

    print(f"manager1 和 manager2 是否为同一个实例: {manager1 is manager2}")

    # 测试模型初始化
    print("\n测试模型初始化...")
    model = get_rapidocr_model()
    print(f"模型类型: {type(model).__name__}")

    # 测试模型预热
    print("\n测试模型预热...")
    warmup_success = warmup_ocr_models(3)

    # 检查状态
    print(f"\n模型状态: {get_model_manager().get_status()}")

    # 测试OCR调用
    print("\n测试OCR调用...")
    test_image = r"e:\workaaa\shengchanguanli\vq_closeup.jpg"
    if os.path.exists(test_image):
        start = time.time()
        result = get_rapidocr_model()(test_image)
        call_time = time.time() - start
        print(f"OCR调用完成，耗时: {call_time:.2f}秒")
        print(f"识别结果: {result[0][1] if result else '无结果'}")
    else:
        print(f"测试图片不存在: {test_image}")

    print("\n=== 测试完成 ===")