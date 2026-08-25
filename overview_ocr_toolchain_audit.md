# 打标首件对比项目 · OCR 工具链核查总览（2026-07-30）

> 核查方式：直接读源码 + 运行环境实测（venv 包可用性 / 系统 Tesseract / pyzbar DLL）。
> 结论先行，逐条附依据与风险。

## 一、当前 OCR / 识别工具清单（含 OCR 邻接的版面与条码能力）

**文字识别类**
1. RapidOCR（`rapidocr_onnxruntime`，即 PaddleOCR 检测+识别的 ONNX 版）— 文字主引擎
2. Tesseract（系统 `C:\Program Files\Tesseract-OCR\tesseract.exe`，`subprocess` 调 chi_sim+eng）— 文字降级备用

**版面 / 切图类**
3. PP-DocLayoutV3（ONNX Runtime，PaddleOCR 官方版面模型）— 切图主模型（auto-paddlev3）
4. OpenCV 轮廓检测（`detect_by_contours`）— 线框视图矩形候选，混合策略补充

**二维码 / 条码类**
5. zxing-cpp（`zxingcpp`）— 照片二维码/条码主解码器（v19.15，纯 pip 无 DLL）
6. pyzbar — 二维码/条码兜底解码（**生产环境 zbar DLL 缺失，实际失效**）
7. cv2.QRCodeDetector — 文字 OCR 流程内的 QR 增强提取（`extract_qr_codes`）
8. `_qr_finders`（OpenCV 定位符聚类）— 解码器全未命中时的兜底

## 二、环境实测（判断依据）
- venv 可用：rapidocr_onnxruntime ✅ / zxingcpp ✅ / pyzbar ✅(但 DLL 加载失败) / cv2 ✅ / onnxruntime ✅
- pytesseract：**缺失**（不影响，因走 exe 子进程）
- 系统 Tesseract：**已安装** ✅
- pyzbar 生产实测：`zbar_library.load_objects` 失败 → 生产侧不可用

## 三、PaddleOCR 角色
- (a) RapidOCR = 文字识别主引擎；(b) PP-DocLayoutV3 = 切图主模型。二者同属 PaddleOCR 家族。
- 与 Tesseract：**反向备用**——RapidOCR 是主，Tesseract 才是备用。
- 与 zxing/pyzbar：**QR 解码不是 PaddleOCR 能力范畴**，互不备用，独立能力栈。
- 与 PP-DocLayoutV3：流水线上下游协作（先版面切图，再 RapidOCR 识块内文字）。

## 四、准确率适配性判断：部分（Partial）
- 文字场景：RapidOCR 是唯一稳定达标引擎（real_match_regression 基线 R/Y/G/Gray=62/41/243/861，零退化；冷启动竞态修复前 fallback Tesseract 致"忽好忽坏"反证 Tesseract 不可靠）。
- **PaddleOCR 无法覆盖**：①二维码/条码解码（RapidOCR 不解码 QR 内容）；②极端线框视图切图（PP-DocLayoutV3 对 4K3GR 仅检出 1/4，需 OpenCV 轮廓补充）。

## 五、统一可行性：不能完全统一成"仅 PaddleOCR"
- RapidOCR + PP-DocLayoutV3 已同属 Paddle 栈（已"统一"）。
- QR 解码不在 PaddleOCR 能力内；强行并入需新增 Paddle QR 模型（是扩不是统）。
- 可行收敛：去 pyzbar（换 zxing-cpp 单解码器）、Tesseract 降为灾难兜底、OpenCV 本就是底层依赖保留。

## 六、强制"只用 PaddleOCR"的风险
1. QR 解码能力丧失（高）→ 已修的"二维码被标示/假红框"全面复发。缓解：保留 zxing-cpp。
2. 切图退化（中-高）→ 线框视图漏切。缓解：保留 OpenCV 轮廓融合。
3. 灾难恢复缺口（中）→ RapidOCR 加载失败则 OCR 全空。缓解：Tesseract 留作灾难分支。
4. 准确率（正向）→ 去掉劣质 Tesseract 常规路径反而提准确率，但需确保 RapidOCR 永远可用（L1 warm-up 已解）。
5. 数据流程兼容（低）→ 引擎标记字段需同步 C# OcrService。缓解：改后跑 fleet_sweep + real_match_regression。
6. 进度（中）→ "QR 重写进 Paddle"是数月级工作。缓解：分阶段，QR 维持现状。

## 七、整体建议：分阶段合并（推荐）
- 阶段0（现在，低风险）：删 pyzbar 依赖，QR 统一到 zxing-cpp；Tesseract 仅留灾难兜底。
- 阶段1（保持）：RapidOCR 文字 + PP-DocLayoutV3 切图（同 Paddle 栈）；OpenCV 作底层依赖保留。
- 阶段2（暂缓）：不把 QR 塞进 PaddleOCR，继续 zxing-cpp，除非有强需求+愿投入 QR 模型训练。
- 护栏：任何收敛改动后跑 fleet_sweep.py（静态盲区）+ real_match_regression.py（动态全量）验证零退化。
