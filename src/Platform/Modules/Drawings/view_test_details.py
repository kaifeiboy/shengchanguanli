#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查看测试详细结果
"""

import sqlite3
import os

# 测试结果数据库
test_db = r"e:\workaaa\shengchanguanli\src\test_results\test_run_20260828_150820\test_results.db"

if os.path.exists(test_db):
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()

    # 获取所有测试结果
    cursor.execute('''
    SELECT image_name, ocr_result, processing_time, confidence,
           has_model, has_phone, has_nfc, text_length
    FROM test_results
    ORDER BY processing_time DESC
    LIMIT 5
    ''')

    print("处理时间最长的5张图片的OCR结果：")
    print("=" * 100)

    for row in cursor.fetchall():
        image_name, ocr_result, proc_time, confidence, has_model, has_phone, has_nfc, text_length = row

        print(f"\n图片: {image_name}")
        print(f"处理时间: {proc_time*1000:.0f}ms")
        print(f"置信度: {confidence*100:.1f}%")
        print(f"文本长度: {text_length}")
        print(f"关键信息: 模型={'Y' if has_model else 'N'} 电话={'Y' if has_phone else 'N'} NFC={'Y' if has_nfc else 'N'}")
        print(f"OCR结果:\n{ocr_result}")
        print("-" * 100)

    conn.close()
else:
    print(f"测试数据库不存在: {test_db}")