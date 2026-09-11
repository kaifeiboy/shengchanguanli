#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查看最新测试详细结果
"""

import sqlite3
import os

# 最新的测试结果数据库
test_db = r"e:\workaaa\shengchanguanli\src\test_results\test_run_20260828_162203\test_results.db"

if os.path.exists(test_db):
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()

    # 获取所有测试结果的详细分析
    cursor.execute('''
    SELECT
        image_name,
        ocr_result,
        processing_time,
        has_model,
        has_phone,
        has_nfc,
        text_length
    FROM test_results
    ORDER BY processing_time DESC
    ''')

    print("=== 最新测试详细结果分析 ===\n")

    results = cursor.fetchall()

    # 按处理时间分组分析
    fast_cases = [r for r in results if r[2] <= 12000]  # ≤12秒
    medium_cases = [r for r in results if 12000 < r[2] <= 20000]  # 12-20秒
    slow_cases = [r for r in results if r[2] > 20000]  # >20秒

    print(f"快速处理 (≤12秒): {len(fast_cases)}张")
    for r in fast_cases:
        print(f"  {r[0]}: {r[2]*1000:.0f}ms - 模型:{'Y' if r[3] else 'N'} 电话:{'Y' if r[4] else 'N'} NFC:{'Y' if r[5] else 'N'}")

    print(f"\n中等处理 (12-20秒): {len(medium_cases)}张")
    for r in medium_cases:
        print(f"  {r[0]}: {r[2]*1000:.0f}ms - 模型:{'Y' if r[3] else 'N'} 电话:{'Y' if r[4] else 'N'} NFC:{'Y' if r[5] else 'N'}")

    print(f"\n慢速处理 (>20秒): {len(slow_cases)}张")
    for r in slow_cases:
        print(f"  {r[0]}: {r[2]*1000:.0f}ms - 模型:{'Y' if r[3] else 'N'} 电话:{'Y' if r[4] else 'N'} NFC:{'Y' if r[5] else 'N'}")
        # 显示慢速案例的OCR结果
        print(f"  OCR结果: {r[1][:100].replace(chr(10), ' ')}...")

    # 关键信息识别分析
    model_count = sum(1 for r in results if r[3])
    phone_count = sum(1 for r in results if r[4])
    nfc_count = sum(1 for r in results if r[5])

    print(f"\n=== 关键信息识别分析 ===")
    print(f"型号标识识别: {model_count}/{len(results)} ({model_count/len(results)*100:.1f}%)")
    print(f"电话号码识别: {phone_count}/{len(results)} ({phone_count/len(results)*100:.1f}%)")
    print(f"NFC标识识别: {nfc_count}/{len(results)} ({nfc_count/len(results)*100:.1f}%)")

    # 文本长度分析
    text_lengths = [r[6] for r in results]
    avg_length = sum(text_lengths) / len(text_lengths)
    print(f"平均文本长度: {avg_length:.1f}字符")

    # 处理时间分析
    times = [r[2] for r in results]
    avg_time = sum(times) / len(times)
    print(f"平均处理时间: {avg_time*1000:.0f}ms")

    # 10秒目标分析
    within_10s = sum(1 for t in times if t <= 10.0)
    print(f"10秒内完成: {within_10s}/{len(results)} ({within_10s/len(results)*100:.1f}%)")

    conn.close()

    # 性能瓶颈分析
    print(f"\n=== 性能瓶颈分析 ===")

    if len(slow_cases) > 0:
        print("慢速案例特征分析:")
        slow_avg_length = sum(r[6] for r in slow_cases) / len(slow_cases)
        slow_model_rate = sum(1 for r in slow_cases if r[3]) / len(slow_cases)

        print(f"  平均文本长度: {slow_avg_length:.1f}字符 (整体平均: {avg_length:.1f}字符)")
        print(f"  型号识别率: {slow_model_rate*100:.1f}% (整体: {model_count/len(results)*100:.1f}%)")

        if slow_avg_length > avg_length:
            print("  -> 慢速案例文本长度更长，可能包含更多信息")
        if slow_model_rate < model_count/len(results):
            print("  -> 慢速案例型号识别率更低，可能需要更多处理")

    # 优化建议
    print(f"\n=== 优化建议 ===")

    if within_10s / len(results) < 0.5:
        print("1. 需要优化智能跳过逻辑，提高跳过率")
        print("2. 考虑进一步优化图像预处理速度")
        print("3. 实施并行处理优化")

    if len(slow_cases) > len(results) * 0.3:
        print("4. 慢速案例占比较高，需要分析具体原因")
        print("5. 考虑对慢速案例进行专项优化")

else:
    print(f"测试数据库不存在: {test_db}")