# 自动切图规则验证报告（PaddleOCR PP-DocLayoutV3 / ONNX 绕 AVX2）

**日期**：2026-07-24
**结论**：PaddleOCR 自动切图规则已稳定验证，**正式纳入生产作为最终切图规则**（auto 默认；旧 v6 红框仅作保底）。

## 1. 为什么绕开 Paddle 原生推理
- 本机 CPU = Intel i5-2430M（Sandy Bridge, 2011），**仅 AVX、无 AVX2**。
- `paddlepaddle` 的 Inference 预测器在 `create_predictor` 时发射 AVX2 指令 → 确定性崩溃 `rc=3221225477`（实测 `PaddleOCR` 文字模型、PP-DocLayoutV3 均崩；eager/jit 路径正常）。
- 修法：改用 **ONNX Runtime** 跑 PP-DocLayoutV3（模型即 PaddleOCR 官方同款经 Paddle2ONNX 转换，满足"用 PaddleOCR 分图"硬要求）。本机 `onnxruntime 1.27` 在 AVX-only CPU 已实证可用（RapidOCR 同款运行时）。

## 2. 落地实现
| 文件 | 作用 |
|------|------|
| `data/models/PP-DocLayoutV3.onnx` (130MB) | 模型，来源 hf-mirror `alex-dinh/PP-DocLayoutV3-ONNX`（本机 paddle2onnx 与 paddle 3.3.1 ABI 不匹配，改下现成 ONNX） |
| `pp_doclayout_onnx.py` | ONNX 推理模块，懒加载 `ort.InferenceSession(..., ["CPUExecutionProvider"])` |
| `segment_blocks_auto.py` | `detect_auto_regions()` 调 ONNX（不再 import paddle） |
| `segment_blocks.py` | `segment()` 步骤 0 先调 auto，有块→`v7-auto-paddlev3`；无/异常→退回 v6 红框保底 |

**预处理**：BICUBIC 800×800（keep_ratio=False）→ `/255`（无 ImageNet 归一化）→ CHW；输出 `(N,7)=[cls,score,x1,y1,x2,y2,order]`，坐标已回原图。
**舍弃规则**：`KEEP_CLS={14:image,3:chart}`、`MIN_SCORE=0.60`、面积比 0.3%–40%、宽高比 ≤10（滤扁长非部件误检）。

## 3. 验证结果（17 张 `生产打标效果图/*Model.pdf`）
全部 `mode=auto-paddlev3`，**AUTO = 17/17，零 AVX2 崩溃，零 v6 回退**：

| 图纸 | 块数 | 图纸 | 块数 |
|------|------|------|------|
| 4K3GR 线控器 | 5 ✅与 JSON 5 块 IoU 0.79–0.93 | PC-P1HEQ | 5 |
| HYXC-VF01 | 2 | 新约克86(YCWA15NCBQ) | 4 |
| HYXC-VH01GZ | 7 | 约克86彩屏(YCWA17NCWQ)14位码 | 8 |
| HYXC-VH02 | 1 | 约克86彩屏(YCWA17NCWQ)7位码 | 7 |
| HYXC-VK01 | 3 | 约克彩屏集控 YCTA113CGQ | 4 |
| PC-P1HJQ | 6 | 新约克86 线控器 | 7 |
| PC-P1HPQ3 | 2 | 水模块 HSXC-FN01 | 2 |
| PC-P1HPQA | 2 | | |
| PC-P1HVQA | 6 | | |
| PC-P1HVQ | 6 | | |

块数 1–8 均为合理产品部件视图数；4K3GR 命中用户提供的 PaddleOCR-VL-1.6 JSON ground truth。

## 4. 生产接线（无需改 C#）
- `DrawingService` 经 `_ocr.CreatePythonPsi(_segmentScript,"segment",...)` 调 `segment_blocks.py`，契约不变。
- `_pythonExe` 默认 `envs/default\Scripts\python.exe`（含 onnxruntime）→ ONNX 路径自动生效。
- Python 脚本与模型均为运行时加载，**免 build / 免重启**。

## 5. 注意事项
- 模型 `data/models/PP-DocLayoutV3.onnx`(130MB) 须随部署留存（缺失时 auto 返回 None，自动退回 v6 保底）。
- 回归脚本保留：`_batch_onnx_validate.py`。
