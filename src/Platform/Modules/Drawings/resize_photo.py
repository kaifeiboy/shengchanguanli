import sys
from PIL import Image

# ⚠️ 本文件必须位于 src/Platform/Modules/Drawings/resize_photo.py
#    DrawingService.cs(MatchBlock) 按 <assembly>/../../../Modules/Drawings/resize_photo.py
#    定位本脚本，并以 if (File.Exists(...)) 判定——缺失则【静默跳过】降采样，
#    照片会按原图分辨率送 RapidOCR（大图 1008x2560 实测 37s+）→ H5 45s 超时。
#    历史教训：曾因"优化文件结构"被移至 _archived_diagnostics/ 导致降采样失效、对比超时。
#    若需重构路径，必须同步修改 DrawingService.cs 中的 resizeScript 拼接。

path = sys.argv[1]
img = Image.open(path)
w, h = img.size
if max(w, h) > 960:
    s = 960 / max(w, h)
    img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    # JPEG 不支持 alpha；不转换会在 RGBA 输入时抛异常，被 C# catch{} 吞掉 → 降采样静默失效
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, quality=90, optimize=True)
