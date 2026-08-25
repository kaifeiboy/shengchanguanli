# v19.18 Phase 1 — 同源实时 OCR（底层架构重建启动）

> 目标：文本 + 图标按规则匹配准确率 **100%**。本方案分四阶段重建匹配范式，Phase 1 已落地。

## 一、为什么之前"永远改不完"

v19.x 系列修复（v19.11~v19.17）全部工作在**错误的匹配范式**之上：

- `run()` 比较的是 **「整张照片的全局文字袋」 vs 「块图的 DB RawText 文字袋」**
- 照片侧：生产路径 `photo_text_override` 为空 → Python 端 live RapidOCR（✅ 正确）
- 块图侧：`block_text_override` = segmentation 时写入 DB 的 RawText，作为**唯一匹配基准**（❌ 陈旧、可能与照片不同源）

→ 这导致 **R2（基准为陈旧 DB）** 与 **R3（照片/块图不同引擎/不同源）**，是"输出不稳定"的最大隐藏源。
v19.x 在文字袋层打补丁（清 QR 垃圾、序列号顺序、跨块补丁），所以每修一类另一类又冒出来。

## 二、Phase 1 改动（仅 `diff_visualizer.py` 的 `run()`，框架不动）

| 侧 | 改前 | 改后（v19.18） |
|----|------|------|
| **照片** | `photo_text_override` 为空时 live；传入时整段用 override（占位 bbox） | **live RapidOCR 为主**；override 仅当 live 几乎为空时兜底。图标过滤**复用** live_photo，不再重复 OCR |
| **块图** | `block_text_override`(DB RawText) 为唯一基准 | **live RapidOCR 为主**（优先复用 C# 缓存块 OCR，同为 RapidOCR；否则实时 OCR）；**DB RawText 降为 live 失败时的兜底** |

代码级实证：`DrawingService.cs:2126` 生产第 6 参为空 → 照片本就 live；`block_text_override`（`:2125`）= DB RawText 才是症结。

**根除**：R2（陈旧 DB 不再是基准）+ R3（照片/块图同引擎 RapidOCR、同预处理）。

## 三、验证（进行中）

- 编译：✅ `py_compile` 通过
- 单块自比对：✅ 无崩溃；did=132/2、115/4、123/3 表现合理（无假 R/Y）
- 全量回归 `real_match_regression.py`：🔄 后台运行中（任务 `ghGpDb`），对比旧基线 R/Y/G/Gray = 62/41/243/861

## 四、四阶段路线图（框架不变，仅改 `diff_visualizer.py`）

| 阶段 | 根除根因 | 核心动作 | 风险 |
|------|---------|---------|------|
| **P1 同源实时 OCR** | R2 + R3 | 照片/块图都走 live RapidOCR，DB 仅兜底 | 低（已落地） |
| **P2 照片 ROI 位置感知** | R1 | 检测标签牌矩形裁剪后再 OCR，从源头消除跨块溢出，让 `_has_label_feature` 补丁退休 | 低 |
| **P3 字段抽取语义匹配** | R4 + R5 | 型号/序列号/QR/参数/安全语 字段对字段容错（序列号静默、QR 比 payload、参数数值归一） | 中（字段字典复用现有规则） |
| **P4 字段级度量** | 验收 | `fleet_sweep.py`/`real_match_regression.py` 升级为字段级 precision/recall，量化"准不准" | 低 |

## 五、护栏（每次改动后必跑）

- `fleet_sweep.py`：静态盲区探测（22 图 99 块 RawText/几何，暴露过滤器漏掉的 token）
- `real_match_regression.py`：动态全量回归（27 图自比对 + did=115 真实照片）

## 六、诚实预期

Phase 1 是**必要但不充分**——它解决了"基准错误 + 引擎不一致"这个最隐蔽的根因。
要达到 100%，仍需 P2（位置感知，解决跨块溢出误报）与 P3（字段语义，解决连写/分行/数值格式脆弱匹配）。
P1 完成后立即推进 P2，逐阶段用护栏验证零退化。
