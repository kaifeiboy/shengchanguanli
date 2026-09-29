# V2 真值标注清单（按用例分组）

> 用法：对每张照片，判断该 mark 在**实物**上是否确有此标示。
> - `present` = 实物有此标（与系统判定无关，凭眼判断）
> - `missing` = 实物确实没有此标（系统判 Matched 即为**假绿**）
> - `offscreen` = 该标在照片范围外/被遮挡看不到（不计召回）
> 填完把每行的 truth 抄回 `truth_template.csv` 的 truth 列，
> 再执行：`python run_baseline.py --import-truth truth_template.csv`

共 42 个用例 / 242 个 mark 待标注。

---

## 一、标注具体步骤（两条等价路径，任选其一）

### 路径 A｜浏览器工作台（推荐，最快）
1. 我方已生成自包含 HTML：`truth_workbook.html`（照片内嵌，离线可用）。
2. 用浏览器**双击打开**它（建议 Chrome/Edge），不要只在内置预览里看——内置预览可能禁止下载。
3. 对每个用例：
   - 在「本照片真实视图」下拉选**实际拍的是产品哪个面**（TopCover 顶盖等）；
   - 看照片，对表格里**每个标**点选 `有标 / 缺标 / 拍不到`；不确定可填备注。
4. 顶部「已填 X / 242」实时计数。全部填完点「导出 truth_answers.json」保存文件。
5. 把 `truth_answers.json` 放回本目录，我执行 `python harvest_truth.py truth_answers.json` 写回 `testset.json`。
6. 我跑 `python run_baseline.py` 全量，出 B 类指标。

### 路径 B｜Markdown 清单（可打印/手写）
1. 直接看下面每个用例的表格（照片路径已给出，可对照打开原图）。
2. 在每行「真值」列手填 `present / missing / offscreen`，并记下本照片真实视图。
3. 抄回 `truth_template.csv` 的 `truth` 列（空白处）。
4. 我执行 `python run_baseline.py --import-truth truth_template.csv` 写回。

> 两条路径最终都进 `testset.json` 的 `truth.marks`；视图真值（路径 A 的 view 下拉 / 路径 B 需手工补）用于 `viewAccuracy`。

---

## 二、合格标准

### （甲）单条标注的质量红线（决定真值可信）
- **凭眼判断**：每个标必须看照片实物判定，**严禁照抄「系统判定」列**（那列只是提示）。
- **三态互斥且唯一**：每个标恰好选 `present / missing / offscreen` 之一，不能空、不能多选。
- `offscreen` 仅限该标**确实在取景范围外 / 被手指或外壳完全遮挡看不到**；"我没看清/不确定"不能选 offscreen，应判 present 或 missing 中更贴近实物的，或补备注。
- `present` = 实物上确实打了对的标（哪怕系统判 NotDetected，只要实物有就选 present——这正是召回要抓的信号）。
- `missing` = 实物确实没打该标（若系统判 Matched，即为假绿；若系统判 Missing，即真缺标系统判对）。
- 视图必须填**真实拍到的面**，不要用系统推断视图蒙混。

### （乙）出数所需的覆盖门槛
- 全量 242 个 mark 都填 → recall / falseRedRate / falseGreenRate 干净出数。
- 至少要把系统判 `Matched / Missing / LowConfidence / NotDetected` 的标填了（共 59+47+24+... 见下表），否则对应分母缺失、指标失真。
- 视图：42 个用例都填视图 → `viewAccuracy` 才出数。

当前 `truth_template.csv` 系统判定分布（提示列，非真值）：
Matched 59 · NotDetected 47 · NotApplicable 66 · LowConfidence 24 · NotComparable 43 · Extra 3

### （丙）项目验收门槛（B 类指标达标线，来自 #15）
| 指标 | 含义 | 合格线 |
|---|---|---|
| viewAccuracy | 自动视图识别正确率 | ≥ 95% |
| recall | 实物有标且系统"看到"的比例 | ≥ 95% |
| falseRedRate | 系统报缺标但其实有标（假红） | ≤ 5% |
| falseGreenRate | 系统报一致但其实缺标（假绿） | ≤ 5% |

> 注：A 类过程健康度（照片合格率/视图识别率/锚点生效/高置信占比/耗时）每跑都自动出，与真值无关；但**不达标就不能说系统"可用"**——目前高置信占比 44.4%（门槛 90%）、P95 38.8s（原用户诉求 <20s）仍差距明显。

---

## 三、用例清单

## p61-f403feb6  （照片：data/drawingsv2_photos/202609/f4_f403feb6d8d66021521a925fcde440d4c8490709cfeade40fd8b992572a5d8a8.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | LowConfidence |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-ed96d26e  （照片：data/drawingsv2_photos/202609/ed_ed96d26e188cf3738a36648618fe42f88148bc3c170ecbc6350f831d369b25b6.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | NotComparable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotComparable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | NotComparable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | NotComparable |  |

## p61-89ec3ab0  （照片：data/drawingsv2_photos/202609/89_89ec3ab0dcb473483673f09b2af11f1b1dddb9aed4d7cee457ada0baae9d542f.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | LowConfidence |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-1550cee2  （照片：data/drawingsv2_photos/202609/15_1550cee2117b9be7b5460a96838a2686e11956e7878a6568cacad9cc171dee8b.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | LowConfidence |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-c91b6258  （照片：data/drawingsv2_photos/202609/c9_c91b6258023f4fb30ea3663c617569cbfa15262bbac9e189aa0fc59a29f828db.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-b8f7be02  （照片：data/drawingsv2_photos/202609/b8_b8f7be02d6e23ee4fb3f82b49a24918af2d3bc3c7be6ca435d9cc89d273f9b15.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | LowConfidence |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-77534c34  （照片：data/drawingsv2_photos/202609/77_77534c342b57765df29bb19ae82df3bcd68361fb514be7e90fad3ba56a9b6405.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | LowConfidence |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-e783f363  （照片：data/drawingsv2_photos/202609/e7_e783f363642990d03d3c01275c780f68a0ef42f564b3f9459322bea9227f7ceb.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | NotComparable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotComparable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | NotComparable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | NotComparable |  |

## p61-150010ad  （照片：data/drawingsv2_photos/202609/15_150010ad4c6f482c644b238066e7cc3fcf04391cd6435ec4de40352ced2bf093.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | LowConfidence |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-6f206d21  （照片：data/drawingsv2_photos/202609/6f_6f206d216f128341f9d6c1b4c5d6f50a6d5d0f175d25618e1f7f2149e899b802.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-0253298c  （照片：data/drawingsv2_photos/202609/02_0253298c428bf08245120e32540b630b650129dd6ea59a35108abf048324dcce.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-ab54f291  （照片：data/drawingsv2_photos/202609/ab_ab54f2916c6948beb2b25f7306bf62bd21323ef211d2da2df6ae38395727c23c.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p61-64d97161  （照片：data/drawingsv2_photos/202609/64_64d971618edb64178292a85ccc6001529c3842e34b70e9655ef9a07369c4be9d.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HVQ效果图25.6.20-Model#000 | 服务热线 | TopCover | Matched |  |
| 02-PC-P1HVQ效果图25.6.20-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| 02-PC-P1HVQ效果图25.6.20-Model#002 | NFC | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#003 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#004 | QHRW5 | TopCover | NotApplicable |  |
| 02-PC-P1HVQ效果图25.6.20-Model#005 |  | TopCover | LowConfidence |  |
| 02-PC-P1HVQ效果图25.6.20-Model#006 |  | TopCover | Matched |  |

## p62-7b3bf646  （照片：data/drawingsv2_photos/202609/7b_7b3bf6465f99f2b54f0f9ab1123317238894c5ee2ed7a77f469c63c134548406.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HJQ效果图25.7.15dwg-Model#001 | 服务热线 | Side | NotComparable |  |
| 02-PC-P1HJQ效果图25.7.15dwg-Model#002 | 禁止强电 | Side | NotComparable |  |
| 02-PC-P1HJQ效果图25.7.15dwg-Model#003 | DC15/24V | Side | NotComparable |  |
| 02-PC-P1HJQ效果图25.7.15dwg-Model#004 | 禁止强电+DC15/24V | Side | NotComparable |  |

## p62-3452fef2  （照片：data/drawingsv2_photos/202609/34_3452fef2bfe8ce95433efd999936f1557d237e159d35d5f7b32922aba754aa16.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-PC-P1HJQ效果图25.7.15dwg-Model#001 | 服务热线 | Side | NotComparable |  |
| 02-PC-P1HJQ效果图25.7.15dwg-Model#002 | 禁止强电 | Side | NotComparable |  |
| 02-PC-P1HJQ效果图25.7.15dwg-Model#003 | DC15/24V | Side | NotComparable |  |
| 02-PC-P1HJQ效果图25.7.15dwg-Model#004 | 禁止强电+DC15/24V | Side | NotComparable |  |

## p63-3ca0bfd4  （照片：data/drawingsv2_photos/202609/3c_3ca0bfd41326f0fafa0109ba99f7d16a87f732eb97f4b6f907dce6ef6d5a253d.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-c8509be1  （照片：data/drawingsv2_photos/202609/c8_c8509be1a4ab89780fde504d6c5101c6fc353321d8bafccaf9f2ea5558f9018b.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-5a64e4d2  （照片：data/drawingsv2_photos/202609/5a_5a64e4d2dbe52c605a9d0a92b576a225e6fd2ba71278fc891d6b95deedfb2fa1.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-05617ef6  （照片：data/drawingsv2_photos/202609/05_05617ef68856374cc85b328bbf09457d967895b2f0f7d559f369c9c4cf1c00cb.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#001 | 底部信息 | BottomCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#002 | 后盖接线标识 | Side | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-75c033fc  （照片：data/drawingsv2_photos/202609/75_75c033fca8f50f5d5bcb988f1c77e015e55e65fcea2173527320eda2e49e277f.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-ade5622b  （照片：data/drawingsv2_photos/202609/ad_ade5622b342b00da6ffdb317aba25c78df7fbef79d8cc65096ee7fd90d4b39a6.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | NotComparable |  |

## p63-b977114e  （照片：data/drawingsv2_photos/202609/b9_b977114e3a4d1bc3b574f15c3c0e1ca8978f474f5861913d3a4a30b4fa36039a.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | NotComparable |  |

## p63-7d6f7bf5  （照片：data/drawingsv2_photos/202609/7d_7d6f7bf55d995dc1dc8e2601986c4bfcce02af424ea50b6fc6cc8b985b28c1d2.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-57987cb4  （照片：data/drawingsv2_photos/202609/57_57987cb4a41e2061c5e5e4fd4a944869bcb1a5ab2fd2c2a3c04d11c1bd433788.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | Matched |  |

## p63-08a0d038  （照片：data/drawingsv2_photos/202609/08_08a0d038f324074e88fe1b13cfc3bdf3e400d9172d6b3c0c2b9ec11449ba54b4.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-6a15dd8d  （照片：data/drawingsv2_photos/202609/6a_6a15dd8da88ebbac6a43b5bd7fd4b189690860a780b528d8027f0cd6b070e6fc.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | Matched |  |

## p63-5cd95a01  （照片：data/drawingsv2_photos/202609/5c_5cd95a012e8f6b4a80f4e3f2232cd233daeb3f8d79744dad81fe643ab3d406b8.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#001 | 底部信息 | BottomCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#002 | 后盖接线标识 | Side | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-7cec2f7d  （照片：data/drawingsv2_photos/202609/7c_7cec2f7d648d5f27e4d4174cff44e0cfaa2cd49363afbfdb67b82a89956f2410.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-da122f48  （照片：data/drawingsv2_photos/202609/da_da122f4822f0a448a96b2a7a8a226e9a3c5d1c72c63d366d76d6fb67d8f345a8.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | Matched |  |
| extra#0008 |  |  | Extra |  |

## p63-12bbbddf  （照片：data/drawingsv2_photos/202609/12_12bbbddfc8cd22a9a8eed62ff4994944fa2a3c228f7f5912bc86993837e99f42.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | Matched |  |

## p63-12221415  （照片：data/drawingsv2_photos/202609/12_12221415cb552b1fc0b5771c760d6e171104dc369788cd8eb0dd8a2c279dc261.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | NotComparable |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | NotComparable |  |

## p63-1c6f32f0  （照片：data/drawingsv2_photos/202609/1c_1c6f32f0e3a2666e3fb00e148cfb624c6a2667cc4b2586a0fef6f68ef6070831.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | Matched |  |

## p63-0635f4ff  （照片：data/drawingsv2_photos/202609/06_0635f4ff20e13b1e274d0b2a4b5e869d50b66b2f34a82cc025b6d88d46ccc687.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#000 | 上盖顶部 | TopCover | NotApplicable |  |
| 02-新约克86线控器效果图-25.7.15-Model#003 | 禁止强电 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#004 | 地暖阀 | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#005 | DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#006 | 禁止强电+地暖阀+DC15/24V | TopCover | NotDetected |  |
| 02-新约克86线控器效果图-25.7.15-Model#007 |  | TopCover | Matched |  |
| 02-新约克86线控器效果图-25.7.15-Model#008 |  | TopCover | LowConfidence |  |
| 02-新约克86线控器效果图-25.7.15-Model#009 |  | TopCover | Matched |  |
| extra#0008 |  |  | Extra |  |

## p63-0704699d  （照片：data/drawingsv2_photos/202609/07_0704699dd9b7fe77eee529c2da3be35a3b18c64b1042336ef31e50aadfea4bcd.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-ec883a33  （照片：data/drawingsv2_photos/202609/ec_ec883a338bb2411f6a4d436e7e67aad6a1948db412cdfa4ebb6474b9e3056a04.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p63-4798e3d5  （照片：data/drawingsv2_photos/202609/47_4798e3d5aca5d10aaba04ab80a80e851b2f719c58dc19c08c6bf09bfa11b37ed.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| 02-新约克86线控器效果图-25.7.15-Model#model-YCWA15NCWQ | YCWA15NCWQ | Nameplate | NotApplicable |  |

## p64-d470809b  （照片：data/drawingsv2_photos/202609/d4_d470809b08b65865f0177e98d1d02e2e9370a7568a349dab355e2e5fa1a3105a.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| PC-P1HEQ-效果图25.7.15-Model#000 | 服务热线 | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#002 | DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#003 | 禁止强电+DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#004 |  | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#005 |  | TopCover | LowConfidence |  |

## p64-bed894ce  （照片：data/drawingsv2_photos/202609/be_bed894ce76792a72740c1bf9be6c09f467a8333c1295f4d492ae2a973b16a249.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| PC-P1HEQ-效果图25.7.15-Model#000 | 服务热线 | TopCover | NotComparable |  |
| PC-P1HEQ-效果图25.7.15-Model#001 | 禁止强电 | TopCover | NotComparable |  |
| PC-P1HEQ-效果图25.7.15-Model#002 | DC15/24V | TopCover | NotComparable |  |
| PC-P1HEQ-效果图25.7.15-Model#003 | 禁止强电+DC15/24V | TopCover | NotComparable |  |
| PC-P1HEQ-效果图25.7.15-Model#004 |  | TopCover | NotComparable |  |
| PC-P1HEQ-效果图25.7.15-Model#005 |  | TopCover | NotComparable |  |

## p64-29537114  （照片：data/drawingsv2_photos/202609/29_295371141c01f30ce1ba2f7d779ab3d5c89a1d00c89ef5b088840e3829cbe313.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| PC-P1HEQ-效果图25.7.15-Model#000 | 服务热线 | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#002 | DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#003 | 禁止强电+DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#004 |  | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#005 |  | TopCover | LowConfidence |  |

## p64-69716771  （照片：data/drawingsv2_photos/202609/69_69716771c8e072de3025eb6b8d3cb1506f1e1bc915826499771b1a0baea0bcbe.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| PC-P1HEQ-效果图25.7.15-Model#000 | 服务热线 | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#002 | DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#003 | 禁止强电+DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#004 |  | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#005 |  | TopCover | LowConfidence |  |

## p64-543710f9  （照片：data/drawingsv2_photos/202609/54_543710f9e6cbf440cdeba7015773b4fefe86bf51d86f2bb5ee589cd1223b0e9a.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| PC-P1HEQ-效果图25.7.15-Model#000 | 服务热线 | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#002 | DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#003 | 禁止强电+DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#004 |  | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#005 |  | TopCover | LowConfidence |  |

## p64-ae4973b4  （照片：data/drawingsv2_photos/202609/ae_ae4973b4ad6a383e11aa7aaf7f525e364066102efd51bf0280f73069ec015368.jpg）

| markKey | 期望文本 | 视图 | 系统判定(提示) | 真值 |
|---|---|---|---|---|
| PC-P1HEQ-效果图25.7.15-Model#000 | 服务热线 | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#001 | 禁止强电 | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#002 | DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#003 | 禁止强电+DC15/24V | TopCover | NotDetected |  |
| PC-P1HEQ-效果图25.7.15-Model#004 |  | TopCover | Matched |  |
| PC-P1HEQ-效果图25.7.15-Model#005 |  | TopCover | LowConfidence |  |
| extra#0006 |  |  | Extra |  |
