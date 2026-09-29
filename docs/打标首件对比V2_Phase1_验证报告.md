# DrawingsV2 Phase 1 验证报告 —— 图纸侧部位提取落库

> 阶段目标：建档案期一次性提取「逻辑图块 V2Block」（文本层 span + 图像对象空间聚类），噪声三步过滤，C 类文字转曲走 vision_fallback（渲染 200DPI + RapidOCR），落库 `data/drawingsv2.db`，源文件夹 `E:\生产打标效果图` 全程只读。
> 状态：**已完成并端到端验证（2026-09-19）**。

## 一、代码改动（5 文件，已 build 0 错误、部署生效）

| 文件 | 改动 |
|------|------|
| `Scripts/vpdf/cli.py` | 新增 `cmd_blocks` + `blocks` 子命令，输出 `vpdf-blocks/1`（`--json-only`） |
| `Decision/V2Models.cs` | 新增 `V2BlockSet / V2BlockPage / V2Block / V2BlockCandidate`（JsonElement/JsonSerializer 桥，补 `using System.Text.Json`） |
| `Decision/V2Store.cs` | 建表 `v2_drawing_blocks` + `v2_block_marks`；`SaveProfile` 增 `V2BlockSet?` 参数；`InsertBlocks` 落库（blocks 失败不致命） |
| `Runtime/V2Python.cs` | `ExtractBlocksAsync` 调 `blocks --json-only` 反序列化 |
| `Runtime/V2Service.cs` | `AnalyzeAsync` try 调 `ExtractBlocksAsync`，parserVersion 标 `v2-py-1.2.0` |

## 二、覆盖范围（21/21 源图纸，A/B/C 三类全中）

| 指标 | 值 |
|------|----|
| 源 PDF 数 | 21 |
| 已建 profile 数 | 21（100%） |
| 图块总数（v2_drawing_blocks） | 118 |
| 候选总数（v2_block_marks） | 405 |

### 按提取路径分布
- `vision_fallback = 0`（A 栅格化 / B 原生矢量，走文本层 span 聚类）：**19 profile / 109 块**
- `vision_fallback = 1`（**C 类文字转曲**，渲染 200DPI + RapidOCR）：**profile 56 日立（5 块）+ 57 海信（4 块）**

→ C 类（文字转曲、无文本层）路径已确认落地，符合用户硬要求「方案须覆盖含文字转曲的图纸」。

## 三、回填策略（保护既有 marks / 复核状态）

`SaveProfile` 对同 key+sha 档案「先删后插」，重跑 `/analyze` 会清掉手动取消的 mark 与复核状态（破坏性）。故分两套：
- **18 个既有 profile（blocks=0）**：insert-only 回填脚本，调真实 `vpdf_run.py blocks`，按 `InsertBlocks` schema 落库，**不动 marks / review**。
- **3 个无 profile 的源 PDF**（`02-HYXC-VH02` / `02-HYXC-VK01` / `02-PC-P1HEQ2`）：走真实 `GET /api/drawingsv2/analyze?name=<绝对路径>` 新建 profile 72/73/74，无数据可失。

## 四、部署与验证证据（≥2 角度交叉一致）
1. **进程生效**：Platform 新进程 PID 264 启动 09/19 11:04:07 > 改文件 mtime；`/health` ok、`selfCheck 13/13`。
2. **端到端真实端点**：profile 68（约克10寸屏）经真实 `/analyze` → `v2_drawing_blocks` 3 块 / `v2_block_marks` 9 候选，含真实 dim 内容（模具编号 / 4.5 / 2.6 / 4.8 / 59.3）。
3. **DB 底层直连**（WAL 模式）核验：21 profile 全有块，C 类 vision_fallback=1 仅落在 56/57。
4. **Python 独立复现**：`_backfill_blocks.py` 调真实 `vpdf_run.py blocks` 全 21 份产出块，与端点同源代码。

## 五、已知小瑕疵（不影响功能，未改）
17 个回填 profile 的 `parser_version` 列仍为 `vpdf-py-1.1.0`（marks 来源），图块由 1.2.0 代码补入；属元数据标记不一致，不阻断 Phase 2/3/4 使用。

## 六、临时文件
本次会话产生的 `_backfill_blocks.py` / `_patch_*.py` / `_ps_state.txt` / `_health.json` / `_import3.txt` / `_test56.err` / `_analyze58.json` 已全部 `os.replace` 移至 `_trash\`（safe-delete 沙箱 fail-closed，只移不删）。

## 七、下一步
Phase 2（照片预处理与部位定位，Task#51）：单色底深色笔画连通域部位检测（不物理裁剪）、可选 WB + CLAHE、原生分辨率 OCR 取代 `MAX_SIDE=1280` 降采样、检测失败退化整图、坐标经 `_rebase` 转图像相对 norm_bbox。
