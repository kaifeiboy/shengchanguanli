# -*- coding: utf-8 -*-
"""
ocr_with_bbox.py —— 封装 RapidOCR，返回带 bbox 的文本行 JSON
================================================================
供 diff_visualizer.py 调用，获取照片/图块中每段文字的精确位置。

输出（stdout，最后一行 JSON）：
  {
    "success": true,
    "lines": [
      {"text": "PC-P1HJQ", "bbox": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]], "conf": 0.89},
      ...
    ],
    "fullText": "PC-P1HJQ服务热线：4008601111",
    "duration": 0.342
  }

bbox 格式：[[左上x,左上y],[右上x,右上y],[右下x,右下y],[左下x,左下y]]
"""
import sys, json, time, traceback
import numpy as np
from PIL import Image


def ocr_image(image_path):
    """调用 RapidOCR，返回带 bbox 的结构化结果"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return {"success": False, "error": "RapidOCR not installed"}

    engine = RapidOCR()
    img = cv2_imread(image_path)
    if img is None:
        return {"success": False, "error": f"Cannot load image: {image_path}"}

    t0 = time.time()
    result, elapsed = engine(img)
    duration = time.time() - t0

    if not result:
        return {"success": True, "lines": [], "fullText": "", "duration": round(duration, 3)}

    lines = []
    texts = []
    for item in result:
        if len(item) < 3:
            continue
        box, txt, conf = item[0], item[1], float(item[2]) if item[2] else 0.0
        # 标准化 bbox：4 个 [[x,y],...] 点
        corners = [[float(p[0]), float(p[1])] for p in box]
        lines.append({
            "text": txt,
            "bbox": corners,
            "conf": conf
        })
        texts.append(txt)

    return {
        "success": True,
        "lines": lines,
        "fullText": "".join(texts),
        "duration": round(duration, 3)
    }


def cv2_imread(path):
    """用 PIL 加载图像（兼容所有格式）返回 numpy array (H,W,3) RGB"""
    try:
        pil = Image.open(path).convert("RGB")
        return np.asarray(pil)
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "Usage: ocr_with_bbox.py <image_path>"}))
        return 1

    path = sys.argv[1]
    if not __import__('os').path.exists(path):
        print(json.dumps({"success": False, "error": f"File not found: {path}"}))
        return 1

    result = ocr_image(path)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
