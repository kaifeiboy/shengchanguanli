# v19.43 修复C + run() 统一新4色规则 — 实施报告

日期：2026-08-19
范围：drawings 模块（diff_visualizer.py 单文件），符合"不得更改平台和其它模块"硬约束

---

## 一、修复C：`_detect_icon_regions` 排除 CAD 工艺说明误识

### 根因（多角度实跑验证）

| 角度 | 证据 |
|---|---|
| 诊断1 | `_detect_icon_regions(123_blk_00)` 无 text_bboxes = [(394,141,22,22)]（1 个假图标）|
| 诊断2 | 块图 OCR：`PC-P1HEQ服务热线：4008601111` bbox=(261,145,292,20) 与 22×22 **重叠** |
| 诊断3 | 带 text_bboxes 重跑仍返回 22×22 —— v19.22 的 `_is_qr_like or not overlap` 豁免：正方形一律当 QR 保留 |
| 诊断4 | 检测路径：22×22 仅 **1 个定位符** finder 聚类（did=117 blk_0 真 QR 46×46 是 **3 个定位符**）|

### 修复

1. `_detect_icon_regions` CAD 分支：记录「严格 QR 结构（≥3 定位符 L 型布局 `_is_strict_qr_cluster`）」聚类到 `_qr_struct_rects`；文本重叠过滤 CAD 分支改为 **仅 strict QR 结构豁免**（原 `_is_qr_like` 全量豁免删除）。
2. `run_on_photo` 块侧自检补传块图 OCR `text_bboxes`（复用 `_blk_ocr_real` 缓存，此前过滤链根本不执行）。
3. `run()` 块侧自检同样补传 `text_bboxes`（移除本地 `_is_qr_like` 豁免过滤）。

### 效果

- EQ 侧面 blk_0 iconMissing **1 → 0**（H5 真实端点验证）
- did=117 blk_0/4/5（46×46/59×59/48×48）、did=123 blk_4（97×97）真 QR **全部保留**（strict 豁免）

---

## 二、run() 单块模式统一新4色规则（发现并修复 3 处真 bug）

| # | Bug | 根因 | 修复 |
|---|-----|------|------|
| ① | iconRegions 恒空 | 图标段 L2756 引用 `_display_panels`，但其初始化在文本段（L2796）→ **NameError 被 except 静默吞掉**（LCD 灰化改造引入的回归，块图视图图标标示全失效）| 图标段前补 `_display_panels = set()` |
| ② | 照片 QR 漏检 | run() 照片图标检测用 deskew 后 JPEG temp（缺 v19.40 修复）| 补 `_photo_icons_src` deskew 前无损 PNG + 异常/成功路径 finally 清理 |
| ③ | 97×97 大 QR 假缺标 | `is_override=(icon_regions_override is not None)` 未传 override 时 False → 97×97(25.5% 短边) 不走 `_block_qr_candidate` 15%~50% 放宽 → 位置匹配尺寸比拒绝（run_on_photo 用 `bool(block_icons_raw)`=True 判定一致）| 对齐 `is_override=bool(block_icons)` |
| ④ | 文本多标画橙框 | yellowRegions 绘制 (217,119,6) 与新规则"不同文本不用标示"冲突 | 移除绘制；JSON 数据层契约保留 |

### 验证（run() 4 用例全过）

| 用例 | 修复前 | 修复后 |
|---|---|---|
| EQ blk_0（误识 22×22）| iconRegions 恒空（NameError）| 全空 ✓（误识消除）|
| EQ blk_4（真 QR 97×97）| missing=[97×97] | missing=[] ✓ |
| 117 blk_0（真 QR 46×46）| missing=[46×46] | missing=[] ✓ |
| 117 blk_4（真 QR 59×59）| missing=[] | missing=[] ✓ |

---

## 三、全量回归（零退化）

| 验证 | 结果 |
|---|---|
| real_match_regression | **R=29 Y=18 G=591 Gray=664** 块数=114（基线 R=29 Y=18 G=586 Gray=669；G/Gray +5/-5 等量转移，在 ±25 容差内——修复C 预期效果：CAD 误识图标被滤后 5 块灰→绿）|
| validate.py | **PASSED**（零负向 + 3 负向用例全过）|
| accuracy_harness | 匹配层 14/14 + 集成层 32/32 = **1.0000** |
| H5 真实端点（EQ 侧面）| blk_0 green，iconMissing **1→0** |
| H5 真实端点（EQ 背面）| blk_4 green，iconMissing=0 |

---

## 四、改动文件清单

仅 `src/Platform/Modules/Drawings/diff_visualizer.py`（drawings 模块专属，`_debugVersion` → v19.43）

---

## 五、待用户拍板（1 项）

**H5 块图视图"产品多标 (N)"文案**：run() 的 `yellowRegions` JSON 数据层保留（C#/H5 契约不变），图上不再画橙框——但 H5 差异摘要仍会显示"产品多标 N 处"（与图上不画框存在数字/图不一致，与之前 15NCWQ 同类问题）。若要彻底一致需改 H5 文案/计数（触冻结 UI 契约），待确认。
