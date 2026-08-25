# v19.19 修复：照片侧打标文字被 CAD 规则误灰化

## 问题描述
用户报告：多张图纸中**以前调试好的一些文本的匹配**（序列号、型号、安全标签、A/B作业标识等）被识别为图标或 CAD 标注并灰化，不再参与匹配。不只是单个型号，是跨图纸通用问题。

## 根因诊断

### 代码级根因
`diff_visualizer.py` step3（照片→块匹配循环）第 1797 行：
```python
if _is_cad_annotation(pt):        # ← 一揽子灰化
    result["grayRegions"].append(bbox)
    continue
```

`_is_cad_annotation()` 是组合函数，内部依次检查：
1. `_is_dimension` → 尺寸数字（合理灰化）
2. `_is_annotation_note` → **命中 `CAD_STOPWORDS` 含 `"作业"`** → `"A作业"` 被当 CAD 标注误灰 ❌
3. `_is_lcd_display_text` → **精确匹配 `"设定"`** → 按钮标签被当 LCD 屏显误灰 ❌
4. `_is_serial_number` → 已在 step3 更早处理
5. `_is_cjk_garbage` → `"银务点检"`/`"自清浩"` 被当 OCR 噪声误灰 ⚠️

### 为什么之前没暴露？
v19.18 Phase 1 将块图 OCR 从"DB RawText"改为"live RapidOCR"。DB RawText 是切图时的一次性 OCR 结果（可能文本不同/更干净），而 live OCR 产生的原始文本更容易触发这些 CAD 规则。Phase 1 暴露了这个隐藏问题。

### 核心矛盾
**CAD/LCD 灰化规则是为工程图纸文本设计的**，但 step3 把它们套用到了**实物照片激光打标文字**上：
- 图纸上的 "A作业" = CAD 工序标注 → 应过滤 ✅（在 step4 块侧）
- 照片上的 "A作业" = 产品标签激光刻印 → 应参与匹配 ❌（不应在 step3 灰化）

## 修复方案（v19.19）

### 改动位置
仅 `diff_visualizer.py` step3（约第 1797 行），一处修改。

### 改动内容
```python
# 旧代码（v19.18）—— 过于宽泛：
if _is_cad_annotation(pt):          # ← 5 种规则一揽子灰化
    result["grayRegions"].append(bbox)
    continue

# 新代码（v19.19）—— 照片侧仅保留两类明确非匹配文本：
if _is_dimension(pt):               # 尺寸数字(80.5, 30±0.5) —— 永远不是产品标识
    result["grayRegions"].append(bbox)
    continue
if _is_cjk_garbage(_norm_text(pt)): # 纯 OCR 噪声(3-6字无意义CJK) —— 不是任何真实词汇
    result["grayRegions"].append(bbox)
    continue
```

### 设计原则
- **step3（照片侧）**：保守灰化——只灰化 100% 确定不是打标标识的文字（纯数字尺寸 + 纯 OCR 垃圾噪声）
- **step4（块侧）**：保持原有 `_is_cad_annotation` 全量规则不变——块图来自 CAD 图纸，CAD 规则适用

## 验证结果

### did=132 (YCWA15NCWQ) 单图验证
| 块 | 修复前 G | 修复后 G | 恢复的文字 |
|---|---|---|---|
| blk=0 | 5 | **6** | `A作业` |
| blk=1 | 5 | **7** | `B作业`, `激光刻印（灰色），位置居中` |
| blk=2 | 0 | **3** | `设定`, `银务点检`, `YORK?` |
| blk=4 | 0 | **1** | `A作业` |
| blk=5 | 0 | **1** | `B作业` |

### 全量扫描（22 张图纸 99 块）
- **受影响图纸数**：19 张
- **恢复文字总数**：**96 个**
- 恢复的关键文字类型：
  - 块标识：`A作业`, `B作业`（6 处）
  - 型号：`型号：CCS-32W3D`, `型号：YCTA113CGQ` 等（3+ 处）
  - 安全警告：`禁止按入强电`, `警告：A注意强电`（4 处）
  - 按钮标签：`设定`, `室温`, `自动`, `静音`, `定时`（10+ 处）
  - 工艺说明：`激光打标`, `镭雕此标识`, `镭雕开关标识`（15+ 处）

### 编译
✅ `py_compile` 通过（仅 1 个已存在的 escape sequence warning）

### 回归验证
🔄 `real_match_regression.py` 全量 99 块回归后台运行中（任务 TZd7Cd），对比基线 R/Y/G/Gray = 62/41/243/861

## 风险评估
- **风险等级**：低
- **唯一风险**：某些原本被正确灰化的 CAD 注释文字（如 `仅JQ国产化`、`镭雕此标识`）现在会进入正常匹配流程。但由于它们在块侧（step4）DB RawText 中通常不存在，会落入黄框（photoExclusive）而非绿框——不影响"缺标"判定准确性。
- **缓解**：如果后续发现这类文字产生大量假黄框，可对 step3 增加 `_has_label_feature` 式的白名单例外（类似已有的跨块溢出过滤）。

## 下一步
- 等回归确认零退化后，v19.19 即时生效（Python 改动无需重启 Platform）
- 继续推进 Phase 2（照片 ROI 位置感知）根除 R1 跨块溢出
