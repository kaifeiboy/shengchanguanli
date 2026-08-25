# did=132 R=1（QHRLA 缺标）根因确认报告

> 验证方式：生产等价路径 `temp/v1_prod_diag.py`（subprocess + 绝对路径 + C# 最小环境），引擎版本 v19.31。

## 结论

**R=1 是「跨产品比对」导致，不是 OCR 问题，按规则属于正确行为。无需改动匹配引擎。**

## 一、根因：用错了图纸比对

| 图纸 | 型号 | CAD 块型号码 | 实际产品 |
|------|------|-------------|----------|
| **did=112** | PC-P1HJQ | `QHR45` (blk0) | 用户照片即此产品（QR 解码=`QHR452005128EQ0024`） |
| **did=132** | YCWA15NCWQ | `QHRLA`(blk4) / `QHRLB`(blk5) | — |

用户把 **PC-P1HJQ / QHR45 照片** 拿到 **did=132 / YCWA15NCWQ / QHRLA** 的 CAD 上比对 → 两个完全不同的产品 → 型号不匹配 → R=1。

## 二、OCR 两侧均已准确（回答"能否加强 OCR"）

- **照片侧**：OCR 读 `品回QHR45`；**QR 机器解码=`QHR452005128EQ0024`** → 金标准证实照片**确实是 QHR45**，并非把 QHRLA 误读成 QHR45（无 A↔5 混淆）。
- **CAD 侧**：blk4 OCR 读 `QHRLA`（conf 0.829，清晰）；blk5=`QHRLB`。
- 全树扫描：did=132 的 134 张 match 照片 QR **全部=QHRLA**；`QHR45` 照片 **全部属于 did=112**（39 张）。

**→ 加强照片 OCR 不会改变结果：照片真为 QHR45，强化识别只会更确定它是 QHR45。**

## 三、生产等价复现（证据）

| 场景 | 输入 | 结果 |
|------|------|------|
| A. 正确比对 | did=132 照片(QR=QHRLA) vs did=132 blk4 CAD | blk4 **R=0**；blk5 CAD=QHRLB vs 照片QHRLA → **R=1**（真实型号差异 QHRLA/QHRLB，正确） |
| B. 用户场景复现 | **did=112 QHR45 照片** vs did=132 blk4 CAD | **R=1，`blockExclusive=['QHRLA']`** ✅ 精确复现用户报告 |
| C. 正确图纸 | did=112 QHR45 照片 vs did=112 blk0 CAD(QHR45) | **R=0, G=4 干净匹配** ✅ |

> 说明：上一轮会话"照片=QHR45 由 QR 确认"所用 QR 实际来自 did=112 照片（被误归因到 did=132）。本轮回正：did=132 自身照片 QR 均为 QHRLA。

## 四、处置建议

1. **R=1 维持**：属规则正确行为（不同产品型号不匹配），不改引擎。
2. **比对时使用正确图纸**：QHR45 / PC-P1HJQ 照片应比对 **did=112**，比对 did=132 会产生无意义的跨产品红框。
3. **可选通用加固（非本例必需）**：当照片含 QR 时，用 zxing 解码的型号/序列号作为金标准校正 OCR 文本，硬防 A↔5 / O↔0 类混淆。因本例照片已真为 QHR45，加此加固也不会改变该 R=1，属通用鲁棒性增强，需带 `real_match_regression.py` 护栏再上。

## 五、附带代码质量提示（非阻塞）

`diff_visualizer.py:10` 版本说明字符串含 `\d` 触发 `SyntaxWarning: invalid escape sequence '\d'`，建议改为原始字符串 `r"..."`。不影响功能，可顺手修。
