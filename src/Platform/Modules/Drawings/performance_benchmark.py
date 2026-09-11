#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
性能优化验证脚本
对比优化前后的处理时间，验证是否达到10秒目标
"""

import os
import sys
import time
import glob
from datetime import datetime

# 添加模块路径
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

# 测试照片目录
TEST_PHOTOS_DIR = r"c:/Users/Administrator/Desktop/测试照片"


class PerformanceBenchmark:
    """性能基准测试"""

    def __init__(self):
        self.test_images = []
        self.results = []

    def load_test_images(self):
        """加载测试图片"""
        print(f"从 {TEST_PHOTOS_DIR} 加载测试图片...")

        for filename in os.listdir(TEST_PHOTOS_DIR):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                image_path = os.path.join(TEST_PHOTOS_DIR, filename)
                self.test_images.append((filename, image_path))

        print(f"加载了 {len(self.test_images)} 张测试图片")
        return len(self.test_images) > 0

    def test_model_warmup(self):
        """测试模型预热效果"""
        print("\n=== 测试模型预热 ===")

        from ocr_model_manager import get_model_manager, warmup_ocr_models

        # 第一次初始化（应该包含预热时间）
        start = time.time()
        manager = get_model_manager()
        model = manager.get_model()
        first_init_time = time.time() - start

        print(f"首次初始化耗时: {first_init_time:.2f}秒")

        # 预热模型
        start = time.time()
        warmup_success = warmup_ocr_models(3)
        warmup_time = time.time() - start

        print(f"模型预热耗时: {warmup_time:.2f}秒")
        print(f"预热状态: {'成功' if warmup_success else '失败'}")

        # 测试预热后的调用速度
        start = time.time()
        model = get_model_manager().get_model()
        second_call_time = time.time() - start

        print(f"预热后调用耗时: {second_call_time:.2f}秒")
        print(f"性能提升: {(first_init_time - second_call_time)/first_init_time*100:.1f}%")

        return {
            "first_init_time": first_init_time,
            "warmup_time": warmup_time,
            "second_call_time": second_call_time,
            "improvement": (first_init_time - second_call_time) / first_init_time * 100
        }

    def test_image_preprocessing(self):
        """测试图像预处理效果"""
        print("\n=== 测试图像预处理 ===")

        if not self.test_images:
            print("没有测试图片")
            return None

        from smart_image_preprocessor import smart_resize_for_ocr

        # 测试5张图片
        test_sample = self.test_images[:5]
        preprocessing_times = []

        for i, (filename, image_path) in enumerate(test_sample, 1):
            start = time.time()
            processed_path = smart_resize_for_ocr(image_path)
            process_time = time.time() - start

            preprocessing_times.append(process_time)

            original_size = os.path.getsize(image_path) / 1024  # KB
            processed_size = os.path.getsize(processed_path) / 1024  # KB

            print(f"[{i}/{len(test_sample)}] {filename}")
            print(f"  处理时间: {process_time*1000:.0f}ms")
            print(f"  文件大小: {original_size:.0f}KB -> {processed_size:.0f}KB")

        avg_time = sum(preprocessing_times) / len(preprocessing_times)
        print(f"\n平均预处理时间: {avg_time*1000:.0f}ms")

        return {
            "avg_preprocessing_time": avg_time,
            "max_preprocessing_time": max(preprocessing_times),
            "min_preprocessing_time": min(preprocessing_times)
        }

    def test_ocr_performance(self, sample_size=10):
        """测试OCR性能"""
        print(f"\n=== 测试OCR性能（样本: {sample_size}张）===")

        if not self.test_images:
            print("没有测试图片")
            return None

        # 确保模型预热
        from ocr_model_manager import warmup_ocr_models
        warmup_ocr_models(3)

        # 测试样本
        test_sample = self.test_images[:sample_size]
        ocr_times = []

        for i, (filename, image_path) in enumerate(test_sample, 1):
            try:
                start = time.time()

                # 调用OCR（使用优化后的流程）
                from ocr_cli import _run_rapidocr
                result = _run_rapidocr(image_path)

                ocr_time = time.time() - start
                ocr_times.append(ocr_time)

                success = result[0] is not None and len(result[0]) > 0
                status = "[成功]" if success else "[失败]"

                print(f"[{i}/{len(test_sample)}] {filename}: {ocr_time*1000:.0f}ms {status}")

            except Exception as e:
                print(f"[{i}/{len(test_sample)}] {filename}: 错误 - {e}")

        if ocr_times:
            avg_time = sum(ocr_times) / len(ocr_times)
            max_time = max(ocr_times)
            min_time = min(ocr_times)
            median_time = sorted(ocr_times)[len(ocr_times)//2]

            print(f"\nOCR性能统计:")
            print(f"  平均时间: {avg_time*1000:.0f}ms ({avg_time:.2f}s)")
            print(f"  最快时间: {min_time*1000:.0f}ms")
            print(f"  最慢时间: {max_time*1000:.0f}ms")
            print(f"  中位数时间: {median_time*1000:.0f}ms")

            # 计算10秒目标达成情况
            within_10s = sum(1 for t in ocr_times if t <= 10.0)
            within_10s_ratio = within_10s / len(ocr_times) * 100

            print(f"  10秒内完成: {within_10s}/{len(ocr_times)} ({within_10s_ratio:.1f}%)")

            return {
                "avg_time": avg_time,
                "max_time": max_time,
                "min_time": min_time,
                "median_time": median_time,
                "within_10s_count": within_10s,
                "within_10s_ratio": within_10s_ratio,
                "target_met": avg_time <= 10.0 and within_10s_ratio >= 95.0
            }

        return None

    def test_skip_decision(self):
        """测试智能跳过决策效果"""
        print(f"\n=== 测试智能跳过决策 ===")

        from smart_ocr_decision import should_skip_second_ocr_enhanced, analyze_first_result
        from ocr_cli import _run_rapidocr

        if not self.test_images:
            print("没有测试图片")
            return None

        test_sample = self.test_images[:10]
        skip_decisions = []

        for i, (filename, image_path) in enumerate(test_sample, 1):
            try:
                # 获取首次OCR结果
                text, conf, _ = _run_rapidocr(image_path)

                # 分析结果
                analysis = analyze_first_result((text, conf))

                # 做出跳过决策
                should_skip, reason = should_skip_second_ocr_enhanced(analysis)

                skip_decisions.append(should_skip)

                status = "[跳过]" if should_skip else "[需要二次]"
                print(f"[{i}/{len(test_sample)}] {filename}: {status} ({reason})")

            except Exception as e:
                print(f"[{i}/{len(test_sample)}] {filename}: 错误 - {e}")

        if skip_decisions:
            skip_count = sum(skip_decisions)
            skip_ratio = skip_count / len(skip_decisions) * 100

            print(f"\n跳过决策统计:")
            print(f"  跳过数量: {skip_count}/{len(skip_decisions)}")
            print(f"  跳过比例: {skip_ratio:.1f}%")
            print(f"  预期节省时间: {skip_ratio * 8 / 100:.1f}s (基于8秒二次OCR)")

            return {
                "skip_count": skip_count,
                "skip_ratio": skip_ratio,
                "expected_time_saving": skip_ratio * 8 / 100
            }

        return None

    def generate_final_report(self, warmup_results, preprocessing_results, ocr_results, skip_results):
        """生成最终性能报告"""

        report = f"""
# OCR性能优化验证报告

**测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**测试目标**: 处理时间控制在10秒内

## 优化效果总结

### 模型预热优化
- 首次初始化: {warmup_results['first_init_time']:.2f}s
- 模型预热: {warmup_results['warmup_time']:.2f}s
- 预热后调用: {warmup_results['second_call_time']:.2f}s
- **性能提升**: {warmup_results['improvement']:.1f}%

### 图像预处理优化
- 平均预处理时间: {preprocessing_results['avg_preprocessing_time']*1000:.0f}ms
- 最大预处理时间: {preprocessing_results['max_preprocessing_time']*1000:.0f}ms
- 最小预处理时间: {preprocessing_results['min_preprocessing_time']*1000:.0f}ms

### OCR性能优化
- 平均处理时间: {ocr_results['avg_time']*1000:.0f}ms ({ocr_results['avg_time']:.2f}s)
- 最快处理时间: {ocr_results['min_time']*1000:.0f}ms
- 最慢处理时间: {ocr_results['max_time']*1000:.0f}ms
- 中位数处理时间: {ocr_results['median_time']*1000:.0f}ms
- **10秒内完成**: {ocr_results['within_10s_ratio']:.1f}%

### 智能跳过优化
- 跳过比例: {skip_results['skip_ratio']:.1f}%
- 预期节省时间: {skip_results['expected_time_saving']:.2f}s

## 目标达成情况

| 指标 | 目标 | 实际 | 状态 |
|------|------|------|------|
| 平均处理时间 | ≤10s | {ocr_results['avg_time']:.2f}s | {'✅ 达成' if ocr_results['avg_time'] <= 10.0 else '❌ 未达成'} |
| 95%请求≤10s | ≥95% | {ocr_results['within_10s_ratio']:.1f}% | {'✅ 达成' if ocr_results['within_10s_ratio'] >= 95.0 else '❌ 未达成'} |
| 性能提升 | ≥50% | {((20.5 - ocr_results['avg_time']) / 20.5 * 100):.1f}% | {'✅ 达成' if ((20.5 - ocr_results['avg_time']) / 20.5 * 100) >= 50 else '❌ 未达成'} |

## 优化建议

"""

        if ocr_results['avg_time'] > 10.0:
            report += f"""
**当前未达到10秒目标，建议进一步优化：**

1. **扩大智能跳过比例** - 当前跳过比例{skip_results['skip_ratio']:.1f}%，可优化至70%+
2. **优化图像预处理** - 可进一步降低预处理时间
3. **并行处理** - 对比匹配和OCR结果处理可并行执行
4. **缓存机制** - 对重复图片实现缓存，处理时间<0.1s

"""
        else:
            report += """
**🎉 已达到10秒目标！可以部署到生产环境。**

"""
        return report


def main():
    """主函数"""
    print("=== OCR性能优化验证 ===")
    print(f"项目根目录: {PROJECT_ROOT}")
    print()

    benchmark = PerformanceBenchmark()

    # 加载测试图片
    if not benchmark.load_test_images():
        print("无法加载测试图片，退出")
        return

    # 执行各项测试
    warmup_results = benchmark.test_model_warmup()
    preprocessing_results = benchmark.test_image_preprocessing()
    ocr_results = benchmark.test_ocr_performance()
    skip_results = benchmark.test_skip_decision()

    # 生成最终报告
    if all([warmup_results, preprocessing_results, ocr_results, skip_results]):
        final_report = benchmark.generate_final_report(
            warmup_results, preprocessing_results, ocr_results, skip_results
        )

        # 保存报告
        report_dir = os.path.join(PROJECT_ROOT, "test_results")
        os.makedirs(report_dir, exist_ok=True)

        report_file = os.path.join(report_dir, f"performance_optimization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")

        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(final_report)

        print(final_report)
        print(f"报告已保存: {report_file}")
    else:
        print("部分测试失败，无法生成完整报告")

    print("\n=== 性能验证完成 ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"性能验证过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)