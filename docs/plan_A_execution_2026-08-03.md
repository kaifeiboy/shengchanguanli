# 方案 A 执行计划（待逐项确认，2026-08-03 18:46）

**用户选择**：方案 A（规则收敛 + 全量重切） + did=123 PC-P1HEQ
**目标**：把 selfblock 假阳率从 40%（40/99 块）压到 ≤ 5%（≤ 5 块），不再"修 A 坏 B"
**总投入**：3-5 天（含全量回归 + 4-5 轮迭代）
**代码改动**：仅 `diff_visualizer.py` 一个文件，**不改 C#**

---

## ⏸ 步骤 0：执行前的"冻结快照"（必须先做）

**目的**：万一任何步骤引入了回归，可以 1 分钟回退到 v19.37。

具体动作：
```
cp src/Platform/Modules/Drawings/diff_visualizer.py \
   src/Platform/Modules/Drawings/diff_visualizer.py.bak_v1937
```
并跑 selfblock baseline 记录到 `docs/fleet_baseline_v1937.md`（已有扫描数据可复用）。

⏳ **需用户确认**：是否同意步骤 0？

---

## 步骤 1：合并 3 个互殴的过滤函数（核心改动 1）

**问题**（引用 v19.33→v19.35 教训）：
- `_is_cad_annotation()` 调用 3 个独立子判断（`_is_annotation_note` / `_is_lcd_display_text` / `_is_cjk_garbage`）
- 每个子判断各自维护白名单/黑名单
- 改一个 → 必须连锁改 3 个（v19.35 删「禁止」就是因为 `_is_annotation_note` 和 `_is_cjk_garbage` 白名单互相打架）

**改动**：
1. 保留 `_is_serial_number` / `_is_qr_ocr_noise` / `_is_dimension` 三个**有明确边界**的规则（不参与合并）
2. 合并 `_is_annotation_note` + `_is_lcd_display_text` + `_is_cjk_garbage` → **单函数** `_should_skip_text(text, ctx)`：
   - 输入 `ctx = {"has_anchor": bool, "block_brand": str, "lang": "CN"|"ASCII"|"mixed"}`
   - 当 `has_anchor=True`（参考侧找到同源词）→ 永不灰化
   - 当 `has_anchor=False` → 走子规则（保留必要白名单，但放在 _should_skip_text 内部）
3. step3 和 step4 都用这个**统一函数**，消除"step3 灰但 step4 绿"的不对称

⏳ **需用户确认**：是否同意步骤 1？

---

## 步骤 2：全量 31 图重切（核心改动 2）

**问题**（核心根因 P3）：
- DB `drawing_blocks.Icons` 有 12 个块存了由 `_detect_icon_regions` 自检产生的图标
- 其中至少 2 个（did=115 blk_3 = `[226,266,16,16]`；did=132 blk_0 = selfblock 路径）已被证实是**伪图标**
- 重切时**强制清空所有 Icons** → 杜绝历史污染

**改动**：
1. 写一个 `temp/reset_icons_20260803.py` 一次性脚本：
   ```sql
   UPDATE drawing_blocks SET Icons=NULL;  -- 只清 Icons，不动 RawText
   -- 备份：
   CREATE TABLE drawing_blocks_icons_bak_v1937 AS SELECT DrawingId, BIdx, Icons FROM drawing_blocks;
   ```
2. 重切：`POST /api/drawings/segment?force=true`（已有 API，按批跑）
3. 重切过程中：
   - 块 RawText 重新 OCR 写入 DB
   - **Icons 字段保持 NULL**（C# 端不写，由 live `_detect_icon_regions` 自检兜底，v19.37 已改成 truthy 处理空 list）

⏳ **需用户确认**：
- 是否同意步骤 2.a（SQL 一次性清空 + 备份表）？
- 是否同意步骤 2.b（重切 31 图，预计 10-30 分钟）？

---

## 步骤 3：图标检测器收敛（核心改动 3）

**问题**（决定性证据）：selfblock 时 30 块报 `iconMissing`，**说明 `_detect_icon_regions` 即使在 photo=block 同一张图时都会误检出"图标"**。

**改动**：
1. 7 个子检测器收敛成 3 个：
   - **A 级（保留）**：zxing 解码（确认 QR）/ pyzbar 解码（确认 QR）— **必须解码成功才计图标**
   - **B 级（保留）**：`_qr_finders` + `_is_strict_qr_cluster`（结构定位符检测）— 已验证有效（v19.27 修好）
   - **C 级（降权）**：`_dense_rects` / `_is_loose_qr_cluster` / `_icon_edge_density` 改为"**仅作候选标记**，必须 A 或 B 命中才算图标"
2. C 级候选标记存到新字段 `iconCandidates`，不入 `iconRegions`。H5 显示紫框（候选）但不画红黄框。
3. v19.32 引入的 `CLAHE` 增强保留（低对比度 QR 检测用）

⏳ **需用户确认**：是否同意步骤 3？需要 H5 端配合增字段吗？目前架构仅改 Python 是 PR-safe 的，C# 不动 → iconCandidates 字段不影响 H5，但用户看不到候选标记。

---

## 步骤 4：H5 yellowRegions OOB 静默失败补强（小修）

**问题**：当前 `yellowRegions` 含大坐标时画到块图溢出，静默失败（用户看不到黄框但 JSON 污染）。

**改动**：在 `run()` 末尾 `for r in result["yellowRegions"]: ... for r in result["redRegions"]: ...` 循环里加：
```python
bx, by, bw, bh = block_pil.size
clipped = []
for r in result["yellowRegions"]:
    x, y, w, h = r
    if x < 0 or y < 0 or x+w > bw or y+h > bh:
        # 警告但保留（便于追踪）
        print(f"[v19.38] yellow OOB: blk={bw}x{bh} {r}", file=sys.stderr)
        # 截断到块图范围，避免 OOB：
        x = max(0, x); y = max(0, y); w = min(w, bw-x); h = min(h, bh-y)
    clipped.append((x,y,w,h))
result["yellowRegions"] = clipped
```
同样的修对 redRegions。

⏳ **需用户确认**：是否同意步骤 4？

---

## 步骤 5：护栏（全量回归强化）

**已有** `real_match_regression.py`（baseline=48 块差异），加两条：
1. **新 selfblock 套件**（参考 fleet_scan.py）：99 块跑 selfblock，**要求假阳率 ≤ 5%**（≤ 5 块）
2. **realphoto 套件**：did=115 真实照片 + did=132 真实照片各 5 块，**要求 R+Y ≤ v19.37 baseline**

每改一版就 batch 跑两个套件，结果写到 `docs/regression_log.md`。

⏳ **需用户确认**：是否同意步骤 5？

---

## 步骤 6：分阶段执行

| 阶段 | 改动内容 | 验证 | 预计耗时 |
|---|---|---|---|
| 6.1 | 步骤 1（合并过滤函数） | fleet selfblock 假阳率应从 40 → ~30% | 半天 |
| 6.2 | 步骤 3（图标检测收敛） | 假阳率应从 ~30 → ~10% | 1 天 |
| 6.3 | 步骤 4（OOB 截断） | OOB 数 = 0，R/Y 数字不变 | 2 小时 |
| 6.4 | 步骤 2（重切 31 图） | fleet 假阳率应进一步下降 | 1-2 小时（人工触发 + 等） |
| 6.5 | 步骤 5（护栏全跑） | realphoto 套件回归 | 1 小时 |

每阶段不通过 → 回滚 + 重新分析。

---

## 7. 不做的事（明示避免范围蔓延）

- ❌ 不动 C# / H5 / start-platform.ps1 / 端口 / API 契约（冻结规则 §3）
- ❌ 不改 `clean_descriptive_qr.py` / `cut_region.py` / `batch_ocr.py` / `compare_before_after.py`
- ❌ 不引入新依赖（rapidocr / zxing / PIL 之外）
- ❌ 不重写 OCR 引擎（保持 RapidOCR）
- ❌ 不改 `_norm_text`（业务级归一化已是沉淀规则，复杂度高）

---

## 8. 完成定义（DoD）

✅ selfblock 99 块假阳率 ≤ 5%
✅ realphoto 套件 R+Y ≤ 48（与 v19.37 baseline 持平或更好）
✅ 全量 31 图重切完成且 DB Icons 全清无污染
✅ 回归记录 `docs/regression_log.md` ≥ 3 轮干净
✅ `diff_visualizer.py` `_debugVersion="v19.38"`

⏳ **请用户对每个步骤拍板/否决**。任何步骤被否决 → 回退到保守路线（单点补丁）。
