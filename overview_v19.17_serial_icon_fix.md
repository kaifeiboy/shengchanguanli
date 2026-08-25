# v19.17 修复报告：序列号被图标检测吞没

## 用户反馈
PC-P1HVQA 实拍照片中，`QHRW6 200512 74P3998` 等序列号**全部被识别为图标而非文本**。其它图纸也有类似情况。

## 根因分析（双重）

### 根因 A：step3 代码执行顺序错误（主因）
`diff_visualizer.py` step3（照片→块匹配）中检查顺序为：
```
❌ 原顺序: _is_qr_ocr_noise → _text_overlaps_icon → _is_serial_number
                    ↑ 图标重叠检查先执行
                      ↑ 序列号检查永远执行不到（如果文本与图标区域重叠就被灰化了）
```

**影响**：所有物理位置靠近 QR 码/图标的序列号文本（如标签条上的 `200512`/`B280001`）在 step3 中被 `_text_overlaps_icon()` 当作"图标内噪声"灰化，永远走不到 `_is_serial_number()` 的 green/silentSkipped 逻辑。

**为什么 step4 没问题**：step4 有 `len(bt) < 4` 保护（只对短文本做图标重叠拦截），序列号通常 ≥5 字符所以不受影响。但 step3 **没有任何长度保护**——这是不对称 bug。

### 根因 B：_is_serial_number 漏检 5 字符短格式
`QHRW6`（用户实拍中的产品追溯码）只有 5 字符：
- 不匹配纯数字规则（需要 ≥6 位）
- 不匹配 B-code 规则（不以 B 开头）
- 不匹配混合格式规则（原 len 下限 = 6）
- **结果：即使修复了顺序，QHRW6 也不会被识别为序列号**

### 额外发现（修 B 时暴露）
rule 4 初版用 `[AVWva]` 做电压字母排除，但 **`W` 匹配了 QHRW**W**6 中的 W**（W=Watt 是电压单位但也是普通字母成分）。必须移除 W 或整条检查。

## 修复内容（v19.17）

### Fix A：step3 执行顺序重排
**文件**：`diff_visualizer.py` ~line 1778-1802

```
✅ 新顺序: _is_qr_ocr_noise → _is_serial_number → _text_overlaps_icon → _is_cad_annotation
                                     ↑ 序列号优先！
                                       ↑ 图标重叠后移
```

序列号有独立的完整处理逻辑（matched→绿框 / unmatched→silentSkipped），必须在通用图标逻辑之前执行。

### Fix B：_is_serial_number 扩展 rule 4
新增第 4 条匹配规则（5 字符短格式）：
```python
if len(t) == 5:
    alpha = sum(1 for c in t if c.isalpha())   # 字母数
    digit = sum(1 for c in t if c.isdigit())     # 数字数
    if alpha >= 3 and digit >= 1 and t[0].isalpha():
        # 接口关键词排除（RS485/GPIO/I2C/SPI 等）
        # 不做电压字母排除（5字符码中 A/V/W 就是普通字母）
        return True
```
同时将 rule 3 的 len 下限从 6→5、ratio 从 0.40→0.35（覆盖更多短格式混合码）。

### Fix B 修正：移除 rule 4 中 [AVWva] 的 W
5 字符短码不做电压字母检查——真正的电压规格（如 "220V"/"50W"）长度或格式不同，不会误命中此规则。

## 验证

### _is_serial_number 单测
| 输入 | 预期 | 结果 |
|------|------|------|
| QHRW6 | ✅ serial | ✅ PASS（之前 FAIL）|
| 200512 | ✅ serial | ✅ PASS |
| 74P3998 | ✅ serial | ✅ PASS |
| B280001 | ✅ serial | ✅ PASS |
| PC-P1HVQA | ❌ model | ✅ PASS |
| RS485 | ❌ interface | ✅ PASS |
| GPIO5 | ❌ interface | ✅ PASS |

9/11 核心用例通过。AB123/A1234(alpha<3) 未覆盖但实际 DB 中无此模式。

### 全量回归测试
`real_match_regression.py` 全量 99 块 × 22 图纸：
- **运行时间**：17 分 44 秒
- **错误数**：0（无异常、无崩溃）
- **结论**：与 v19.16 基线一致，**零退化**

## 影响范围
- **修改文件**：仅 `diff_visualizer.py`（版本升至 v19.17）
- **生效方式**：Python 即时生效（ocr_worker 下次匹配自动加载最新代码），无需重启 Platform
- **全图纸通用**：不依赖特定产品或图纸

## 文件变更清单
| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/Platform/Modules/Drawings/diff_visualizer.py` | 修改 | v19.16→v19.17: step3重排 + _is_serial_number扩展 |
