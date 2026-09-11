#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能OCR决策模块
通过分析首次OCR结果，智能决定是否需要二次OCR
目标：减少不必要的二次OCR处理，提升整体性能
"""

import re


def should_skip_second_ocr_enhanced(first_result, image_features=None):
    """增强的二次OCR跳过判断（保守但更智能）

    Args:
        first_result: 首次OCR结果 {
            'text': str,
            'confidence': float,
            'detections': list,
            'text_length': int,
            'has_key_info': dict
        }
        image_features: 图像特征 {
            'resolution': tuple,
            'file_size': int,
            'aspect_ratio': float
        }

    Returns:
        (should_skip: bool, reason: str)
    """
    if not first_result or not first_result.get('text'):
        return False, "empty_result"

    text = first_result.get('text', '')
    confidence = first_result.get('confidence', 0.0)
    detections = first_result.get('detections', [])
    text_length = first_result.get('text_length', len(text))
    key_info = first_result.get('has_key_info', {})

    # 跳过条件1：首次置信度很高（保守阈值0.85）
    if confidence > 0.85:
        return True, f"high_confidence_{confidence:.2f}"

    # 跳过条件2：已识别到所有关键信息
    has_model = key_info.get('has_model', False)
    has_phone = key_info.get('has_phone', False)
    has_nfc = key_info.get('has_nfc', False)

    # 如果识别到型号和电话，通常质量足够
    if has_model and has_phone:
        return True, "complete_key_info"

    # 跳过条件3：检测框很集中（环境干扰少）
    if len(detections) > 0:
        if is_concentrated_detections(detections):
            return True, "concentrated_detections"

    # 跳过条件4：文本质量很好（长度适中+置信度尚可）
    if 15 <= text_length <= 50 and confidence > 0.75:
        return True, f"good_text_quality_{text_length}chars"

    # 跳过条件5：检测框数量合理（不是太少也不是太多）
    if 3 <= len(detections) <= 8 and confidence > 0.7:
        return True, f"reasonable_detection_count_{len(detections)}"

    # 不跳过：需要二次OCR提升质量
    return False, "need_second_ocr"


def is_concentrated_detections(detections, threshold_ratio=0.3):
    """判断检测框是否集中

    Args:
        detections: 检测框列表
        threshold_ratio: 集中度阈值

    Returns:
        bool: 是否集中
    """
    if len(detections) < 2:
        return True  # 单个检测框天然集中

    # 计算所有检测框的中心点
    centers = []
    for detection in detections:
        if isinstance(detection, list) and len(detection) >= 2:
            box = detection[0]  # 检测框坐标
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = (min(xs) + max(xs)) / 2
            cy = (min(ys) + max(ys)) / 2
            centers.append((cx, cy))

    if len(centers) < 2:
        return True

    # 计算中心点的分散程度
    cx_values = [c[0] for c in centers]
    cy_values = [c[1] for c in centers]

    cx_range = max(cx_values) - min(cx_values)
    cy_range = max(cy_values) - min(cy_values)

    # 计算平均分散度
    avg_range = (cx_range + cy_range) / 2

    # 估算图片大小（基于检测框位置）
    max_x = max(max(p[0] for p in detection[0]) for detection in detections)
    max_y = max(max(p[1] for p in detection[0]) for detection in detections)
    image_size = max(max_x, max_y)

    # 判断集中度
    concentration_ratio = avg_range / image_size if image_size > 0 else 0

    return concentration_ratio < threshold_ratio


def analyze_first_result(first_result):
    """分析首次OCR结果，提取关键特征

    Args:
        first_result: 首次OCR原始结果

    Returns:
        分析结果字典
    """
    if not first_result:
        return None

    # 提取文本
    if isinstance(first_result, tuple) and len(first_result) >= 1:
        text = first_result[0] if isinstance(first_result[0], str) else ""
    else:
        text = str(first_result) if first_result else ""

    # 提取置信度
    if isinstance(first_result, tuple) and len(first_result) >= 2:
        confidence = float(first_result[1]) if isinstance(first_result[1], (int, float)) else 0.85
    else:
        confidence = 0.85

    # 检测关键信息
    has_model = bool(re.search(r"PC[- ]?[A-Za-z0-9]{2,}|QHR\d{1,}", text, re.I))
    has_phone = bool(re.search(r"400[-]?\d{7}", text))
    has_nfc = bool("NFC" in text.upper())

    # 检测框（如果有）
    detections = []
    if isinstance(first_result, tuple) and len(first_result) >= 3:
        if isinstance(first_result[2], list):
            detections = first_result[2]

    return {
        "text": text,
        "confidence": confidence,
        "detections": detections,
        "text_length": len(text.strip()),
        "has_key_info": {
            "has_model": has_model,
            "has_phone": has_phone,
            "has_nfc": has_nfc
        },
        "skip_probability": estimate_skip_probability(text, confidence, has_model, has_phone)
    }


def estimate_skip_probability(text, confidence, has_model, has_phone):
    """估计跳过二次OCR的概率

    Args:
        text: OCR文本
        confidence: 置信度
        has_model: 是否有型号
        has_phone: 是否有电话

    Returns:
        float: 跳过概率 (0-1)
    """
    probability = 0.0

    # 置信度因子 (0-0.4)
    if confidence > 0.9:
        probability += 0.4
    elif confidence > 0.85:
        probability += 0.3
    elif confidence > 0.8:
        probability += 0.2
    elif confidence > 0.75:
        probability += 0.1

    # 关键信息因子 (0-0.3)
    if has_model and has_phone:
        probability += 0.3
    elif has_model or has_phone:
        probability += 0.15

    # 文本质量因子 (0-0.3)
    text_len = len(text.strip())
    if 15 <= text_len <= 50:
        probability += 0.3
    elif 10 <= text_len <= 60:
        probability += 0.15
    elif text_len > 5:
        probability += 0.05

    return min(probability, 1.0)


def generate_decision_report(first_result, decision, reason):
    """生成决策报告

    Args:
        first_result: 首次OCR结果
        decision: 是否跳过
        reason: 跳过原因

    Returns:
        报告字符串
    """
    analysis = analyze_first_result(first_result)
    if not analysis:
        return "无法分析首次OCR结果"

    report = f"""
=== OCR决策报告 ===

首次OCR分析:
- 文本长度: {analysis['text_length']} 字符
- 置信度: {analysis['confidence']*100:.1f}%
- 关键信息: 型号={'Y' if analysis['has_key_info']['has_model'] else 'N'} 电话={'Y' if analysis['has_key_info']['has_phone'] else 'N'} NFC={'Y' if analysis['has_key_info']['has_nfc'] else 'N'}
- 检测框数量: {len(analysis['detections'])}
- 跳过概率: {analysis['skip_probability']*100:.1f}%

决策结果:
- 是否跳过二次OCR: {'是' if decision else '否'}
- 决策原因: {reason}

文本预览: {analysis['text'][:100]}...
"""

    return report


if __name__ == "__main__":
    print("=== 智能OCR决策测试 ===\n")

    # 测试案例1：高质量结果
    high_quality_result = (
        "服务热线：4008601111\nPC-P1HJQ",
        0.92,
        []  # 模拟检测框
    )

    decision, reason = should_skip_second_ocr_enhanced(
        analyze_first_result(high_quality_result)
    )

    print("测试1: 高质量OCR结果")
    print(f"决策: {'跳过二次OCR' if decision else '需要二次OCR'}")
    print(f"原因: {reason}")
    print(generate_decision_report(high_quality_result, decision, reason))

    # 测试案例2：低质量结果
    low_quality_result = (
        "服务热\n400\nPC-P",
        0.65,
        []
    )

    decision, reason = should_skip_second_ocr_enhanced(
        analyze_first_result(low_quality_result)
    )

    print("\n测试2: 低质量OCR结果")
    print(f"决策: {'跳过二次OCR' if decision else '需要二次OCR'}")
    print(f"原因: {reason}")
    print(generate_decision_report(low_quality_result, decision, reason))

    print("\n=== 测试完成 ===")