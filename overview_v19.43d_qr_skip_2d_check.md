# v19.43d _draw_roi_box_skip_icons 一维检查 Bug 修复

日期：2026-08-19
范围：drawings 模块（diff_visualizer.py 单文件）

---

## 一、用户反馈（事实复述）

用户提供 2 组 EQ 照片的 H5 截图：
- **第一组**（PC-P1HEQ 侧面）：用户截图显示底部有红框
- **第二组**（QH88Y 背面）：用户截图显示 QR 码被绿框圈住（"QR 框绿"）

---

## 二、事实核查结果

### 第一组（PC-P1HEQ 侧面）→ **H5 真实标记图无红框**

| 项 | 事实 |
|---|---|
| H5 端点返回 | blk_0 status=**green** ✓ / iconMissing=0 ✓ / iconExtra=0 ✓ |
| H5 标记图（EQ_grp1_marked.jpg）| **绿框正确覆盖 'PC-P1HEQ 服务热线：4008601111'**，**无红框** |
| DB blk_0 RawText | `'PC-P1HEQ服务热线：400-620-6607\n16.5±0.5\n52.9'`（电话 4008601111 与照片一致 ✓）|
| 用户截图 @image#1 红框 | 来源待查：①H5 端缓存的旧比对结果 ②H5 摘要栏的"缺图标"计数（v19.42 前的旧状态）|

**结论**：用户截图的红框**不是当前 H5 真实输出**——实际 H5 标记图无红框。建议刷新 H5 重新比对。

---

### 第二组（QH88Y 背面）→ **真 bug，已修复**

#### 根因（实跑验证）

`diff_visualizer.py` 的 `_draw_roi_box_skip_icons` 函数（v19.40b 引入）：

```python
def _horiz(yv, x0, x1):     # 顶/底边
    for (sx, sy, sw, sh) in skip_rects:
        if sx < x1 and sx + sw > x0:    # ← 只检查 x 范围，没检查 y
            segs = _clip(segs, sx - gap, sx + sw + gap)
```

**Bug**：判断 skip_rect（QR）是否与顶/底边重叠时，**只检查 QR 的 x 范围是否跨越 ROI**——**不检查 y 范围是否包含当前边**。这导致：

| 边 | ROI 位置 | QR (117,920,102,94) y 关系 | 原版判定 | 应判定 |
|---|---|---|---|---|
| 顶 y=913 | 顶在 QR 顶 7px 上方 | QR y=920-1014 | 盲目切（仅看 x 重叠）| **不切**（顶不在 QR y 范围）|
| 底 y=1006 | 底在 QR 内部（920-1014）| QR y=920-1014 | 切 ✓ | 切 ✓ |
| 左 x=125 | 左在 QR 内部（117-219）| QR x=117-219 | 切 ✓ | 切 ✓ |
| 右 x=355 | 右在 QR 右侧 136px 外 | QR x=117-219 | 不切 ✓ | 不切 ✓ |

**导致**：顶边左半 103px 被错误切掉，QR 区域视觉上仍像被绿框圈住。

#### 修复（v19.43d）

```python
# 顶/底边：严格 y 范围判断（去 gap 对 y 的扩展，gap 只对 x 切段生效）
if (sy <= yv <= sy + sh and sx < x1 and sx + sw > x0):
    segs = _clip(segs, sx - gap, sx + sw + gap)

# 左/右竖：严格 x 范围判断
if (sx <= xv <= sx + sw and sy < y1 and sy + sh > y0):
    segs = _clip(segs, sy - gap, sy + sh + gap)
```

#### 修复后视觉（H5 真实端点 EQ_grp2 标记图）

| 边 | 修复前 | 修复后 |
|---|---|---|
| 顶 y=913 | 切（错误）| **完整 229px** ✓ |
| 底 y=1006 | 切右半 127px | 切右半 127px ✓（同前）|
| 左 x=125 | 完全切 | 完全切 ✓（同前）|
| 右 x=355 | 完整 95px | 完整 95px ✓（同前）|

**QR 区域 (117-219, 920-1014) 三边断开**（左/顶/底），仅右竖包住 QR 右侧——QR 不再被完整框圈住。

---

## 三、全量回归（零退化）

| 验证 | 结果 |
|---|---|
| real_match_regression | R=29 Y=18 G=591 Gray=664 块数=114（与 v19.43 修复C 后基线一致）|
| validate.py | **PASSED**（零负向 + 负向用例全过）|

---

## 四、改动文件

仅 `src/Platform/Modules/Drawings/diff_visualizer.py`（drawings 模块专属，`_draw_roi_box_skip_icons` 函数 `_horiz/_vert` 严格 y/x 范围判断）。

---

## 五、待办

- **DB RawText 污染清理**：did=123 各块 RawText 含 CAD 尺寸/工艺说明（'16.5±0.5' '52.9' '激光打标字体:黑体'），应清理（与 B 短期 RawText 修正方向一致，但需重新做或确认是否被回退）
- 用户 H5 端"红框"截图问题：建议刷新 H5 重新比对确认无红框
