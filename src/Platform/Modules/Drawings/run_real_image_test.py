#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实图片测试脚本 - 使用测试照片文件夹中的图片进行测试
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime
from pathlib import Path

# 添加模块路径
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

# 测试照片目录
TEST_PHOTOS_DIR = r"c:/Users/Administrator/Desktop/测试照片"


def setup_test_environment():
    """设置测试环境"""
    # 创建测试结果目录
    test_results_dir = os.path.join(PROJECT_ROOT, "test_results", f"test_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(test_results_dir, exist_ok=True)

    # 创建测试数据库
    test_db = os.path.join(test_results_dir, "test_results.db")
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS test_results (
        id INTEGER PRIMARY KEY,
        image_name TEXT NOT NULL,
        image_path TEXT NOT NULL,
        ocr_result TEXT,
        processing_time REAL,
        success BOOLEAN,
        error TEXT,
        has_model BOOLEAN,
        has_phone BOOLEAN,
        has_nfc BOOLEAN,
        confidence REAL,
        text_length INTEGER,
        tested_at TEXT
    )
    ''')

    conn.commit()
    conn.close()

    return test_results_dir, test_db


def run_ocr_on_image(image_path):
    """对单张图片运行OCR"""
    try:
        start_time = time.time()

        # 导入OCR模块
        from ocr_cli import _auto_roi, _run_rapidocr, extract_qr_codes, load_rgb_gray

        # 尝试使用_auto_roi（优化后的路径）
        try:
            ocr_result = _auto_roi(image_path)
            if not ocr_result:
                # 如果_auto_roi返回None，回退到标准OCR
                ocr_result, conf, _ = _run_rapidocr(image_path)
                confidence = conf
            else:
                confidence = 0.85  # _auto_roi的默认置信度
        except Exception as roi_error:
            # _auto_roi失败，回退到标准OCR
            print(f"  _auto_roi失败，回退到标准OCR: {roi_error}")
            ocr_result, confidence, _ = _run_rapidocr(image_path)

        processing_time = time.time() - start_time

        # 分析结果
        has_model = bool("PC-" in ocr_result or "QHR" in ocr_result.upper())
        has_phone = bool("400" in ocr_result)
        has_nfc = bool("NFC" in ocr_result.upper())

        return {
            "success": True,
            "ocr_result": ocr_result,
            "processing_time": processing_time,
            "confidence": confidence,
            "has_model": has_model,
            "has_phone": has_phone,
            "has_nfc": has_nfc,
            "text_length": len(ocr_result.strip()),
            "error": None
        }

    except Exception as e:
        return {
            "success": False,
            "ocr_result": "",
            "processing_time": 0,
            "confidence": 0,
            "has_model": False,
            "has_phone": False,
            "has_nfc": False,
            "text_length": 0,
            "error": str(e)
        }


def save_test_result(test_db, image_name, image_path, result):
    """保存测试结果到数据库"""
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()

    cursor.execute('''
    INSERT INTO test_results
    (image_name, image_path, ocr_result, processing_time, success, error,
     has_model, has_phone, has_nfc, confidence, text_length, tested_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        image_name,
        image_path,
        result['ocr_result'],
        result['processing_time'],
        result['success'],
        result['error'],
        result['has_model'],
        result['has_phone'],
        result['has_nfc'],
        result['confidence'],
        result['text_length'],
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()


def analyze_test_results(test_db):
    """分析测试结果"""
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()

    # 基础统计
    cursor.execute('''
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as success_count,
        AVG(processing_time) as avg_time,
        AVG(CASE WHEN success=1 THEN confidence ELSE NULL END) as avg_confidence,
        AVG(CASE WHEN success=1 THEN text_length ELSE NULL END) as avg_text_length
    FROM test_results
    ''')

    stats = cursor.fetchone()
    total, success_count, avg_time, avg_confidence, avg_text_length = stats

    # 关键信息识别统计
    cursor.execute('''
    SELECT
        SUM(CASE WHEN has_model=1 THEN 1 ELSE 0 END) as model_count,
        SUM(CASE WHEN has_phone=1 THEN 1 ELSE 0 END) as phone_count,
        SUM(CASE WHEN has_nfc=1 THEN 1 ELSE 0 END) as nfc_count
    FROM test_results WHERE success=1
    ''')

    model_count, phone_count, nfc_count = cursor.fetchone()

    # 失败案例统计
    cursor.execute('''
    SELECT image_name, error FROM test_results WHERE success=0
    ''')

    failed_cases = cursor.fetchall()

    # 性能分布
    cursor.execute('''
    SELECT
        MIN(processing_time) as min_time,
        MAX(processing_time) as max_time
    FROM test_results WHERE success=1
    ''')

    min_time, max_time = cursor.fetchone()

    conn.close()

    return {
        "total_tests": total,
        "success_count": success_count,
        "success_rate": success_count / total if total > 0 else 0,
        "avg_processing_time": avg_time,
        "avg_confidence": avg_confidence,
        "avg_text_length": avg_text_length,
        "model_count": model_count,
        "phone_count": phone_count,
        "nfc_count": nfc_count,
        "failed_cases": failed_cases,
        "min_time": min_time,
        "max_time": max_time
    }


def generate_test_report(analysis, test_results_dir):
    """生成测试报告"""
    report = f"""
# OCR真实图片测试报告

**测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**测试图片来源**: {TEST_PHOTOS_DIR}
**测试结果目录**: {test_results_dir}

## 测试概览

| 指标 | 数值 |
|------|------|
| 总测试数 | {analysis['total_tests']} |
| 成功数 | {analysis['success_count']} |
| 成功率 | {analysis['success_rate']*100:.1f}% |
| 平均处理时间 | {analysis['avg_processing_time']*1000:.0f}ms |
| 平均置信度 | {analysis['avg_confidence']*100:.1f}% |
| 平均文本长度 | {analysis['avg_text_length']:.0f}字符 |
| 最快处理时间 | {analysis['min_time']*1000:.0f}ms |
| 最慢处理时间 | {analysis['max_time']*1000:.0f}ms |

## 关键信息识别统计

| 关键信息 | 识别数量 | 识别率 |
|----------|----------|--------|
| 型号标识 (PC-/QHR) | {analysis['model_count']} | {analysis['model_count']/analysis['success_count']*100 if analysis['success_count'] > 0 else 0:.1f}% |
| 电话号码 (400) | {analysis['phone_count']} | {analysis['phone_count']/analysis['success_count']*100 if analysis['success_count'] > 0 else 0:.1f}% |
| NFC标识 | {analysis['nfc_count']} | {analysis['nfc_count']/analysis['success_count']*100 if analysis['success_count'] > 0 else 0:.1f}% |

"""

    if analysis['failed_cases']:
        report += "## 失败案例\n\n"
        for image_name, error in analysis['failed_cases']:
            report += f"- **{image_name}**: {error}\n"
        report += "\n"
    else:
        report += "## 失败案例\n\n[SUCCESS] 所有测试均成功，无失败案例！\n\n"

    return report


def show_detailed_results(test_db):
    """显示详细测试结果"""
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()

    cursor.execute('''
    SELECT image_name, processing_time, confidence, text_length,
           has_model, has_phone, has_nfc, success
    FROM test_results
    ORDER BY processing_time DESC
    ''')

    results = cursor.fetchall()
    conn.close()

    print("\n" + "="*80)
    print("详细测试结果")
    print("="*80)
    print(f"{'图片名称':<25} {'处理时间':<12} {'置信度':<10} {'文本长度':<10} {'模型':<6} {'电话':<6} {'NFC':<6} {'状态':<8}")
    print("-"*80)

    for row in results:
        image_name, proc_time, conf, text_len, has_model, has_phone, has_nfc, success = row

        status = "[OK]" if success else "[FAIL]"
        model_mark = "Y" if has_model else "N"
        phone_mark = "Y" if has_phone else "N"
        nfc_mark = "Y" if has_nfc else "N"

        print(f"{image_name:<25} {proc_time*1000:>8.0f}ms  {conf*100:>6.1f}%   {text_len:>8}    {model_mark:^6} {phone_mark:^6} {nfc_mark:^6} {status:^8}")

    print("="*80)


def main():
    """主函数"""
    print("开始OCR真实图片测试")
    print(f"测试照片目录: {TEST_PHOTOS_DIR}")
    print(f"项目根目录: {PROJECT_ROOT}")
    print()

    # 检查测试照片目录
    if not os.path.exists(TEST_PHOTOS_DIR):
        print(f"测试照片目录不存在: {TEST_PHOTOS_DIR}")
        return

    # 获取测试图片列表
    test_images = []
    for filename in os.listdir(TEST_PHOTOS_DIR):
        if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            image_path = os.path.join(TEST_PHOTOS_DIR, filename)
            test_images.append((filename, image_path))

    if not test_images:
        print(f"在 {TEST_PHOTOS_DIR} 中没有找到测试图片")
        return

    print(f"找到 {len(test_images)} 张测试图片")
    print()

    # 设置测试环境
    print("设置测试环境...")
    test_results_dir, test_db = setup_test_environment()
    print(f"测试环境已创建: {test_results_dir}")
    print()

    # 运行测试
    print("开始运行测试...")
    print("-" * 80)

    success_count = 0
    fail_count = 0

    for i, (image_name, image_path) in enumerate(test_images, 1):
        print(f"[{i}/{len(test_images)}] 处理: {image_name}")

        # 运行OCR
        result = run_ocr_on_image(image_path)

        # 保存结果
        save_test_result(test_db, image_name, image_path, result)

        # 显示结果
        if result['success']:
            success_count += 1
            print(f"  [SUCCESS] 时间: {result['processing_time']*1000:.0f}ms, "
                  f"文本长度: {result['text_length']}, "
                  f"模型: {'Y' if result['has_model'] else 'N'}, "
                  f"电话: {'Y' if result['has_phone'] else 'N'}, "
                  f"NFC: {'Y' if result['has_nfc'] else 'N'}")

            # 显示OCR结果预览
            preview = result['ocr_result'][:100].replace('\n', ' ')
            print(f"  [PREVIEW] 结果: {preview}...")
        else:
            fail_count += 1
            print(f"  [FAILED] 错误: {result['error']}")

        print()

    print("-" * 80)
    print(f"测试完成！成功: {success_count}, 失败: {fail_count}")
    print()

    # 分析结果
    print("分析测试结果...")
    analysis = analyze_test_results(test_db)

    # 生成报告
    report = generate_test_report(analysis, test_results_dir)
    report_file = os.path.join(test_results_dir, "test_report.md")

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"测试报告已保存: {report_file}")
    print()

    # 显示摘要
    print("="*80)
    print("测试结果摘要")
    print("="*80)
    print(f"总测试数: {analysis['total_tests']}")
    print(f"成功数: {analysis['success_count']}")
    print(f"成功率: {analysis['success_rate']*100:.1f}%")
    print(f"平均处理时间: {analysis['avg_processing_time']*1000:.0f}ms")
    print(f"平均置信度: {analysis['avg_confidence']*100:.1f}%")
    print(f"型号标识识别: {analysis['model_count']}/{analysis['success_count']} "
          f"({analysis['model_count']/analysis['success_count']*100 if analysis['success_count'] > 0 else 0:.1f}%)")
    print(f"电话号码识别: {analysis['phone_count']}/{analysis['success_count']} "
          f"({analysis['phone_count']/analysis['success_count']*100 if analysis['success_count'] > 0 else 0:.1f}%)")
    print(f"NFC标识识别: {analysis['nfc_count']}/{analysis['success_count']} "
          f"({analysis['nfc_count']/analysis['success_count']*100 if analysis['success_count'] > 0 else 0:.1f}%)")
    print("="*80)
    print()

    # 显示详细结果
    show_detailed_results(test_db)

    # 显示完整报告
    print()
    print("="*80)
    print("完整测试报告")
    print("="*80)
    print(report)
    print("="*80)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)