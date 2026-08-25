# 不良履历查询模块 —— 开发进度归档（暂停）

> 归档时间：2026-08-06 09:45
> 状态：**已暂停并归档**。共享平台文件（Program.cs / index.html）已全部还原，平台恢复原有功能。
> 重新启用方法见文末「如何恢复」。

## 一、已完成的开发内容

### 1. 需求（用户 2026-08-05 提出）
- 用 `E:\生产不良履历` 做数据源，H5 端按「型号」查询跨所有月份表格的内容并移动端友好展示；
- 支持 H5 单条录入 + 批量上传 `.xls/.xlsx` 文件并入数据源；
- 数据源表格格式固定，只能按现有格式增删；不同月份建同样格式的表格。

### 2. 已确认的技术决策（用户 2026-08-06 选定）
- 写入：`xlrd`(读)+`xlwt`(写) 直接改写 `.xls`，保持原格式（R0 合并标题/8 列表头/两 sheet 保留）；
- 型号匹配：模糊包含（忽略大小写/空格）；
- 批量入口：上传 `.xls/.xlsx` 文件；缺失月份自动建同格式模板。

### 3. 已归档代码（3 个文件，本目录）
| 文件 | 内容 |
|---|---|
| `defect_history.py` | Python CLI：`meta / query / add_single / batch_import / create_month / delete_row`；xlrd 读 + xlwt 重写保留格式；写前 `.bak` 备份 + 临时文件原子替换 + `.lock` 互斥；stdout 输出 JSON |
| `DefectHistoryService.cs` | C# 服务：复用 `OcrService.CreatePythonPsi`（干净环境）；静态 `_writeLock` 串行化写操作；5 端点 |
| `DefectHistoryModule.cs` | IModule 插件：Key=`defecthistory`，挂载 `/api/defecthistory/{meta,query,add,batch,delete}` |

### 4. 已验证的成果（V-1 冒烟，副本目录 data/defecthistory_test 上全过）
- meta / query（模糊匹配 FD01、rd504 跨月命中正确）；
- add_single 自动建缺失月份（2026-06）并写入；
- create_month 新建月份（2026-07）；
- delete_row 删除行（dry-run 与真实）；
- batch_import：`.xlsx` 与 `.xls` 均成功，含无效行跳过、`2026-05` 缺失月自动建表、数值列类型原样保留；
- 重写后 R0 合并标题 / R1 8 列表头 / SMT+组装 两 sheet 全部保留。

### 5. 环境变更（已做，可复用）
- 生产 venv `C:\Users\Administrator\.workbuddy\binaries\python\envs\default` 已补装：`xlrd 2.0.2 / xlwt 1.3.0 / openpyxl 3.1.5`（pip 安装，不影响其它模块）。

## 二、平台 / 其它模块影响（已全部还原）

### 还原前改动范围（仅 2 个共享文件 + 3 个新文件）
| 文件 | 原改动 | 现状态 |
|---|---|---|
| `src/Platform/Program.cs` | +using / +Register(DefectHistoryModule) 2 行 | ✅ 已还原（仅 drawings+unbind） |
| `src/Platform/wwwroot/index.html` | MODULE_RENDERERS/subs 加 defecthistory 键 + 占位函数 | ✅ 已还原（无 defecthistory 残留） |
| `src/Platform/Modules/DefectHistory/*`（3 文件） | 新增模块 | ✅ 已移入本归档目录 |

### 未动过的文件（扫描确认，零改动）
`Drawings/`(DrawingModule/DrawingService/OcrService 等) · `Unbind/` · `Core/` · `Infrastructure/` · `csproj` · `appsettings.json` · `start-platform.ps1` / 启动脚本 / `cloudflared` · 端口配置。

## 三、平台「无法连接」的根因与修复

- **根因**：开发中尝试移动 `obj` 目录时遇到文件锁，退化为部分删除，导致 `obj\Debug\net8.0\` 生成文件损坏（CS0579 特性重复），SYSTEM 计划任务构建持续失败 → 平台无法启动。
- **修复**：已把损坏的 `obj`/`bin` 移出（`temp/platform_obj_corrupt` 等），SYSTEM 将全量重建；随后重新启用 SCG_Platform 计划任务即可恢复。
- 该问题**与模块代码无关**（Admin 侧编译验证 0 错误）。

## 四、如何恢复（重新启用模块）

1. 把 3 个文件移回：`_archived_defecthistory\*` → `src\Platform\Modules\DefectHistory\`；
2. `Program.cs` 恢复 2 行注册；
3. `index.html` 恢复渲染键/副标题/渲染函数（见归档前版本）；
4. 重建部署（SYSTEM 计划任务）；H5 端补齐交互 UI（查询/单条/批量上传页面，尚未开发）。
