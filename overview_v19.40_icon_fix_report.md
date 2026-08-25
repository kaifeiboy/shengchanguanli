# v19.40 实施报告 — 15NCWQ"5 处缺图标"根因修复

> 日期：2026-08-18 | 状态：✅ 已实施并全量验证通过
> 依据：用户批准的《块侧内容识别对齐方案》模块 A（治本）+ 副 bug + 模块 C
> 改动文件：`src/Platform/Modules/Drawings/diff_visualizer.py`（纯 .py，即时生效，无需重启）

---

## 1. 修复内容（v19.40，三处）

### 修复① 照片图标检测源改为 deskew 前无损 PNG（主因治本）
**问题**：run_on_photo 对 >2000px 原图 resize 后经 `_deskew_image` 扶正（实拍 -2.7°）+ JPEG92 重压写 temp，照片侧 `_detect_icon_regions` 完全漏检 QR（photo_icons=[] → v19.22 存在性匹配不触发 → 块侧 QR 候选全缺标）。
**实验推翻原假设**：resize+JPEGq92 也能检出 1 个 QR → **主因是 deskew 旋转重采样**（非 resize/JPEG）。
**修复**：L2928 前将 resize 后的图保存为无损 PNG（`_photo_icons_src`），图标检测改用它；v19.22 存在性匹配不看位置，坐标空间差异零影响。

### 修复② bsize 无条件更新（副 bug，修复①后暴露）
**问题**：L3055 原条件 `if roi is not None and not block_icons_raw` 把 bsize 更新与自检捆绑 → DB Icons 非空或 low_conf 块时 bsize 保持 (1,1) → `_block_qr_candidate`/`_size_ratio_consistent` 归一化错乱 → QR 候选判定失败 → 无法走存在性豁免。
**修复**：bsize 无条件从块图读取真实尺寸；自检仍仅 roi 非空且 Icons 空时执行。

### 修复③ low_conf 块不参与图标差异判定（代码与注释不符的真 bug）
**问题**：v19.31 注释声明"未定位块(low_conf)不参与差异判定"，但代码仍对 low_conf 块调 `_compare_icons` → 照片没拍到该区域却报"缺图标"（且 missing 计入 JSON 但图上不画 → H5 数字与视觉不一致的根源之一）。
**修复**：`_compare_icons` 仅 roi 非空时调用；low_conf 块 missing/extra 恒 0。

### 模块 C：`_sanitize_block_icons` 过滤链
DB Icons/override 数据过过滤链（尺寸 ≥14px 且 ≤ 短边30%、宽高比 0.25~4.0），防绕过检测链的异常图标参与判定。单测 4/4（正常保留 / 超限滤 / 长条滤 / 过小滤）。

---

## 2. 验证结果（多角度交叉）

| 验证项 | 工具 | 结果 |
|--------|------|------|
| 15NCWQ 专测（Python 直测）| run_on_photo | iconMissing **5 → 0**（blk_0 1→0, blk_1 3→0, blk_4 1→0）|
| 15NCWQ 专测（生产 H5 端点）| curl POST /diff-photo | iconMissing **0**（生产服务已加载 v19.40）|
| 全量回归 | real_match_regression.py | **R=29 Y=18 G=586 Gray=669**（与基线完全一致，零退化）|
| 双评 | accuracy_harness.py | 匹配层 14/14 = 1.0；集成层 32/32 = 1.0 |
| 基线锁 | validate.py | **PASSED**（含 did123 三角 / _drawing_yellow_filter / legacy run() 负向用例）|
| 语法 | py_compile | OK |

---

## 3. 事实修正记录

- `_is_icon_aspect_ratio_ok` 实际阈值 **0.25~4.0**（`ar>=0.25` 短边/长边），非方案初稿所写 0.4~2.5（误从密矩形参数推断）——已修正方案文档。
- 主因判断链：最初"LANCZOS resize 漏检"（对，行为）→ 实验定位"deskew 旋转重采样"（精确机制）→ resize/JPEG 非主因（实验证明 JPEGq92 也检出）。

---

## 4. 剩余待办

| 项 | 状态 | 说明 |
|----|------|------|
| 模块 B（RawText 入库清洗）| ⏸ 待用户确认 | 需改 C# EnsureSegmentedCore L1378/refilter L1497 + **重启生产 Platform** + refilter + 全量回归（可能重基线）；blk_3 `15/12` 是否 CAD 标注待目视确认 |
| git 提交 v19.40 | 待办 | working tree 基线提交（照片标示分支）|
| `_norm_text` ts→15 过度归一 | 已知待拍板 | YCWATSNCWQ↔YCWA15NCWQ 误绿（待移除清单）|

---

## 5. 结论

15NCWQ"5 处缺图标"根因（照片侧 deskew 后漏检 → 块侧 QR 候选全缺）已修复，**iconMissing 5→0**，全量回归零退化，生产端点即时生效。用户新规则"QR 按图标处理"经模块 A+C 落实（QR 存在性匹配 + 图标过滤链对齐）。
