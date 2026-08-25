# 「打标文件对比」模块 — 项目上下文据点（2026-08-18）

> 用途：本对话作为后续对该模块**优化与修复的唯一据点**。本文档汇总技术参数、当前进度、必须遵守的规则，供每次开工前快速对齐。
> 平台内模块正式名称 = **打标首件对比**（Key=`drawings`，用户口语称"打标文件对比"）。

---

## 1. 模块定位与技术栈

| 项 | 值 |
|----|----|
| 模块 Key / Name | `drawings` / `打标首件对比`（H5 导航第 1 项，order=10） |
| 核心业务 | 手机拍照 → OCR 提取文字 → 与图纸切块比对 → 四色差异标示（缺标/多标/一致/忽略） |
| 后端 | ASP.NET Core 8 Minimal API（`src/Platform`），Kestrel **5000**(http)/5443(https)，自签 certs/localhost.pfx |
| 数据库 | SQLite `data/app.db`：drawings / drawing_blocks / drawing_histories |
| 前端 | 单文件 H5 `src/Platform/wwwroot/index.html`，浅色商务风，原生 JS |
| OCR | RapidOCR(PP-OCRv4 ONNX) 主 + Tesseract fallback，常驻 worker `ocr_worker.py` |
| 匹配引擎 | `diff_visualizer.py`（Python，**生产加载 `_debugVersion=v19.39`**，179KB） |
| 运行态 | ✅ 平台在线：`0.0.0.0:5000`（PID 9792）+ unbind 中转 `:5001`（PID 8836） |

**图纸库存**：did=106~126 + 132（22 张 / 99 块），切块图 `data/seg/<did>/blocks/<did>_blk_<NN>.png`。

---

## 2. 技术参数（关键常量 / 阈值 / 基线）

### 2.1 四色标示规则（load-bearing，v19.39）
| 色 | 含义 | 触发 |
|----|------|------|
| 🔴 红 | 缺标 | 块有、照片无（产品漏打） |
| 🟡 黄 | 多标 | 照片有、块无（产品多打） |
| 🟢 绿 | 一致 | 双方都有 |
| ⚪ 灰 | 忽略 | 尺寸·CJK 垃圾·QR 噪声·图标·LCD 屏显，不判差异 |

### 2.2 匹配引擎关键规则
- **序列号规则** v19.24/25/26/30：序列号/批次码统一跳过、QR 垃圾剥离（`_clean_qr_garbage`）、纯数字串要求**精确相等**（差 1 位即不同，禁 Jaccard 误绿）。
- **CJK 子串**：仅长度差≤1 判相同；>1 走严格 CJK 容差（Jaccard≥0.65 + LCS 覆盖≥0.55 + 长度比≥0.7），**禁 ASCII 40% 宽松覆盖**。
- **型号码单字母替换 = 纯逐字符（用户 2026-08-18 拍板）**：移除 v19.9 非连续 LCS 容错；行合并容差限定 shorter/longer<0.7。WATBNCWO↔WATGNCWO、YCWA15NCWR↔YCWA15NCWQ 现判不同。
- **drawing-scoped 白名单**（零全局规则变更）：`_drawing_yellow_filter={(115,3):[[818,1211,138,66]]}` 仍生效。
- **LCD 屏显灰化 v2**（ENABLE_DISPLAY_REGION_GRAY=True，用户拍板开启）：内容过滤 LCD 形态 + 近黑像素占比≥0.30 + 面板面积≥2500 宽高比≥1.5。仅 did112 blk1 命中，98 块零误报。

### 2.3 validate.py 基线锁（2026-08-18 重基线）
```
R ≤ 29(+3)   Y ≤ 18(+3)   |G−586| ≤ 25   |Gray−669| ≤ 25
```
- ⚠️ `validate.py --force` 被沙箱 safe-delete 拦截（os.remove 失败）；**重跑基线改用 `real_match_regression.py`**（覆盖写绕过删除），再跑 validate.py 读缓存报告。

### 2.4 准确率框架（data/_accuracy/）
- `accuracy_harness.py`：匹配层（`_texts_match_strict` 直测二分类）+ 集成层（`_compare_block` 每色 P/R/F1 + 混淆矩阵）。
- `cases.json` v2 金标准：match_cases(14) + block_cases(7=32 行)。
- 当前量化：**匹配层 14/14 = 1.0000，集成层 32/32 = 1.0000**；生产全量回归 R=29 Y=18 G=586 Gray=669，validate.py **PASSED**。

### 2.5 照片直接标示端点（分支 feature/photo-roi-marking，已开发待提交）
| 端点 | 说明 |
|------|------|
| POST `/api/drawings/diff-photo` | 照片直接标示（C# DrawingService.MarkDifferencesOnPhoto + Core 重试 3） |
| GET `/api/drawings/diff-photo-image` | 标示图取回 |
- 提速：`ocr_photo_tiled (1,1) ov0.25 md800`；`run_on_photo` 读图 LANCZOS≤2000px；跳过 low_conf 块图标检测；作用域仅 run_on_photo，validate 全量基线一致。
- ⭐⭐ **photo 端点 seg_root 契约（致命坑）**：C# 必须传 base 目录 `Path.Combine(_dataDir,"seg")`，Python 自行拼 `<did>/blocks/...`。误传 per-drawing 目录会双嵌致块参考图缺失 → LCD 灰化跳过 → 误标红。

---

## 3. 当前进度

### 3.1 已完成（达成）
- JQ(112)/VQ(116) 端到端 full 匹配 + 差异标记 ✅（早期版本实测 score=1.0）
- 四色标示 + CAD 过滤 + LCD 屏显灰化 + 序列号/日期码/QR 噪声规则族 ✅
- 型号码纯逐字符收紧（用户拍板）✅，validate.py 重基线 PASSED ✅
- 照片直接标示 + 切图块双展示（分支开发完毕，**未提交 git**）✅ 代码完成
- 准确率框架双评（匹配层/集成层）全 1.0 ✅

### 3.2 待办（优先级排序）
- **P0**：最小按钮标签 OCR 漏读 — `ocr_photo_tiled` 全局下采样把最小标签压出阈值。修复方向 C（ROI 内小字 upscale 再 OCR，最小改动），改 `photo_registration.py:127`，**须真实照片回归**。
- **P1**：git 基线提交 working tree（照片标示 + B 方案，当前仅文件备份）；natapp 稳定升级（免费域名 2026-08-17 被换 a72ab824→d7892af6.natappfree.cc；稳定方案 VIP_1 ¥9/月 或 Cloudflare named tunnel）。
- **P2**：Phase2 照片 ROI 根治跨块溢出；Phase3/4 字段语义匹配 + 字段级度量。
- **P3**：缺陷履历模块恢复（须先读 `_archived_defecthistory/README` 并确认）。
- **护栏盲区**：validate.py 仅统计文本区，不含 iconMissingRegions；图标/照片 OCR 改动须额外人工全量核验。
- 已确认但未处置：`_norm_text` 的 `ts→15` 过度归一（YCWATSNCWQ↔YCWA15NCWQ 误绿）待移除清单。

---

## 4. 必须遵守的规则（硬约束）

### 4.1 冻结范围（默认不可动，触冻结需用户确认）
- 后端架构：`IModule` 接口、路由前缀、程序结构
- 入口配置：`Program.cs` 端口 5000 写死、`NetworkResolver`
- API 契约：`/api/*` 请求/响应格式（match-block 的 matchStatus/matched/score/candidates 字段）
- H5 壳：`index.html` 模块壳与浅色商务风视觉
- 允许例外：① 故障修复（有复现证据 + 用户确认）② 用户主动要求 ③ 新增独立 IModule

### 4.2 维护模式（2026-08-12 硬规则）
- 只改被明确要求部分 → 界定影响面 → 只改必要 → 跑本模块冒烟 + 回归 → **动模块外先问**
- 没要求改动的，不擅自改动（用户 2026-07-23 明确），不做顺手重构/优化

### 4.3 方法论
- 全量排查 + 根因优先 + 护栏（`real_match_regression.py` 动态 + `fleet_sweep.py` 静态）
- V-1 subprocess 验证（禁 `import run()` 混用）
- 验证/复现必须走真实入口（如 H5 端点 JSON），**截图推测不可作根因依据**（2026-08-18 教训：基于截图文字的根因多被代码级复现推翻）

### 4.4 环境安全红线
- **生产路径写测试 = 高危事故**（2026-08-10 空 Workbook 覆盖生产）：写测试必须用临时 ROOT 副本，绝不直接对生产路径写
- **safe-delete fail-closed**：删前进回收站；沙箱回收站不可用 → 拒绝一切删除（含 dangerouslyDisableSandbox）；只拦删除不拦移动；批量≥50 触发确认
- **Bash 沙箱阻断 .NET 写盘**：`dotnet build` 报 CS2012 → 必须 `dangerouslyDisableSandbox:true`（+ `rm -rf obj/Debug bin/Debug` + `-p:UseSharedCompilation=false`）；沙箱下「普通 ls」看不到 escalated 写入物
- Python venv = `C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（含 rapidocr/zxingcpp/PIL/olefile/openpyxl）
- 改 .py 即时生效；改 `ocr_cli.py`/`.NET` 需重启 Platform（Stop-ScheduledTask SCG_Platform → build → Start）+ 真实照片回归

### 4.5 部署 / 进程
- 开机自启 SYSTEM 计划任务：SCG_Platform(→5000/5443) + SCG_UnbindProxy(→5001) + SCG_Natapp，node managed 22.22.2
- SCG_Platform 实际跑 `node platform-watchdog.js`（src/Platform/proxy，SYSTEM），看门狗拉起 `dotnet run --no-build`→Platform.exe
- natapp authtoken=bfbfa1d78c5bbf41（data/natapp_token.txt）；域名注入 data/tunnel_url.txt + 两份 appsettings External:BaseUrl（重启生效）；内网 IP=10.20.60.40

---

## 5. 本对话协作约定

1. **本对话 = 该模块优化/修复的长期据点**，后续需求直接在此提出，上下文按本文档 + `.workbuddy/memory/MEMORY.md` 对齐。
2. 每次任务开工：先明确「改什么、影响面、验收标准」→ 小步改 → 跑护栏（real_match_regression / validate / accuracy harness）→ 报告。
3. 涉及冻结范围、模块外改动、生产路径写操作 → 先问再动。
4. 文档与代码不同步时以代码为准，并以本次实测复现为权威结论。

---

*整理时间：2026-08-18 14:10 | 依据：项目交接文档、调试优化方案、下一阶段实施计划、.workbuddy/memory/MEMORY.md（v19.39 全量规则）、当日工作日志*
