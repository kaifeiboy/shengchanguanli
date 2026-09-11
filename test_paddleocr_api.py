#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速测试PaddleOCR API"""

import os
import sys

def test_paddleocr_api():
    """测试PaddleOCR的API"""
    print("Testing PaddleOCR API...")
    
    test_images = ["vq_closeup.jpg", "jq_closeup.jpg"]
    available_images = [img for img in test_images if os.path.exists(img)]
    
    if not available_images:
        print("No test images found")
        return
    
    try:
        from paddleocr import PaddleOCR
        print("OK PaddleOCR imported successfully")
        
        # 创建实例
        ocr = PaddleOCR(lang='ch')
        print("OK PaddleOCR instance created")
        
        # 测试识别
        for test_image in available_images:
            print(f"\nTesting: {test_image}")
            print("-" * 60)
            
            try:
                # 尝试新的API
                result = ocr.predict(test_image)
                print(f"Result type: {type(result)}")
                print(f"Result length: {len(result) if result else 0}")
                
                if result and len(result) > 0:
                    first_item = result[0]
                    print(f"First item type: {type(first_item)}")
                    print(f"First item: {first_item[:100] if isinstance(first_item, str) else str(first_item)[:100]}")
                    
                    # 尝试提取文本
                    if isinstance(first_item, list):
                        texts = []
                        for item in first_item:
                            if isinstance(item, (list, tuple)) and len(item) >= 1:
                                text = str(item[0]) if len(item) > 0 else ""
                                texts.append(text)
                        
                        if texts:
                            combined_text = "\n".join(texts)
                            print(f"Extracted text: {combined_text[:100]}...")
                        
            except Exception as e:
                print(f"ERROR: {str(e)[:100]}")
                import traceback
                traceback.print_exc()
    
    except Exception as e:
        print(f"Failed to test PaddleOCR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_paddleocr_api()