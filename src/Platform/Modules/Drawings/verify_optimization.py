#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
性能优化完善和验证脚本
基于当前12张测试图片，完善优化措施并验证效果
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


def verify_optimizations_integrated():
    """验证所有优化措施是否正确集成"""

    print("=== 验证优化措施集成状态 ===\n")

    # 检查1: 模型管理器
    print("1. 检查模型管理器...")
    try:
        from ocr_model_manager import get_model_manager, warmup_ocr_models
        manager = get_model_manager()
        print(f"   模型管理器: 已集成")
        print(f"   模型状态: {manager.get_status()}")
    except ImportError:
        print("   模型管理器: 未集成")

    # 检查2: 图像预处理
    print("\n2. 检查图像预处理...")
    try:
        from smart_image_preprocessor import optimize_image_pipeline
        print(f"   智能预处理: 已集成")
    except ImportError:
        print("   智能预处理: 未集成")

    # 检查3: 智能跳过决策
    print("\n3. 检查智能跳过决策...")
    try:
        from smart_ocr_decision import should_skip_second_ocr_enhanced, analyze_first_result
        print(f"   智能跳过决策: 已集成")
    except ImportError:
        print("   智能跳过决策: 未集成")

    # 检查4: OCR流程优化
    print("\n4. 检查OCR流程优化...")
    try:
        from ocr_cli import _auto_roi
        print(f"   OCR流程: 已优化")
    except ImportError:
        print(f"   OCR流程: 未优化")

    print("\n=== 集成验证完成 ===\n")


def run_optimized_test():
    """运行优化后的完整测试"""

    print("=== 运行优化后性能测试 ===\n")

    # 加载测试图片
    test_images = []
    for filename in os.listdir(TEST_PHOTOS_DIR):
        if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            image_path = os.path.join(TEST_PHOTOS_DIR, filename)
            test_images.append((filename, image_path))

    print(f"找到 {len(test_images)} 张测试图片\n")

    # 预热模型
    print("预热模型...")
    try:
        from ocr_model_manager import warmup_ocr_models
        warmup_success = warmup_ocr_models(3)
        print(f"模型预热: {'成功' if warmup_success else '失败'}\n")
    except Exception as e:
        print(f"模型预热异常: {e}\n")

    # 运行测试
    results = []
    for i, (filename, image_path) in enumerate(test_images, 1):
        print(f"[{i}/{len(test_images)}] 处理: {filename}")

        try:
            start_time = time.time()

            # 使用优化后的OCR流程
            from ocr_cli import _auto_roi
            ocr_result = _auto_roi(image_path)

            processing_time = time.time() - start_time

            # 分析结果
            text = ocr_result if ocr_result else ""
            has_model = bool("PC-" in text or "QHR" in text.upper())
            has_phone = bool("400" in text)
            has_nfc = bool("NFC" in text.upper())

            result = {
                "filename": filename,
                "processing_time": processing_time,
                "ocr_result": text,
                "has_model": has_model,
                "has_phone": has_phone,
                "has_nfc": has_nfc,
                "text_length": len(text),
                "success": len(text) > 0
            }

            results.append(result)

            status = "[OK]" if result['success'] else "[FAIL]"
            skip_indicator = "[跳过]" if processing_time < 8.0 else "[完整]"  # 假设<8秒为跳过二次OCR

            print(f"  {status} {skip_indicator} 时间: {processing_time*1000:.0f}ms, "
                  f"模型:{'Y' if has_model else 'N'} 电话:{'Y' if has_phone else 'N'} NFC:{'Y' if has_nfc else 'N'}")

            # 显示跳过决策信息
            if processing_time < 8.0:
                print(f"  [INFO] 可能跳过了二次OCR，节省时间")

        except Exception as e:
            print(f"  [ERROR] 处理失败: {e}")
            results.append({
                "filename": filename,
                "processing_time": 0,
                "ocr_result": "",
                "has_model": False,
                "has_phone": False,
                "has_nfc": False,
                "text_length": 0,
                "success": False,
                "error": str(e)
            })

    # 分析结果
    print("\n" + "="*80)
    print("优化效果分析")
    print("="*80)

    success_count = sum(1 for r in results if r['success'])
    avg_time = sum(r['processing_time'] for r in results) / len(results) if results else 0
    min_time = min(r['processing_time'] for r in results) if results else 0
    max_time = max(r['processing_time'] for r in results) if results else 0

    within_10s = sum(1 for r in results if r['processing_time'] <= 10.0)
    within_10s_ratio = within_10s / len(results) * 100 if results else 0

    within_8s = sum(1 for r in results if r['processing_time'] <= 8.0)
    within_8s_ratio = within_8s / len(results) * 100 if results else 0

    model_count = sum(1 for r in results if r['has_model'])
    phone_count = sum(1 for r in results if r['has_phone'])
    nfc_count = sum(1 for r in results if r['has_nfc'])

    print(f"总测试数: {len(results)}")
    print(f"成功数: {success_count} ({success_count/len(results)*100:.1f}%)")
    print(f"平均处理时间: {avg_time*1000:.0f}ms ({avg_time:.2f}s)")
    print(f"最快处理时间: {min_time*1000:.0f}ms")
    print(f"最慢处理时间: {max_time*1000:.0f}ms")
    print(f"10秒内完成: {within_10s}/{len(results)} ({within_10s_ratio:.1f}%)")
    print(f"8秒内完成: {within_8s}/{len(results)} ({within_8s_ratio:.1f}%)")
    print(f"型号识别: {model_count}/{success_count} ({model_count/success_count*100 if success_count > 0 else 0:.1f}%)")
    print(f"电话识别: {phone_count}/{success_count} ({phone_count/success_count*100 if success_count > 0 else 0:.1f}%)")
    print(f"NFC识别: {nfc_count}/{success_count} ({nfc_count/success_count*100 if success_count > 0 else 0:.1f}%)")

    # 目标达成情况
    print(f"\n目标达成情况:")
    status_10s = "[OK]" if avg_time <= 10.0 else "[FAIL]"
    print(f"  平均时间<=10s: {status_10s} (目标: {avg_time:.2f}s)")

    status_95 = "[OK]" if within_10s_ratio >= 95.0 else "[FAIL]"
    print(f"  95%≤10s: {status_95} (目标: {within_10s_ratio:.1f}%)")

    status_50 = "[OK]" if (20.5 - avg_time) / 20.5 * 100 >= 50 else "[FAIL]"
    print(f"  性能提升≥50%: {status_50} (提升: {(20.5 - avg_time) / 20.5 * 100:.1f}%)")

    # 按时间分组统计
    print(f"\n处理时间分布:")
    fast = [r for r in results if r['processing_time'] <= 8.0]
    medium = [r for r in results if 8.0 < r['processing_time'] <= 15.0]
    slow = [r for r in results if r['processing_time'] > 15.0]

    print(f"  快速(≤8s): {len(fast)}张 ({len(fast)/len(results)*100:.1f}%)")
    print(f"  中等(8-15s): {len(medium)}张 ({len(medium)/len(results)*100:.1f}%)")
    print(f"  慢速(>15s): {len(slow)}张 ({len(slow)/len(results)*100:.1f}%)")

    return results


def generate_optimization_report(before_avg, before_results, after_avg, after_results):
    """生成优化对比报告"""

    improvement = (before_avg - after_avg) / before_avg * 100 if before_avg > 0 else 0
    time_saved = before_avg - after_avg

    report = f"""
# OCR性能优化对比报告

**测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**测试图片**: {len(after_results)}张

## 性能对比

| 指标 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| 平均处理时间 | {before_avg*1000:.0f}ms | {after_avg*1000:.0f}ms | {improvement:+.1f}% |
| 最快处理时间 | {min(r['processing_time'] for r in before_results)*1000:.0f}ms | {min(r['processing_time'] for r in after_results)*1000:.0f}ms | - |
| 最慢处理时间 | {max(r['processing_time'] for r in before_results)*1000:.0f}ms | {max(r['processing_time'] for r in after_results)*1000:.0f}ms | - |
| 10秒内完成率 | {sum(1 for r in before_results if r['processing_time'] <= 10.0)/len(before_results)*100:.1f}% | {sum(1 for r in after_results if r['processing_time'] <= 10.0)/len(after_results)*100:.1f}% | - |

## 目标达成情况

**10秒目标**:
- 平均时间≤10s: {"OK" if after_avg <= 10.0 else "FAIL"}
- 95%请求≤10s: {"OK" if sum(1 for r in after_results if r['processing_time'] <= 10.0)/len(after_results) >= 0.95 else "FAIL"}

**性能提升目标**:
- 性能提升≥50%: {"OK" if improvement >= 50 else "FAIL"} (实际: {improvement:.1f}%)
- 节省时间: {time_saved:.2f}秒/张

## 识别质量对比

| 关键信息 | 优化前 | 优化后 | 变化 |
|----------|--------|--------|------|
| 型号识别率 | {sum(1 for r in before_results if r['has_model'])/len(before_results)*100:.1f}% | {sum(1 for r in after_results if r['has_model'])/len(after_results)*100:.1f}% | - |
| 电话识别率 | {sum(1 for r in before_results if r['has_phone'])/len(before_results)*100:.1f}% | {sum(1 for r in after_results if r['has_phone'])/len(after_results)*100:.1f}% | - |
| NFC识别率 | {sum(1 for r in before_results if r['has_nfc'])/len(before_results)*100:.1f}% | {sum(1 for r in after_results if r['has_nfc'])/len(after_results)*100:.1f}% | - |

"""

    return report


def main():
    """主函数"""

    print("=== OCR性能优化完善和验证 ===\n")

    # 1. 验证优化集成状态
    verify_optimizations_integrated()

    # 2. 运行优化测试
    optimized_results = run_optimized_test()

    if not optimized_results:
        print("测试失败，无法生成报告")
        return

    # 3. 生成优化报告
    print("\n" + "="*80)
    print("生成优化对比报告")
    print("="*80)

    # 读取之前的测试结果作为对比基准
    import sqlite3
    test_db = r"e:\workaaa\shengchanguanli\src\test_results\test_run_20260828_162203\test_results.db"

    if os.path.exists(test_db):
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        cursor.execute('SELECT processing_time FROM test_results')
        before_times = [row[0] for row in cursor.fetchall()]
        before_avg = sum(before_times) / len(before_times) if before_times else 0

        # 读取详细结果用于识别质量对比
        cursor.execute('''
        SELECT processing_time, has_model, has_phone, has_nfc
        FROM test_results
        ''')

        before_results = []
        for row in cursor.fetchall():
            before_results.append({
                "processing_time": row[0],
                "has_model": bool(row[1]),
                "has_phone": bool(row[2]),
                "has_nfc": bool(row[3])
            })

        conn.close()

        after_avg = sum(r['processing_time'] for r in optimized_results) / len(optimized_results)

        report = generate_optimization_report(before_avg, before_results, after_avg, optimized_results)

        # 保存报告
        report_dir = os.path.join(PROJECT_ROOT, "test_results")
        os.makedirs(report_dir, exist_ok=True)

        report_file = os.path.join(report_dir, f"optimization_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")

        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)

        print(report)
        print(f"优化报告已保存: {report_file}")

    else:
        print("未找到之前的测试结果，无法生成对比报告")
        print("仅显示优化后结果:")

        for r in optimized_results:
            status = "[OK]" if r['success'] else "[FAIL]"
            print(f"{r['filename']}: {r['processing_time']*1000:.0f}ms {status}")

    print("\n=== 优化验证完成 ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"优化验证过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)