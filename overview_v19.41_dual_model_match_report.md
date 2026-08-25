# 15NCWQ 双型号匹配修复实施报告

> 日期：2026-08-18 | 状态：✅ A + B 短期 + B 中期 已实施并全量回归通过
> 方案：用户拍板 A（型号感知匹配）→ B 短期（RawText 数据修正）→ B 中期（_auto_roi 小字修复）

---

## 1. 实施清单

### 1.1 B 短期：RawText 数据修正（生产 DB 写，已备份）

| 块 | 修正前 | 修正后（真值）|
|---|---|---|
| blk_0 | A作业\n80. 5\n3±0.5\n0净NFC便捷控制\nYCWATSNCWQ\n服务热线：400-620-6607 | YCWA15NCWQ\n服务热线：400-620-6607\nNFC便捷控制 |
| blk_1 | 服务热线：400-620-8807\nWATBNCWO\n()NFC便捷控制\n激光刻印(灰色),位置居中 | 服务热线：400-620-6607\nYCWA16NCWQ\nNFC便捷控制 |
| blk_4 | 30 ± 0. 5\n20\nQHRLA\n200512\nBFFC001 | QHRLA\n200512\nBFF0001 |
| blk_5 | 30 ± 0. 5\n20\nQHRLB\n200512\nBFFO001 | QHRLB\n200512\nBFF0001 |

- 备份：`data/_backup/app_20260818_2010.db`
- 改动：误读字符修正（6607/YCWA15NCWQ/YCWA16NCWQ/BFF0001）+ 舍弃 CAD 标注（A作业/B作业/30±0.5/20/激光刻印等）

### 1.2 A：型号感知匹配（photo_registration.py）

- 正则 `(?:YCWA\d+NCWQ|QHRLA|QHRLB)` 提取型号
- `extract_model(text)` 单行型号提取
- `block_model(block)` 块 RawText 推导所属型号
- `assign_photo_lines_to_blocks` 加块级型号过滤：照片主导型号 vs 块型号不一致 → 整块 skip

### 1.3 B 中期：_auto_roi 小字修复（ocr_cli.py）

- 簇内全部检测行高 < 15px（CAD 12px 打标区）→ `_auto_roi` 返回 None → main 回退全图 `_run_rapidocr`（原始检测）
- 根因：二次 OCR（裁剪+LANCZOS 2x 放大）会劣化 6607→8807、NSWATGNCWO_→WATBNCWO（已实跑 5 变体预处理均失败）
- 全图原始检测对 6607 正确（conf=0.93）

---

## 2. 验证结果（多角度）

### 2.1 端到端（H5 真实生产端点）

| 照片 | 修复前 | 修复后 |
|---|---|---|
| 15NCWQ 正面 | blk_1 red（假红）| blk_0 green / blk_1 low_conf（型号不同 skip）|
| 15NCWQ 背面 | blk_5 red（假红）| blk_4 green / blk_5 low_conf（QHRLB skip）|
| iconMissing | 0 | 0 |

### 2.2 全量回归（零退化）

| 验证 | 结果 |
|---|---|
| accuracy_harness（匹配层 14/14 + 集成层 32/32）| ✅ 1.0000 |
| real_match_regression（114 块 + did=115 真实照片）| ✅ R=29 Y=18 G=586 Gray=669（与基线一致）|
| validate.py（含 3 个负向用例）| ✅ PASSED |

---

## 3. 用户后续待办

1. **重启 Platform** 让 ocr_cli.py B 中期改动在 OcrService subprocess 链路生效（工作记忆"改 ocr_cli.py 需重启 Platform"）
2. **9670002 编码核对**：产品背面铭牌第三行是 9670002，图纸 blk_4 印的是 BFF0001（已修正 RawText）→ 实物与图纸编码实际不一致，需用户核对实物标签
3. **型号字形识别**：YCWA15NCWQ → YCWATSNCWQ（ts→15 误读）仍未解决，OCR 模型能力极限；可考虑：采集样本微调 / 超分预处理（长期）
4. **git 提交 working tree**（照片标示 + A + B 中期 + 4 块 RawText 修正）
