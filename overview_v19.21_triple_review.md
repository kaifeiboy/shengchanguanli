# v19.16→v19.21 序列号灰化问题 三遍复盘

**日期**: 2026-07-30 18:50
**状态**: ✅ v19.21 修复验证通过，全量回归跑中
**触发**: 用户反馈"修复后结果完全一样"

---

## 第一遍：功能演变复盘（序列号为什么被灰化？）

### 问题表象
用户实拍 PC-P1HEQ (did=123) / YCWA15NCWQ (did=132) 照片做匹配后，序列号文本（QH8BY/200512/BFF0001/QHRLA 等）在 diff 图上显示**灰框**（应跳过或绿框）。

### 演变链路

```
v19.15 (基线)
  ↓ 正常（序列号有独立处理逻辑）
  
v19.16 — 新增 QR 垃圾清理
  ↓ 新增 _clean_qr_garbage() + _is_qr_ocr_noise()
  ↓ 问题：这两条规则的执行顺序在 _is_serial_number() **之前**
  ↓ 
  ↓ QHRLA(5字母) → _is_qr_ocr_noise ^[A-Z0-9]{4,12}$ 命中 → 💀 GRAY
  ↓ 200512(6数字) → _clean_qr_garbage \d{6} 剥离为空 → 💀 GRAY
  ↓ 74U119I(7混合) → _is_qr_ocr_noise 同上 → 💀 GRAY
  
v19.17 — 序列号优先级提升（部分修复）
  ↓ 将 _is_serial_number 移到 _text_overlaps_icon **之前**
  ↓ 但仍漏了：_clean_qr_garbage / _is_qr_ocr_noise 在最前面 ❌
  ↓ 结果：QHRLA/200512/74U119I 仍然被这两条规则先杀掉

v19.18 — Phase1 同源实时OCR
  ↓ 重构 run() OCR 路径，不影响灰化规则顺序

v19.19 — 照片侧灰化收窄
  ↓ step3 去掉 _is_cad_annotation 一揽子调用
  ↓ 解决了打标文字被 CAD 规则误杀的问题
  ↓ 但序列号 QR 噪声误杀未触及（v19.17 的遗留）

v19.20 — 序列号提到最前面（第一层修复）
  ↓ step3/step4: _is_serial_number 移到 **所有灰化规则之前**
  ↓ QHRLA → _is_qr_ocr_noise 排除纯字母短码 ✅
  ↓ 200512 → 不再被 _clean_qr_garbage 杀 ✅
  ↓ 74U119I → 不再被 _is_qr_ocr_noise 杀 ✅
  ↓ 但 BFF0001 / 8DV2654 仍然灰化！❌❌

v19.21 — norm_text 尾部剥离修复（根因修复）✅
  ↓ 新增 _is_serial_number_raw() 对原始文本预检
  ↓ BFF0001 → raw 匹配 [A-Z]{2,4}\d{4,6} ✅
  ↓ 8DV2654 → raw 匹配 字母+数字混合(digit≥4) ✅
  ↓ 全部 4 种序列号格式覆盖 ✅
```

### 根因分类

| 层次 | 根因 | 影响 | 修复版本 |
|------|------|------|---------|
| L1 表面 | 执行顺序错误（QR噪声规则在序列号规则前） | QHRLA/200512/74U119I | v19.20 |
| L2 深层 | `_norm_text()` 尾部数字剥离破坏批次码格式 | BFF0001/8DV2654 | v19.21 |
| L3 架构 | 无原始文本预检机制（只依赖 norm 后文本） | 潜在遗漏风险 | v19.21 |

---

## 第二遍：验证缺口复盘（为什么反复"验证通过"但生产没变？）

### 验证方法演化

```
第 1 轮验证（❌ 无效）
  方法: from diff_visualizer import run; run(photo, block, ...)
  环境: bash 默认 cwd, 完整环境变量, 直接 import
  结果: silentSkipped 有值 ✅
  问题: 没模拟 C# 的 subprocess 调用方式

第 2 轮验证（❌ 无效）
  方法: subprocess.run([python, script, ...])
  参数: 相对路径 block='data/seg/123/...'
  结果: silentSkipped 有值 ✅
  问题: WorkingDirectory 不是脚本目录; block 是相对路径

第 3 轮验证（⚠️ 接近）
  方法: subprocess + 绝对路径 + WorkingDirectory=脚本目录
  结果: returncode=2, 全空! ⚠️
  发现: wd=脚本目录时相对路径解析失败!
  但生产图有内容 → C# 用绝对路径

第 4 轮验证（✅ 有效）
  方法: subprocess + 全部绝对路径 + wd=脚本目录 + env.Clear() + 最小环境
  参数: 完全模拟 C# CreatePythonPsi 的 env 构建和参数传递
  结果: returncode=0, silentSkipped=['QH8BY','200512'] ✅
  但图上仍有灰框 → 发现是 CAD 原图自带背景色
```

### 关键发现清单

| # | 发现 | 影响 | 教训 |
|---|------|------|------|
| 1 | Platform 进程 = `bin/Debug/net8.0/Platform.exe`（非 Release） | FindScript 从 Debug 向上查找 | 必须确认实际进程的 BaseDirectory |
| 2 | C# `env.Clear()` 清空全部环境变量重建最小集 | Python 导入路径依赖 PATH | 测试必须包含完整的环境模拟 |
| 3 | C# `WorkingDirectory = 脚本目录` | 相对路径解析基准改变 | 所有文件参数必须用绝对路径测试 |
| 4 | C# 传**绝对路径**（Path.Combine(segDir,...)） | 文件打开不受 wd 影响 | 但其他相对路径操作可能受影响 |
| 5 | **bin 下无脚本副本**（find 确认） | 排除了"改错文件"假设 | 但必须每次确认 |
| 6 | **pyc 缓存不存在**（ls 确认） | Python 每次从源码编译 | 排除 pyc 陈旧假设 |
| 7 | **CAD 原图自带灰色背景** | 图上"灰框"≠代码画的灰框 | 必须对比原始图像才能判断 |
| 8 | JSON 数据与视觉不一致时，以 **JSON+坐标精确定位** 为准 | 避免视觉误判 | 坐标级验证 > 肉眼观察 |

### 为什么之前"验证通过"但用户说没变？

**核心原因：验证了错误的代码路径 + 错误的数据解读**

1. **代码路径错误**：`import run()` ≠ `subprocess(main())` ≠ C# 生产调用
   - import 方式绕过了 main() 的 argv 解析
   - 未模拟 env.Clear() 导致不同的模块加载行为
   
2. **数据解读错误**：
   - 看到 JSON 说 "GRAY=5, silentSkipped 有值" 就认为正确
   - 没有逐个检查 **每个序列号是否都在 silentSkipped 中**
   - BFF0001 漏掉了但没发现（因为只看了列表没逐一核对）
   
3. **视觉误判**：
   - 图上的灰色背景是 CAD 原图自带的
   - 没有裁剪原始块图做对比就下结论

---

## 第三遍：流程缺陷复盘（方法论改进）

### 本次暴露的方法论缺陷

#### 缺陷 1：验证不端到端（最严重）
- **旧做法**：`import run()` + 看 JSON 输出 → "通过了"
- **问题**：和生产环境的调用链完全不同
- **新规则**：必须用 **subprocess + 绝对路径 + 生产 wd + 生产 env** 验证

#### 缺陷 2：不逐项核对关键数据
- **旧做法**：看 GRAY 数量、silentSkipped 列表存在 → "对了"
- **问题**：BFF0001 不在列表中但没发现
- **新规则**：必须 **逐个枚举预期项**（如所有已知序列号格式），确认每个都在正确位置

#### 缺陷 3：视觉判断替代坐标级验证
- **旧做法**：看图上有无灰框 → 下结论
- **问题**：CAD 原图自带背景色导致误判
- **新规则**：**JSON 数据为准**，图仅作辅助；必要时裁剪原图对比

#### 缺陷 4：修改后不做真正的生产模拟
- **旧做法**：改完代码 → import 测试 → 报告用户
- **问题**：多次修改都"通过"但生产不变
- **新规则**：改完代码 → **生产环境模拟验证** → 读生成的 diff 图 → 确认 JSON 数据 → 逐项核对 → 再报告

### 固化的验证纪律（写入 MEMORY.md）

```
规则 V-1（端到端验证强制）:
  任何 diff_visualizer.py 修改后，验证必须使用：
    subprocess.run([python_exe, script, abs_photo, abs_block, abs_out, ...],
                  cwd=脚本目录, env=C#最小环境)
  禁止使用 import run() 或相对路径作为唯一验证手段。

规则 V-2（逐项枚举强制）:
  验证输出时必须逐个检查所有已知的关键项：
    - 每个 known serial number 格式是否在 silentSkipped
    - 每个 GRAY 坐标对应的文本内容
    - RED/YELLOW/GREEN 是否符合预期
  禁止只看 count 就下结论。

规则 V-3（坐标级验证优先）:
  图像判断必须以 JSON 坐标数据为准。
  如 JSON 与视觉不一致，先裁剪原始图像排除伪影，
  再确认画图逻辑是否使用了正确的数据源。

规则 V-4（生产路径确认）:
  排查问题时必须确认：
    (a) Platform 进程的实际路径（Debug vs Release）
    (b) FindScript 解析出的实际脚本路径
    (c) bin 目录是否有脚本副本
    (d) pyc 缓存时间戳
    (e) C# 传参是绝对还是相对路径
```

---

## 修复总结

### v19.21 最终改动（3 处）

| # | 位置 | 改动 | 目的 |
|---|------|------|------|
| 1 | line ~323 | 新增 `_is_serial_number_raw()` | 对 norm_text 之前的原始文本做序列号预检 |
| 2 | line ~1754 | step3: `raw(pt) \|\| norm(pt)` 双重检测 | 覆盖被 norm_text 破坏的批次码格式 |
| 3 | line ~1847 | step4: `raw(bt) \|\| norm(bt)` 对称修复 | 块图侧同样保护 |

### 覆盖的序列号格式

| 格式 | 示例 | 被谁破坏 | v19.21 修复 |
|------|------|---------|-----------|
| 字母+短数字 | QHRW6, QHRLA | _is_qr_ocr_noise (v19.20 修) | _is_serial_number (norm) |
| 纯数字日期码 | 200512, 1250001 | _clean_qr_garbage (v19.20 修) | _is_serial_number (norm) |
| 字母前缀+长数字尾 | **BFF0001**, QH8BY | **_norm_text 尾部剥离** (v19.21 修) | **_is_serial_number_raw** |
| 数字+字母+长数字尾 | **8DV2654**, B280C01 | **_norm_text 尾部剥离** (v19.21 修) | **_is_serial_number_raw** |

### 验证矩阵

| 测试 | v19.19 | v19.20 | v19.21 |
|------|--------|--------|--------|
| QHRLA | ❌ 灰框 | ✅ silent | ✅ silent |
| 200512 | ❌ 灰框 | ✅ silent | ✅ silent |
| 74U119I | ❌ 灰框 | ✅ silent | ✅ silent |
| QH8BY | ❌ 灰框 | ✅ silent | ✅ silent |
| **BFF0001** | **❌ 灰框** | **❌ 灰框** | **✅ silent** |
| **8DV2654** | **❌ 灰框** | **❌ 灰框** | **✅ silent** |
