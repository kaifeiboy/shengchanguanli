# 匹配准确性 · 底层架构诊断与彻底解决路径（2026-07-30）

> 结论：难点不在 OCR 引擎，而在**匹配范式**本身。v19.x 是在"错误范式上打补丁"，故问题反复。
> 框架（.NET/SQLite/H5）不变的前提下，可在 `diff_visualizer.py` 的 `run()` 内重建匹配范式。

## 一、代码实证：当前 run() 的匹配范式
`run(photo_path, block_path, ..., block_text_override, photo_text_override)` 实际是：
- **照片侧**：`photo_text_override`（C# 对整个照片的全局 OCR），所有行赋**占位 bbox `[[0,0],[10,0],[10,10],[0,10]]`** → **零位置感知**（diff_visualizer.py:1653-1658）。
- **块侧**：`block_text_override`（数据库 RawText，陈旧）映射到块图 OCR 的 bbox；即使 `_ocr_image(block_path)` 能实时出文本，也**被 override 丢弃**（diff_visualizer.py:1662-1677）。
- 匹配 = 照片全局文字袋 vs 块 DB 文字袋；位置只用于图标抑制；跨块溢出靠 `_has_label_feature` 启发式补丁（diff_visualizer.py:1824）。

→ 三个架构级缺项：**①照片无位置感知(ROI)**、**②两侧文本不同源(实时 vs 陈旧DB)**、**③字段无语义**。

## 二、底层根因（架构级，非规则 bug）
- **R1 无几何/位置感知**：照片全局 OCR，标签条大 bbox 跨界覆盖尺寸/接口块 → 假多标。补丁 `_has_label_feature` 是症状治疗。
- **R2 比对基准错误（最致命）**：块的"期望文本"是陈旧 DB RawText，而非实时同引擎 OCR。DB 脏数据（小字漏读/人工错改如 YCWATSNCWQ/0净NFC）直接污染匹配。live OCR 明明可用却被 override 丢弃。
- **R3 双引擎不确定性**：RapidOCR↔Tesseract 冷启动 fallback → "忽好忽坏"（L1 已缓解但未根除范式）。
- **R4 QR/图标/字段无语义**：只做"抑制/灰化"，不做结构化理解 → 持续打补丁（v19.14/15/16）。
- **R5 行袋匹配无字段对齐**：顺序/连写/换行脆弱（"服务热线：400-620-6607"照片连写 vs CAD 分行）。
- **R6 无字段级准确率度量**：只能数红黄框，无法量化"到底准不准"。

## 三、彻底方案：字段感知的 ROI 匹配范式（仅改 diff_visualizer.py，框架不变）
- **阶段1（高杠杆·低风险）同源实时 OCR**：匹配时照片与块图**都走同一 live RapidOCR**；DB RawText 降级为"补充/参考"（合并 live 漏读的行），不再作唯一基准。根除 R2+R3。
- **阶段2 照片 ROI**：检测标签牌矩形区域裁剪后再 OCR，跨块溢出于源头消除，让 `_has_label_feature` 补丁退休。根除 R1。
- **阶段3 字段抽取+语义匹配**：型号/序列号/QR/参数/安全语 字段对字段容错（序列号静默、QR 比 payload、参数数值归一）。根除 R4+R5。
- **阶段4 字段级度量+回归**：升级 fleet_sweep/real_match_regression 为字段级 precision/recall。

## 四、可行性 & 护栏
- 阶段1/2 在框架内零成本可做；阶段3 所需字段字典（型号模式/序列号规则/安全词表）已基本具备；阶段2 标签牌检测用轮廓/MSER 即可。
- 风险：重写 run() 有回归 → 用现有 `fleet_sweep.py`(静态盲区)+`real_match_regression.py`(动态全量) 验证零退化；阶段1 单独立项先行。

## 五、建议
先落**阶段1（同源实时 OCR）**，护栏验证零退化后，再推进阶段2/3。这是"从源头保证准确"的真正落地，而非继续在文字袋范式上打补丁。
