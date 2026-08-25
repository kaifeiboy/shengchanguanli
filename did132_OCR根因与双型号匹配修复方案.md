# did=132 OCR 根因验证报告 + 双型号匹配修复方案

> 日期：2026-08-18 | 状态：方案待确认（未改代码）
> 依据：多角度实跑验证（复现链 / 变体实验 / blk_0 对照 / 人眼真值）

---

## 1. OCR 误读根因（确切可证实）

### 1.1 事实链（全部实跑）

| 角度 | 内容 | 结果 |
|---|---|---|
| 复现 | `_auto_roi` 对 blk_1 完整流程：选中簇=[NFC, NSWATGNCWO_, 6607, 激光刻印] → 裁剪 (2,240)-(578,330) 576×90 → 2x LANCZOS 放大 → 二次 OCR | `服务热线：400-620-8807 / WATBNCWO / ()NFC便捷控制 / 激光刻印(灰色),位置居中` |
| 对照 | 同一 blk_1 全图原始检测（`_rapidocr_raw`）| `NSWATGNCWO_ / 服务热线：400-620-6607`（**6607 正确** conf=0.93）|
| 真值 | 用户人眼 | `YCWA16NCWQ / 400-620-6607` |
| blk_0 对照 | 同为 12px 标签条，入库输出 | `YCWATSNCWQ / 6607`（6607 稳定正确）|

### 1.2 变体实验（排除预处理因素）

对 blk_1 裁剪区域做 5 种预处理，二次 OCR 全部无法读出 6607/YCWA16NCWQ：

| 变体 | OCR 输出 |
|---|---|
| 1x 原始裁剪 | 8807 / WATBNCWO |
| 2x LANCZOS | 8807 / WATBNCWO |
| 4x LANCZOS | 8807 / WATBNCWO |
| 2x + 锐化 | 400-B20-8807 / NWATBNCWO（更乱）|
| 4x + 对比度 1.8 | 400-B20-8807（更乱）|
| 型号行 8x 单独放大 | 空（无检测）|

### 1.3 根因结论（复合，多角度交叉）

```
根因 = CAD 12px 小字打标区 RapidOCR 识别极限 + blk_1 标签条笔画质量差
   ├─ ① 12px 小字（PP-OCRv4 建议 ≥15px）：6↔8、Y↔N、C↔S、1↔T 字形信息量不足
   ├─ ② 同一行两读不同（原始 6607 vs 二次 8807）→ 模型尺度敏感 + 非确定性
   ├─ ③ 放大预处理（2x/4x/锐化/对比度）均不改善 → 非预处理问题，是模型/渲染质量极限
   ├─ ④ blk_0 幸运（6607 笔画清晰）、blk_1 不幸（6/8 难辨）→ 块图渲染质量逐块不同
   └─ ⑤ _auto_roi 二次 OCR 未解决小字问题，反而固化错误进 DB RawText
```

---

## 2. 修复方案

### 方案 A：型号感知匹配（用户需求 1，双图纸 15/16NCWQ）

**目标**：照片 OCR 是 YCWA15NCWQ → 只与 15NCWQ 块匹配；16NCWQ → 16 块匹配。

**设计**：
1. **型号推导**（零 schema 变更）：`drawing_blocks.RawText` 正则提取型号（`YCWA\d+NCWQ` / `QHR[LA|LB]` 等）→ 每块标注所属型号（运行时推导，不存 DB）
2. **匹配过滤**：`localize_blocks` 归属时——照片 OCR 检测到型号行 → 该行只归属到"型号相同的块"；块侧比对时，**型号不同的块不参与缺标/多标判定**（跳过）
3. **无型号照片退化**：照片未读到型号 → 保持现状全块比对（不丢能力）
4. **型号相似度**：`YCWA15NCWQ` vs `YCWATSNCWQ`（blk_0 误读）——按现有 `_texts_match_strict` 处理（ts→15 已知 bug 另行拍板）

**改动面**：photo_registration.py（归属过滤）+ diff_visualizer.py（型号过滤层）——drawings 模块内，零平台影响。

### 方案 B：OCR 入库质量修复（需求 2）

**短期（数据修正，需用户授权写生产 DB）**：
1. blk_1 RawText 人工修正为 `YCWA16NCWQ\n服务热线：400-620-6607\nNFC便捷控制`（真值）
2. blk_5 同理核对（QHRLB 背面）
3. **舍弃 CAD 工艺说明**："激光刻印(灰色),位置居中" 类 → 从 RawText 剔除（用户拍板规则）

**中期（入库链路）**：
1. `ocr_cli._auto_roi`：小字块（检测行高 <15px）**跳过二次 OCR**，直接用原始检测结果（已验证原始检测 6607 正确）
2. 或放大算法 2x LANCZOS → **NEAREST**（保字形，LANCZOS 平滑小字笔画致混淆）——需实验验证
3. 入库与 diff 统一引擎路径（避免两读不同）

**长期（模型/数据）**：
1. 采集 CAD 小字打标样本 → 微调 RapidOCR rec 模型
2. 或引入超分辨率预处理（Real-ESRGAN 类）前处理小字区

### 方案 C：图纸库模型管理（配合 A）

- 图纸 PDF 含多型号（15/16NCWQ）时，切割入库标注"型号归属"，H5 端按型号展示/过滤
- 依赖方案 A 的型号推导实现，无独立改动

---

## 3. 影响面与回归

| 改动 | 文件 | 影响 |
|---|---|---|
| A 型号过滤 | photo_registration.py + diff_visualizer.py | drawings 模块内；validate.py 需回归（匹配行为变化）|
| B 短期数据修正 | data/app.db drawing_blocks | ⚠️ 生产 DB 写操作，须用户授权 + 备份 |
| B 中期 _auto_roi | ocr_cli.py | 被 OcrService/batch_ocr/ocr_worker/cut_region 引用（drawings 内）→ 改后全量回归 |
| 回归 | real_match_regression + accuracy_harness + validate.py | R=29 Y=18 G=586 Gray=669 基线 |

---

## 4. 待用户拍板

1. 方案 A 的型号推导：从 RawText 正则提取（零 schema）还是 DB 加 Model 列（schema 变更触冻结）？
2. 方案 B 短期：是否授权修正 blk_1/blk_5 RawText（生产 DB 写）+ 舍弃"激光刻印"说明？
3. 方案 B 中期：_auto_roi 小字跳过二次 OCR vs NEAREST 放大——先做实验验证哪个有效？
4. 实施顺序：A（匹配正确性）→ B 短期（数据）→ B 中期（入库）？

---

*验证工具链：_auto_roi 完整复现（聚类/裁剪/放大/二次 OCR）+ 5 变体预处理实验 + blk_0/blk_1 对照 + _rapidocr_raw 原始检测 + ocr_cli subprocess 入库同款路径*
