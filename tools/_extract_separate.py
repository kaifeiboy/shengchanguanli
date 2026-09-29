# -*- coding: utf-8 -*-
"""
_extract_separate.py —— 只读 + 零损伤部位分离

══ 安全约束（本次任务硬要求：不得损坏原图）══
  1. 源 PDF 全程 fitz.open(path) 只读打开，**从不调用 doc.save() / insert / delete / annotate 到源 doc**
  2. 所有产物一律写入独立的 outputs/ 目录，源文件按字节不动
  3. 运行前后各计算一次源文件 sha256 并比对，写进 report.json 作为留证
  4. 可视化在 numpy 渲染副本上绘制（不在 document 上画），最后只存 PNG

══ v6（2026-09-24 晚）成员按 bbox 包含 + 种子去重 + 二维码格式说明剔除区 ══
  用户反馈（p62）：①P1 把「上盖二维码格式说明」并进来了，应舍弃；②P2/P3 完美；
  ③另有两个部位没被单独标出，其中一个是「有打标文本的面板」。
  根因（_seed_audit.py 实证）：p62 有 9 个种子只出 3 块 —— 真部位的打标文本
  被几何一级规则丢掉（96 项丢 86）→ 种子「无 kept item」被防幽灵框规则跳过。
  修复：
    A. 成员判定改为「item 中心落在部位 bbox 内」（含被几何丢弃的项）——
       部位内部的内容天然属于部位；语义补刀因此拿到全文（技术要求表头恢复）。
    B. 种子去重：cell-bbox IoU>=0.5 或 containment>=0.85 = 同一实体碎裂（如标题栏
       #5/#8 重复、前面板两个组件嵌套）→ 合并，防重复出块。
    C. 「二维码格式说明」前置剔除区：指纹文本（二维码格式/以上内容按实际产品型号
       + >=2 个字段标签）→ 连同 60pt 内的 QR 图案整区剔除，区内矢量不参与轮廓格子、
       区内 item 不参与任何判定（对应用户舍弃列表第 4 类，且不影响产品上的二维码）。
  部位切分单位仍是 v5 的「轮廓骨架 8-连通域」（骨架断开 = 必然两个部位）。

══ 当前共识的判定规则（2026-09-24 全量实测）══
  保留某 item = 所在矩形内矢量路径数 >= 25     ← 「有产品轮廓」，AUC 0.960
             OR 该 item 自身是内嵌图像          ← 豁免产品上的打标二维码
             OR 距页面最近边缘 >= 20%
  （注：全回召 94.2% / 精准 91.8% / F1 0.930；误差尾量由文本语义二级补刀兜底）

用法：python _extract_separate.py [--pid=ID]   # 不给 pid 则随机挑一张
"""
import io, os, sys, json, random, hashlib, datetime
import re
import numpy as np
import fitz
import cv2

DB = r"E:/workaaa/shengchanguanli/data/drawingsv2.db"
OUT_ROOT = r"E:/workaaa/shengchanguanli/outputs"
SCRIPTS = r"E:/workaaa/shengchanguanli/src/Platform/Modules/DrawingsV2/Scripts"
sys.path.insert(0, SCRIPTS)

T_DRAWS = 25            # 区域内矢量路径条数阈值（item 几何准入）
T_BORDER = 20.0         # 距页边距阈值(%)
THR_PT = 38.0           # 松散残留聚类间距阈值(pt)
VISION_DPI = 200        # C 类（无文本层）时渲染 OCR 的 dpi
GRID_PT = 24.0          # 矢量网格步长(pt)：把「产品轮廓」变成可聚类的一等 item
GRID_MIN_DRAWS = 12     # 单格矢量路径数达到该值 → 视为「轮廓格子」
MIN_COMP_CELLS = 3      # 骨架连通域 ≥3 格才算「部位种子」（同旧 outline 支撑门槛）
ITEMLESS_MIN_CELLS = 6  # 无任何内容 item 的种子：≥该格数仍成部位（纯图形部位）
FAR_ATTACH_PT = 60.0    # 矢量归属种子的最远距离(pt)；再远不撑框
MERGE_IOU = 0.5         # 种子 cell-bbox IoU ≥ 该值 → 同一实体碎裂，合并
MERGE_CONTAIN = 0.85    # 或小 bbox 被大 bbox 包含比例 ≥ 该值 → 合并
QR_SPEC_HINTS = ("二维码格式", "以上内容按实际产品型号")
QR_SPEC_LABELS = ("制造编码", "日期代码", "供应商代码", "生产流水号")
QR_SPEC_IMG_PT = 60.0   # 指纹文本 60pt 内的图像（= QR 图案）一并划入剔除区
TITLE_ZONE_EXTRA = ()   # 「发放部门/模具编号」等小表无格子桥，走松散池语义拒即可
# 表格文字锚的定向外扩（上/左/右/下 pt）：p67 实证表格顶线 y=458 比锚文字
# （标准化/设计/校对 y>=509）高 51pt，必须向上扩才能覆盖表格框线；
# 下视部位底缘 y≈440 距锚上缘 454 有 14pt 余量，故向上最多扩 55。
TABLE_PAD_UP, TABLE_PAD_SIDE, TABLE_PAD_DOWN = 55.0, 25.0, 12.0
# —— v7 弱格生长 / 孤儿升格（p67 实证驱动）——
# p67 六个真值部位里：正面/底座只有 3 个强格（>=12 矢量），侧视整条 0 强格；
# 阈值降到 4 又会让相邻视图经尺寸线粘连。故：强核成形 + 弱格限深生长 + 孤儿升格。
WEAK_DRAWS = 4          # 弱格子阈值：稀疏轮廓线（单格 >=4 矢量）即可参与生长
WEAK_GROW_CELLS = 2     # 强核沿弱格 BFS 最远生长格数（2 格 ≈ 48pt），防止跨走廊吞并
ORPHAN_MIN_CELLS = 6    # 无强核孤儿弱格团 ≥该格数才升格为种子（侧视条 ≈10 格）
ORPHAN_ZONE_OVERLAP = True  # 孤儿团 bbox 与任何剔除区相交 → 丢弃（防技术要求/表格残边幽灵框）
ORPHAN_MIN_DRAWS = 100  # 孤儿升格种子在终框内的可用矢量数门槛：p67 实证侧视条=147、
                        # 走廊尺寸簇=44~80 —— 无强核背书的种子必须自证部位级轮廓密度

# —— v8 部位框空白走廊精修（p67 实证驱动）——
# 24pt 网格在 <24pt 的真走廊处必然把两侧内容吸进同一格（顶视|正面走廊 11.6pt、
# 正面|侧视走廊 41pt 均失守）；弱格生长/孤儿升格又被「标注带弱格桥」（r5c11，
# y120~144 的 39±0.5/11±0.5 标注区 11 条矢量）把三段标注带连成假孤儿团。
# 修复：种子框生成后，在框内做递归 XY 切分。判据源自 _corr67/_xycut67 实测：
# 真走廊带内【无任何 >=XY_SPAN 跨度的矢量贯穿】——尺寸线/引出线/箭头全部
# 止于走廊一端（C1~C5 实测：0~15 条相交矢量，无一条长跨度贯穿）。
XY_GAP = 7.5         # 最小空白区隔宽(pt)：v8.2 实证定标 —— p50 顶视|正面真空隙
                     # 8pt（两个独立边框，y≈113 与 120.5 各一条）须切开；
                     # p52 P4|P7 共享边框双线（y 340.48/340.96 间距 0.48pt）切碎
                     # 带后最大空白仅 6pt 须保持合并。p67 C1=11.6 亦满足。
XY_SPAN = 8.0        # 参与贯穿判定的矢量最小跨度(pt)：2pt 刻度线/箭头不算连接
XY_MIN_DRAWS = 60    # 切出的子块最少矢量数：p67 真值部位最少 146（侧视），
                     # 标注窄带/部位内空洞窄条 0~50 —— 低于此并回相邻块
XY_MARGIN = 0.08     # 走廊不得贴种子框边缘（8%）：贴边的是外缘空白非部位间隔
XY_MAX_DEPTH = 4     # 递归深度上限
XY_REFINE = os.environ.get("XY_REFINE", "1") == "1"   # 归因开关：0=回退 v7.8 行为
TOUCH_GAP = 2.5      # 贴合合并：两域 bbox 间隙/重叠 |g|<=该值视为贴合(pt)。
                     # p52 实证 P7|P4 种子贴合 0.46pt；p67 正面|侧视 bbox 间隙 2pt
                     # （本体走廊 41pt，撑框虚胖）——须靠带内长矢量二次判定
TOUCH_SPAN = 15.0    # 贴合带内长矢量门槛：垂直贴合看 >=15pt 横线、水平贴合看
                     # >=15pt 竖线（共享边框/连续轮廓）。p52 顶视|正面贴合带内
                     # 9 条矢量全 <8pt 小字符=真空隙不合并；P7|P4 带内双线边框
                     # (105.6/141.8pt)+竖框线连续延伸 → 同一部位（用户拍板）
MIN_PART_SIDE = float(os.environ.get("MIN_PART_SIDE", "30"))   # 部位 bbox 最短短边(pt)
MIN_PART_AREA = float(os.environ.get("MIN_PART_AREA", "4000")) # 部位 bbox 最小面积(pt²)
MERGE_REFINE_CONTAIN = 0.40  # 精修碎片并回阈值：p62 实证 P3 顶部段 contain=0.47，
                             # 0.5 差一点导致部位顶部被掏空；真负例（正面|侧视）交集=0 不受影响


def _rect_dist(a, b):
    """两矩形 [x0,y0,x1,y1] 的间距；相交/接触 = 0"""
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return (dx * dx + dy * dy) ** 0.5


def _center_in(rect, box):
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def dash_group_rects(draws, W, H):
    """共线断续细线组 → 应从「轮廓格子」排除的线 rect 元组集合。
    机械制图里两种线天然呈「同一直线上一串短实线段」：
      ① 中心线/对称线（点划线被 CAD 导出拆成小段，p67 实证 V x=174 共 12 段跨 180pt）
      ② 被标注文字断开的尺寸线（p67 实证 y=137.5 共 4 段跨 153pt）
    它们是视图之间空白带的「填充桥」，必须不参与轮廓格子。
    判据：同向细线（厚<=2pt、单段 4pt~20% 页宽/高）按共线坐标分桶，
    相邻段间隙 <=14pt 连链；链内 >=3 段且总跨度 >=50pt → 整链排除。
    ⚠️ 只排除出【格子】——它们仍可参与归属撑框（与旧行为一致）。"""
    out = set()
    for orient in ("H", "V"):
        lines = []
        for d in draws:
            r = d["rect"]
            if orient == "H":
                if r.height <= 2.0 and 4.0 <= r.width <= W * 0.2:
                    lines.append(r)
            else:
                if r.width <= 2.0 and 4.0 <= r.height <= H * 0.2:
                    lines.append(r)
        buckets = {}
        for r in lines:
            key = round((r.y0 if orient == "H" else r.x0) * 2) / 2
            buckets.setdefault(key, []).append(r)
        for rs in buckets.values():
            rs.sort(key=lambda r: (r.x0 if orient == "H" else r.y0))
            chains, cur = [], [rs[0]]
            for r in rs[1:]:
                gap = (r.x0 - cur[-1].x1) if orient == "H" else (r.y0 - cur[-1].y1)
                if 0.0 <= gap <= 14.0:
                    cur.append(r)
                else:
                    chains.append(cur)
                    cur = [r]
            chains.append(cur)
            for ch in chains:
                if len(ch) < 3:
                    continue
                span = ((ch[-1].x1 - ch[0].x0) if orient == "H"
                        else (ch[-1].y1 - ch[0].y0))
                if span < 50.0:
                    continue
                # 每段都必须「短小」（<=35% 链跨度）：CAD 重描的外框线是一条
                # 长主段 + 数条完全重叠的线（p67 实证 y=25 组主段占跨度 99%），
                # 会被 gap<=14 误连成链——用段长约束把它们与真断续线区分开。
                if any(((r.x1 - r.x0) if orient == "H" else (r.y1 - r.y0)) > 0.35 * span
                       for r in ch):
                    continue
                for r in ch:
                    out.add((round(r.x0, 3), round(r.y0, 3),
                             round(r.x1, 3), round(r.y1, 3)))
    return out


def build_exclusion_zones(items, W, H):
    """前置剔除区列表（v7）：区内矢量不进轮廓格子、不撑部位框、item 不参与判定。
    ① qr_spec：二维码格式说明区（沿用 qr_spec_zone）
    ② table：标题栏/会签栏/图名区——语义文字锚定 + 定向外扩（覆盖表格框线）
    ③ note：>=2 条 >=12 字纯中文说明句聚集区（技术要求等）"""
    from vpdf import blocks as B
    zones = []
    zq = qr_spec_zone(items)
    if zq:
        zones.append({"rect": zq, "kind": "qr_spec"})

    def _pad(rect, left, up, right, down):
        return [max(0.0, rect[0] - left), max(0.0, rect[1] - up),
                min(W, rect[2] + right), min(H, rect[3] + down)]

    notes = [it for it in items
             if it["kind"] == "text" and B._is_pure_explanation(it.get("text") or "")]
    # 纯说明句按小间距聚类（gap<=30pt），只有组内 >=2 条才成剔除区——
    # v7.2 教训：直接并集会把散布全页的说明句（注1/注2/技术要求/产品上的长句）
    # 连成一个横跨大半页的巨区，把真部位的格子全部剔掉。
    groups = []
    for it in notes:
        hit = None
        for g in groups:
            if _rect_dist(it["rect"], g["bb"]) <= 30.0:
                hit = g
                break
        if hit:
            hit["items"].append(it)
            r = it["rect"]
            hit["bb"] = [min(hit["bb"][0], r[0]), min(hit["bb"][1], r[1]),
                         max(hit["bb"][2], r[2]), max(hit["bb"][3], r[3])]
        else:
            groups.append({"items": [it], "bb": list(it["rect"])})
    for g in groups:
        if len(g["items"]) >= 2:
            zones.append({"rect": _pad(g["bb"], 6.0, 6.0, 6.0, 6.0), "kind": "note"})

    # 表格锚：语义词命中 + 位置约束（页底 30% / 左缘 / 右缘）+ 不在既有剔除区内。
    # v7.3 教训：①子串匹配让「日期代码/程序版本号」（隔磁纸字段）命中'日期/版本'
    # 把锚拉到页面中部；②「品质工程部/发放部门」（发放部门表）把锚拉到 x0=0——
    # 两者都会让 table zone 吞掉底座/下视的格子。位置约束 + 优先区排除双保险。
    covered = [z["rect"] for z in zones]

    def _already_covered(rect):
        return any(_center_in(rect, cr) for cr in covered)

    def _pos_ok(rect):
        y0, x0, x1 = rect[1], rect[0], rect[2]
        return y0 > H * 0.70 or x0 < W * 0.20 or x1 > W * 0.85

    ax, ay = [], []
    for it in items:
        if it["kind"] != "text":
            continue
        t = it.get("text") or ""
        if not t:
            continue
        if (any(w in t for w in B.TITLEBLOCK_WORD_HINTS)
                or any(d in t for d in B.SIGNBLOCK_DEPTS if d != "发放部门")
                or B.TITLE_NAME_RE.search(t)):
            if _already_covered(it["rect"]) or not _pos_ok(it["rect"]):
                continue
            ax += [it["rect"][0], it["rect"][2]]
            ay += [it["rect"][1], it["rect"][3]]
    if ax:
        zones.append({"rect": _pad([min(ax), min(ay), max(ax), max(ay)],
                                   TABLE_PAD_SIDE, TABLE_PAD_UP,
                                   TABLE_PAD_SIDE, TABLE_PAD_DOWN),
                      "kind": "table"})
    return zones


def qr_spec_zone(items):
    """识别「二维码格式说明」剔除区（用户舍弃列表第 4 类）。
    指纹：命中 hint 文本 且 字段标签 >=2 个 → 区域 = 指纹文本并集
          + 60pt 内的图像（QR 图案）。
    返回 [x0,y0,x1,y1] 或 None。防误伤：指纹不足时绝不剔除。"""
    hits = [it for it in items if it["kind"] == "text" and
            any(h in (it["text"] or "") for h in QR_SPEC_HINTS)]
    labels = [it for it in items if it["kind"] == "text" and
              any(lab in (it["text"] or "") for lab in QR_SPEC_LABELS)]
    if not hits or len(labels) < 2:
        return None
    xs, ys = [], []
    for it in hits + labels:
        xs += [it["rect"][0], it["rect"][2]]
        ys += [it["rect"][1], it["rect"][3]]
    for it in items:
        if it["kind"] == "image" and _rect_dist(it["rect"], [min(xs), min(ys), max(xs), max(ys)]) <= QR_SPEC_IMG_PT:
            xs += [it["rect"][0], it["rect"][2]]
            ys += [it["rect"][1], it["rect"][3]]
    return [min(xs), min(ys), max(xs), max(ys)]


def grid_matrix(draws, W, H, zones=None, dash_rects=None):
    """矢量密度矩阵：排除 ①长直线（>50% 页宽/高，图框/表格线）②剔除区（zones）
    ③断续共线组（dash_rects）后，统计每个 GRID_PT 格子命中的矢量条数。
    强格（>=GRID_MIN_DRAWS）= 骨架；弱格（>=WEAK_DRAWS）= 稀疏轮廓，供生长。"""
    zones = zones or []
    dash_rects = dash_rects or set()
    cw, ch = GRID_PT, GRID_PT
    ncols, nrows = int(W / cw) + 1, int(H / ch) + 1
    grid = [[0] * ncols for _ in range(nrows)]
    for d in draws:
        rb = d["rect"]
        if rb.width > W * 0.5 or rb.height > H * 0.5:
            continue
        c4 = (rb.x0, rb.y0, rb.x1, rb.y1)
        if any(_center_in(c4, z) for z in zones):
            continue
        if (round(rb.x0, 3), round(rb.y0, 3), round(rb.x1, 3), round(rb.y1, 3)) in dash_rects:
            continue
        for r in range(max(0, int(rb.y0 / ch)), min(nrows, int(rb.y1 / ch) + 1)):
            for c in range(max(0, int(rb.x0 / cw)), min(ncols, int(rb.x1 / cw) + 1)):
                grid[r][c] += 1
    return grid


def cells_from_grid(grid, zones=None, thr=GRID_MIN_DRAWS):
    """密度矩阵 → 达到阈值的格子列表（带 rect/cell），剔除区残余格子丢弃。"""
    zones = zones or []
    cells = []
    for r, row in enumerate(grid):
        for c, n in enumerate(row):
            if n >= thr:
                rect = [c * GRID_PT, r * GRID_PT, (c + 1) * GRID_PT, (r + 1) * GRID_PT]
                if any(_center_in(rect, z) for z in zones):
                    continue      # 剔除区边缘的残余格子一并丢弃
                cells.append({"rect": rect, "kind": "outline", "text": "",
                              "cell": (r, c)})
    return cells


def outline_cells(page, W, H, draws, zones=None, dash_rects=None):
    """把矢量密集的格子作为「产品轮廓 item」。每个格子带 (r,c) 网格坐标。
    （v7 起为 grid_matrix + cells_from_grid 的组合兼容入口）"""
    grid = grid_matrix(draws, W, H, zones=zones, dash_rects=dash_rects)
    return cells_from_grid(grid, zones=zones, thr=GRID_MIN_DRAWS)


def outline_components(cells):
    """★ 把轮廓格子按 8-连通切成【骨架连通域】。
    业务依据（用户拍板）：「图纸中每个产品部位是独立的、有空白区隔的」。
    因此部位应【按空白切分】，而不是按固定间距聚类 ——
    中间哪怕有尺寸数字/标注线，只要轮廓骨架本身断开，就是两个部位。"""
    by_key = {c["cell"]: c for c in cells}
    seen, comps = set(), []
    for start in by_key:
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nb = (cur[0] + dr, cur[1] + dc)
                    if nb in by_key and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        comps.append(comp)          # comp: [(r,c), ...]
    comps.sort(key=len, reverse=True)
    return comps


def cells_bb(cp):
    """模块级：骨架格子列表 → 外接矩形 [x0,y0,x1,y1]（格子是 (r,c) 元组）。"""
    xs, ys = [], []
    for (r, c) in cp:
        xs += [c * GRID_PT, (c + 1) * GRID_PT]
        ys += [r * GRID_PT, (r + 1) * GRID_PT]
    return [min(xs), min(ys), max(xs), max(ys)]


def weak_grow_and_orphans(core_comps, grid, zones=None, max_grow=None,
                          orphan_min=None):
    """【v7】强核限深生长 + 孤儿弱格团升格。返回 (comps, orphan_flags)。
    p67 实证：正面/底座仅 3 个强格（轮廓线稀疏，单格 <12 矢量），侧视整条
    0 强格 —— 只按强格找种子会漏掉 3/6 个真值部位；阈值直接降到 4 又会让
    相邻视图经尺寸线粘连。故分两步：
      ① 以强核（>=MIN_COMP_CELLS 格）为多源种子，在弱格（>=WEAK_DRAWS）上
         BFS 生长 max_grow 步：视图轮廓离自身密集区不会远，能被完整收编；
         走廊另一侧的视图够不到（>=2 格），天然在空白走廊处停住。
         ⚠️ <MIN_COMP_CELLS 的迷你核不作为生长源（p67 实证：1~2 格碎片被
         弱格养大后会冒出 P9/P10 幽灵块），其格子交给认领/孤儿逻辑。
      ② 未被认领的弱格成孤儿团（8-连通）：>=orphan_min 格才升格为种子
         （侧视条 ≈10 格）；bbox 被某生长域深度包含(>=50%)的孤儿团视为同一
         实体的碎片并回该域；与剔除区相交的孤儿团丢弃（技术要求/表格残边）。"""
    from collections import deque
    max_grow = WEAK_GROW_CELLS if max_grow is None else max_grow
    orphan_min = ORPHAN_MIN_CELLS if orphan_min is None else orphan_min
    zones = zones or []
    nrows = len(grid)
    ncols = len(grid[0]) if nrows else 0

    def cell_ok(r, c):
        if not (0 <= r < nrows and 0 <= c < ncols):
            return False
        if grid[r][c] < WEAK_DRAWS:
            return False
        rect = [c * GRID_PT, r * GRID_PT, (c + 1) * GRID_PT, (r + 1) * GRID_PT]
        return not any(_center_in(rect, z) for z in zones)

    sources = [cp for cp in core_comps if len(cp) >= MIN_COMP_CELLS]
    owner, dist = {}, {}
    dq = deque()
    for cid, comp in enumerate(sources):
        for cell in comp:
            owner[cell] = cid
            dist[cell] = 0
            dq.append(cell)
    while dq:
        cur = dq.popleft()
        d = dist[cur]
        if d >= max_grow:
            continue
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nb = (cur[0] + dr, cur[1] + dc)
                if nb in owner or not cell_ok(*nb):
                    continue
                owner[nb] = owner[cur]
                dist[nb] = d + 1
                dq.append(nb)

    grown = {}
    for cell, cid in owner.items():
        grown.setdefault(cid, []).append(cell)
    out = [grown[cid] for cid in sorted(grown)]
    flags = [False] * len(out)

    # —— 孤儿团：未认领弱格 8-连通
    unclaimed = {(r, c) for r in range(nrows) for c in range(ncols)
                 if (r, c) not in owner and grid[r][c] >= WEAK_DRAWS
                 and cell_ok(r, c)}
    seen, orphans = set(), []
    for start in unclaimed:
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nb = (cur[0] + dr, cur[1] + dc)
                    if nb in unclaimed and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        orphans.append(comp)

    grown_bbs = [cells_bb(cp) for cp in out]
    kept_orphans = []
    for comp in orphans:
        if len(comp) < orphan_min:
            continue
        ob = cells_bb(comp)
        if ORPHAN_ZONE_OVERLAP and any(
                not (ob[2] <= z[0] or ob[0] >= z[2] or
                     ob[3] <= z[1] or ob[1] >= z[3]) for z in zones):
            continue                      # 与剔除区相交 = 残边/说明区碎片
        host = None
        oa = (ob[2] - ob[0]) * (ob[3] - ob[1])
        for gi, gb in enumerate(grown_bbs):
            ix = max(0.0, min(ob[2], gb[2]) - max(ob[0], gb[0]))
            iy = max(0.0, min(ob[3], gb[3]) - max(ob[1], gb[1]))
            if oa > 0 and ix * iy / oa >= 0.5:
                host = gi
                break
        if host is None:
            kept_orphans.append(comp)     # 真孤儿（侧视条）→ 升格为种子
        else:
            out[host].extend(comp)        # 同一实体的碎片 → 并回
    out.extend(kept_orphans)
    flags.extend([True] * len(kept_orphans))
    return out, flags


def _hit(r, bb, pad=0.5):
    """矢量 rect 是否「打中」bbox —— 对扁平线（h=0/w=0）必须把零厚维度
    膨胀 pad 再判交，否则平板边框 y=369 的横线与任何 bbox 的 y 重叠恒为 0，
    永远判不中（p67 底座桥线实测教训）。"""
    return (r[0] - pad < bb[2] and r[2] + pad > bb[0] and
            r[1] - pad < bb[3] and r[3] + pad > bb[1])


BRIDGE_MIN_PENETR = 15.0  # 细桥线须沿带轴贯穿两域 bbox 各 >=该深度(pt)：
                          # 平板边框贯穿 41.7/52.3pt ✔；点划中心线进侧视仅 2pt ✘
                          # 【v8.3 起仅作参考值保留，实际判据改用下面的 _SUM】

BRIDGE_MIN_PENETR_SUM = 10.0  # 【v8.3】细桥线两端插入深度【之和】>= 该值即算贯穿：
                              # 旧「双端各 15pt」过严 —— p52 实证：底视|上盖固定板
                              # 之间 24pt 缝处一条竖框线插底视 16.4pt、插固定板仅
                              # 4.48pt（和 20.9）→ 二者实为同一连体部位却误被切开。
                              # 反例仍须挡住：p67 点划中心线两端各 2pt（和 4）✘；
                              # 底座平板边框 41.7+52.3=94 ✔ 不受影响。
                              # 10.0 = 在 20.9(合) 与 4(挡) 之间取中位偏保守。


def bridge_merge(comps, orphan_flags, usable_rects, max_gap=None):
    """【v7.7】同实体碎片桥线合并（并查集，传递闭包）。
    p67 实证：底座被中空切成左右两半（平板中部只有 2 条横线穿过、无格子）、
    正面碎成 3 片 —— 它们是同一视图，必须并回；而 PCB|下视 已被走廊切开、
    侧视与 PCB 只是相邻，绝不能被合并。
    合并四条件（全部满足）：
      a. 两域 bbox 不相交（已重叠 = 走廊切分/去重的既定结果，不碰）；
      b. 同行带（y 重叠 >=0.7*min 高）或同列带（x 重叠 >=0.7*min 宽）；
      c. 间距 <= max_gap（默认 2 格）；
      d. 存在「细桥线」：一条细（厚<=3pt）可用矢量同时打中两域 bbox
        （_hit 判交）——即视图自身的边框/平板边横跨间隙；
      e. 规模相当：两域各自 bbox 内可用矢量数之比 >= 0.30
        —— 防止侧视条(80 条)被 PCB(2434 条) 经相邻同带吞并。"""
    max_gap = (GRID_PT * 2.0) if max_gap is None else max_gap
    n = len(comps)
    if n < 2:
        return comps, orphan_flags
    bbs = [cells_bb(cp) for cp in comps]
    nvec = []
    for bb in bbs:
        nvec.append(sum(1 for r in usable_rects if _hit(r, bb)))

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            a, b = bbs[i], bbs[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                continue                       # a. 已相交 → 不碰
            ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            ha, hb = a[3] - a[1], b[3] - b[1]
            wa, wb = a[2] - a[0], b[2] - b[0]
            same_row = ha > 0 and hb > 0 and iy >= 0.7 * min(ha, hb)
            same_col = wa > 0 and wb > 0 and ix >= 0.7 * min(wa, wb)
            if not (same_row or same_col):     # b. 必须同带
                continue
            gap = max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])
            if gap > max_gap:                  # c. 间距限制
                continue
            if min(nvec[i], nvec[j]) < 0.30 * max(nvec[i], nvec[j]):
                continue                       # e. 规模相当
            bridged = False
            for r in usable_rects:
                if (r[3] - r[1]) > 3.0 and (r[2] - r[0]) > 3.0:
                    continue                   # 细线（至少一维厚度 <=3pt）
                if not (_hit(r, a) and _hit(r, b)):
                    continue
                # d'. 贯穿深度：同一行带 → 桥线横向插入两域、同列带 → 纵向。
                #     v8.3 改判据【两端插入深度之和 >= BRIDGE_MIN_PENETR_SUM】：
                #     p52 实证 底视|固定板 缝 24pt 处竖框线插底视 16.4pt、插固
                #     定板仅 4.5pt（双端各 15pt 旧门槛误杀）→ 和 20.9 ✔ 应合并；
                #     p67 点划中心线两端各 2pt（和 4）✘ 仍挡；底座平板边框
                #     41.7+52.3=94 ✔ 不受影响。
                if same_row:
                    pa = min(r[2], a[2]) - max(r[0], a[0])
                    pb = min(r[2], b[2]) - max(r[0], b[0])
                else:
                    pa = min(r[3], a[3]) - max(r[1], a[1])
                    pb = min(r[3], b[3]) - max(r[1], b[1])
                if pa + pb >= BRIDGE_MIN_PENETR_SUM:
                    bridged = True
                    break
            if bridged:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out, flags = [], []
    for root, members in groups.items():
        cp = []
        for m in members:
            cp.extend(comps[m])
        out.append(cp)
        flags.append(any(orphan_flags[m] for m in members))
    return out, flags


def split_comp_by_gaps(cp, draw_rects, depth=0):
    """【v7 核心】在连通域内部找「空白走廊」切开（用户拍板公理：空白区隔=独立部位）。
    p67 实证：背视与下视之间仅 4pt 空隙、顶视与正面之间隔两层尺寸线——
    24pt 格子在走廊处不断开，必须靠矢量级扫描二次切分。
    走廊三重确认（防误切视图内部空隙）：
      a. 跨越 rect 的 x 覆盖率 <40%（只容忍少量标注线穿越，主体是空的）；
      b. 带上/下 8pt 内各有一条「水平长薄线」（长>=0.5*域宽、厚<=3pt）——
         即两个视图各自的平行外框（边缘对边缘）；
      c. 切开后两侧格子均 >=MIN_COMP_CELLS。
    候选带限制在域 bbox 内 15%~85%（贴边的不是内部走廊）；每域每层只切最宽的
    一条；递归 <=2 层。"""
    if len(cp) < MIN_COMP_CELLS * 2 or depth >= 4:      # 链上最多 4 刀（多视图竖向堆叠）
        return [cp]
    bb = cells_bb(cp)
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    if bw <= 1.0 or bh <= 1.0:
        return [cp]

    def _ov(r, m=6.0):
        return not (r[2] < bb[0] - m or r[0] > bb[2] + m or
                    r[3] < bb[1] - m or r[1] > bb[3] + m)

    rel = [r for r in draw_rects if _ov(r)]   # 只统计与本域相关的矢量

    def _cut(axis):
        # axis='y' 水平切线 / 'x' 竖直切线
        lo, hi, span = (bb[1], bb[3], bh) if axis == "y" else (bb[0], bb[2], bw)
        other_lo, other_hi, ospan = (bb[0], bb[2], bw) if axis == "y" else (bb[1], bb[3], bh)
        edges = sorted({r[1] if axis == "y" else r[0] for r in rel} |
                       {r[3] if axis == "y" else r[2] for r in rel})
        # 带宽 >=2pt：p67 实证 PCB|下视 真实走廊仅 3.0pt（用户拍板：空白间隔
        # 比较小但部位独立）；旧值 4pt 会漏切。
        bands = [(a, b) for a, b in zip(edges, edges[1:])
                 if b - a >= 2.0 and a > lo + 0.15 * span and b < hi - 0.15 * span]
        best = None
        for ya, yb in bands:
            ym = (ya + yb) / 2.0
            cross, cov_xs = 0.0, []
            for r in rel:
                rlo, rhi = (r[1], r[3]) if axis == "y" else (r[0], r[2])
                if rlo < ym < rhi:
                    cov_xs.append((r[0], r[2]) if axis == "y" else (r[1], r[3]))
            cov_xs.sort()
            merged = []
            for x0, x1 in cov_xs:
                if merged and x0 <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], x1)
                else:
                    merged.append([x0, x1])
            cov = sum(m[1] - m[0] for m in merged) / ospan
            if cov >= 0.40:
                continue
            def _edge_line(side):
                for r in rel:
                    r0, r1 = (r[1], r[3]) if axis == "y" else (r[0], r[2])
                    thick = r1 - r0
                    length = (r[2] - r[0]) if axis == "y" else (r[3] - r[1])
                    # 长度门槛必须比【另一侧】跨度（ospan）：p67 主域 bh=336 而 bw=168，
                    # 若用 span(=bh) 则要求横线 ≥168pt，真实视图外框永远不满足 → 全 0。
                    if thick > 3.0 or length < 0.5 * ospan:
                        continue
                    if side == "up" and ya - 2.0 <= r1 <= ya + 8.0:
                        return True
                    if side == "dn" and yb - 8.0 <= r0 <= yb + 2.0:
                        return True
                return False
            if not (_edge_line("up") and _edge_line("dn")):
                continue
            def _part(side):
                out = []
                for (r, c) in cp:
                    c_lo, c_hi = (r * GRID_PT, (r + 1) * GRID_PT) if axis == "y" \
                        else (c * GRID_PT, (c + 1) * GRID_PT)
                    mid = (c_lo + c_hi) / 2.0
                    if (side == "up") == (mid < ym):
                        out.append((r, c))
                return out
            up_cells, dn_cells = _part("up"), _part("dn")
            if len(up_cells) >= MIN_COMP_CELLS and len(dn_cells) >= MIN_COMP_CELLS:
                # 每侧必须跨 >=2 行/列：p67 顶视内部特征线两侧的「单行切片」
                # （顶部窄条 4 格只占 1 行）不得被当成独立部位切开
                up_rs = {rc[0] for rc in up_cells} if axis == "y" else {rc[1] for rc in up_cells}
                dn_rs = {rc[0] for rc in dn_cells} if axis == "y" else {rc[1] for rc in dn_cells}
                if len(up_rs) >= 2 and len(dn_rs) >= 2:
                    if best is None or (yb - ya) > best[0]:
                        best = (yb - ya, ym, up_cells, dn_cells)
        return best

    for axis in ("y", "x"):
        best = _cut(axis)
        if best:
            _, ym, up_cells, dn_cells = best
            return (split_comp_by_gaps(up_cells, draw_rects, depth + 1) +
                    split_comp_by_gaps(dn_cells, draw_rects, depth + 1))
    return [cp]


# ---------------- 安全：只读 + 哈希留证 ----------------
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_items(page):
    """返回 (items, source)。items: [{rect:[x0,y0,x1,y1], kind, text}]"""
    items = []
    for info in page.get_image_info():
        bb = info.get("bbox")
        if bb and len(bb) == 4:
            try:
                items.append({"rect": [float(v) for v in bb[:4]],
                              "kind": "image", "text": ""})
            except Exception:
                pass
    raw = page.get_text("text").strip()
    if len(raw) < 20:
        items += vision_items(page)
        return items, "vision_fallback"
    for sp in page.get_text("dict").get("blocks", []):
        if sp.get("type") != 0:
            continue
        for ln in sp.get("lines", []):
            for s in ln.get("spans", []):
                t = (s.get("text") or "").strip()
                if not t:
                    continue
                items.append({"rect": [float(v) for v in s["bbox"]],
                              "kind": "text", "text": t})
    return items, "text_layer"


def vision_items(page):
    """C 类：渲染 200DPI + RapidOCR。OCR bbox 由像素坐标换算回 pt。"""
    pix = page.get_pixmap(dpi=VISION_DPI)
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.height, pix.width, pix.n)[..., :3][..., ::-1]
    else:
        img = np.stack([img.reshape(pix.height, pix.width)] * 3, axis=-1)
    out = []
    try:
        from rapidocr_onnxruntime import RapidOCR
        res, _ = RapidOCR()(img)
    except Exception as e:
        print("  [warn] OCR 不可用: %s" % e)
        return out
    if not res:
        return out
    scale = 72.0 / VISION_DPI
    for box, txt, score in res:
        try:
            if float(score) < 0.5:
                continue
        except Exception:
            pass
        pts = np.asarray(box, dtype=float).reshape(4, 2)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        out.append({"rect": [x0 * scale, y0 * scale, x1 * scale, y1 * scale],
                    "kind": "text", "text": str(txt).strip()})
    return out


def qualifies(rr, draws, W, H, img_rects):
    """几何一级判定 → (keep, reasons, ndraws, border%)；rr 为 fitz.Rect"""
    nd = 0
    for d in draws:
        rb = d["rect"]
        if rb.x1 < rr.x0 or rb.x0 > rr.x1 or rb.y1 < rr.y0 or rb.y0 > rr.y1:
            continue
        nd += 1
    bd = 100.0 * min(rr.x0, rr.y0, W - rr.x1, H - rr.y1) / min(W, H)
    is_img = any(ir.intersects(rr) and (ir & rr).get_area() >= 0.35 * rr.get_area()
                 for ir in img_rects)
    why = []
    if nd >= T_DRAWS:
        why.append("draws=%d" % nd)
    if is_img:
        why.append("image")
    if bd >= T_BORDER:
        why.append("border=%.1f%%" % bd)
    return (len(why) > 0), why, nd, bd


def cluster(items, thr):
    def gap(a, b):
        dx = max(0.0, max(a["rect"][0], b["rect"][0]) - min(a["rect"][2], b["rect"][2]))
        dy = max(0.0, max(a["rect"][1], b["rect"][1]) - min(a["rect"][3], b["rect"][3]))
        return dx, dy
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            dx, dy = gap(items[i], items[j])
            if dx <= thr and dy <= thr:
                parent[find(i)] = find(j)
    g = {}
    for i in range(len(items)):
        g.setdefault(find(i), []).append(i)
    return list(g.values())


def draw_viz(png_path, pix, W, H, blocks, dropped, img_rects, rejected=None,
             zones=None, skipped=None):
    """在渲染像素副本上画框：
         绿=最终产品部位块  橙=内嵌图像  红细=一级几何舍弃
         青虚=二级语义舍弃  蓝=QR格式说明剔除区  灰=无内容种子(跳过)"""
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = arr.reshape(pix.height, pix.width, pix.n)[..., :3][..., ::-1]
    else:
        g0 = arr.reshape(pix.height, pix.width)
        img = cv2.cvtColor(np.ascontiguousarray(g0), cv2.COLOR_GRAY2BGR)
    img = np.ascontiguousarray(img)          # cv2 要求内存连续
    ph, pw = img.shape[:2]
    sx, sy = pw / W, ph / H

    def R(r):
        return (int(r[0] * sx), int(r[1] * sy), int(r[2] * sx), int(r[3] * sy))

    for r in img_rects:
        x0, y0, x1, y1 = R(list(r)[:4])
        cv2.rectangle(img, (x0, y0), (x1, y1), (255, 180, 0), 2)      # 蓝
    for d in dropped:
        x0, y0, x1, y1 = R(d["rect"])
        cv2.rectangle(img, (x0, y0), (x1, y1), (80, 80, 200), 1)      # 红（一级舍弃）
    for rr in (rejected or []):
        x0, y0, x1, y1 = R(rr["bbox"])
        cv2.rectangle(img, (x0, y0), (x1, y1), (200, 200, 60), 2)     # 青（二级语义舍弃）
        cv2.putText(img, rr["semantic"][0][:6], (x0 + 3, max(14, y0 + 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 60), 1)
    for z in (zones or []):
        x0, y0, x1, y1 = R(z)
        cv2.rectangle(img, (x0, y0), (x1, y1), (255, 120, 0), 2)      # 橙蓝（剔除区）
        cv2.putText(img, "QRSPEC", (x0 + 3, max(14, y0 + 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 120, 0), 2)
    for s in (skipped or []):
        x0, y0, x1, y1 = R(s["bbox"])
        cv2.rectangle(img, (x0, y0), (x1, y1), (160, 160, 160), 1)    # 灰（无内容种子）
        cv2.putText(img, "no_item", (x0 + 3, max(12, y0 + 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (160, 160, 160), 1)
    for i, b in enumerate(blocks):
        x0, y0, x1, y1 = R(b["bbox_pt"])
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 200, 0), 3)        # 绿（部位）
        cv2.putText(img, "P%d" % (i + 1), (x0 + 3, max(14, y0 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 0), 2)
    cv2.imwrite(png_path, img)


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    import sqlite3
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    profs = con.execute(
        "select id, drawing_key, pdf_path, page_count from v2_drawing_profiles"
        " where pdf_path!='' order by id").fetchall()

    pid = None
    pdf_arg = None
    for a in sys.argv[1:]:
        if a.startswith("--pid="):
            pid = int(a.split("=")[1])
        elif a.startswith("--pdf="):       # 【P1】直接给 PDF 路径（供 logical_blocks 调用）
            pdf_arg = a.split("=", 1)[1]

    if pdf_arg and os.path.exists(pdf_arg):
        sel = {"id": pid or 0, "drawing_key": os.path.splitext(os.path.basename(pdf_arg))[0]}
    else:
        cands = [p for p in profs if os.path.exists(p["pdf_path"])]
        if pid:
            sel = next(p for p in cands if p["id"] == pid)
        else:
            sel = random.choice(cands)      # ★ 真正随机
    pdf = pdf_arg if pdf_arg and os.path.exists(pdf_arg) else sel["pdf_path"]
    print("=" * 92)
    print("随机选中: profile %d  %s" % (sel["id"], sel["drawing_key"]))
    print("源文件   : %s" % pdf)

    # —— 安全留证：运行前哈希
    sha_before = sha256(pdf)
    print("原图 sha256 (运行前) : %s" % sha_before)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(OUT_ROOT, "separate_p%d_%s" % (sel["id"], ts))
    os.makedirs(outdir, exist_ok=True)

    # —— 只读打开，全程不写回
    doc = fitz.open(pdf)
    results = []
    for pno in range(doc.page_count):
        page = doc[pno]
        page.remove_rotation()
        W, H = page.rect.width, page.rect.height
        draws = page.get_drawings()
        items, source = load_items(page)
        img_rects = []
        try:
            for info in page.get_image_info():
                bb = info.get("bbox")
                if bb and len(bb) == 4:
                    img_rects.append(fitz.Rect(*[float(v) for v in bb[:4]]))
        except Exception:
            pass

        # —— 0) 前置剔除区（v7）：二维码格式说明 / 表格区 / 纯说明区
        zones = build_exclusion_zones(items, W, H)
        zone = zones[0]["rect"] if zones else None   # 兼容旧报告字段（qr_spec 在首位）
        dash_rects = dash_group_rects(draws, W, H)

        def in_zone(r):
            return any(_center_in(r, z["rect"]) for z in zones)

        kept, dropped = [], []
        for idx0, it in enumerate(items):
            if in_zone(it["rect"]):
                continue                      # 剔除区内容不参与任何判定
            rr = fitz.Rect(*it["rect"])
            k, why, nd, bd = qualifies(rr, draws, W, H, img_rects)
            rec = dict(it)
            rec["_i"] = idx0
            rec.update({"keep": k, "why": why, "ndraws": nd, "border_pct": round(bd, 2)})
            (kept if k else dropped).append(rec)

        # —— 1) 轮廓格子（强核 >=12 矢量）→ 8-连通 → 【空白走廊切分】
        zone_rects = [z["rect"] for z in zones]
        grid = grid_matrix(draws, W, H, zones=zone_rects, dash_rects=dash_rects)
        outlines = cells_from_grid(grid, zones=zone_rects, thr=GRID_MIN_DRAWS)
        comps = outline_components(outlines)

        # 切分用矢量集：非长直线 + 不在剔除区 + 非断续组（中心线不得跨越走廊）
        usable_rects = []
        for d in draws:
            rb = d["rect"]
            if rb.width > W * 0.5 or rb.height > H * 0.5:
                continue
            c4 = (rb.x0, rb.y0, rb.x1, rb.y1)
            if any(_center_in(c4, z["rect"]) for z in zones):
                continue
            if (round(rb.x0, 3), round(rb.y0, 3), round(rb.x1, 3), round(rb.y1, 3)) in dash_rects:
                continue
            usable_rects.append([rb.x0, rb.y0, rb.x1, rb.y1])

        # —— 1b) 【空白走廊二次切分】24pt 格子在小间隙处不断开，靠矢量级扫描再切
        n_comp_raw = len(comps)
        comps = [sub for cp in comps for sub in split_comp_by_gaps(cp, usable_rects)]

        # —— 1c) 【弱格限深生长 + 孤儿升格】正面/底座/侧视这类稀疏轮廓视图
        n_comp_strong = len(comps)
        comps, orphan_flags = weak_grow_and_orphans(comps, grid, zones=zone_rects)
        # —— 1d) 【桥线合并】底座中空两半 / 正面碎片 → 同一实体并回
        comps, orphan_flags = bridge_merge(comps, orphan_flags, usable_rects)
        paired = sorted(zip(comps, orphan_flags), key=lambda t: len(t[0]), reverse=True)
        comps = [cp for cp, _ in paired]
        orphan_flags = [fl for _, fl in paired]

        def cells_bb(cp):
            xs = []
            ys = []
            for (r, c) in cp:
                xs += [c * GRID_PT, (c + 1) * GRID_PT]
                ys += [r * GRID_PT, (r + 1) * GRID_PT]
            return [min(xs), min(ys), max(xs), max(ys)]

        def _iou(a, b):
            ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            inter = ix * iy
            if inter <= 0.0:
                return 0.0, 0.0
            aa = (a[2] - a[0]) * (a[3] - a[1])
            ba = (b[2] - b[0]) * (b[3] - b[1])
            return inter / (aa + ba - inter), inter / max(1e-9, min(aa, ba))

        seeds = []
        for cp, cp_orphan in zip(comps, orphan_flags):   # 已按格子数降序
            if len(cp) < MIN_COMP_CELLS:
                continue
            bb = cells_bb(cp)
            hit = None
            for s in seeds:
                v, contain = _iou(bb, s["bb"])
                if v >= MERGE_IOU or contain >= MERGE_CONTAIN:
                    hit = s
                    break
            if hit:
                hit["cells"] += cp             # 同一实体碎裂 → 合并
                hit["bb"] = cells_bb(hit["cells"])
                hit["orphan"] = hit["orphan"] and cp_orphan
            else:
                seeds.append({"cells": list(cp), "bb": bb, "orphan": cp_orphan})

        seed_cell_rects = []
        for s in seeds:
            seed_cell_rects.append(
                [[c * GRID_PT, r * GRID_PT, (c + 1) * GRID_PT, (r + 1) * GRID_PT]
                 for (r, c) in s["cells"]])

        def nearest_seed(rect):
            """→ (种子下标, 与该种子骨架格子的最小距离)；无种子 → (-1, inf)"""
            best_i, best_d = -1, float("inf")
            for si, rl in enumerate(seed_cell_rects):
                for cr in rl:
                    d = _rect_dist(cr, rect)
                    if d < best_d:
                        best_i, best_d = si, d
                        if best_d == 0.0:
                            return best_i, 0.0
            return best_i, best_d

        # —— 2) 每种子的部位框 = 骨架格子 ∪ 竞争归属的非长直线矢量
        #    （p56 实测：窄条边缘是稀疏长直线，seed 格子只盖中段，必须靠矢量撑框）
        def _zone_shrink(lo_cell, hi_cell, zlo, zhi, cur_lo, cur_hi):
            """按剔除区收一侧边。仅当骨架格子整体在区的一侧才动，且收缩后必须仍是合法区间。
            v6.1 事故根因：此处曾误用上一轮循环的残留变量 s，导致所有种子的 cb 都相同，
            于是任何 bb[2] > zone[0] 的部位框都被裁成 zone[0]-2（侧视/后盖/底座全被误裁）。"""
            if hi_cell <= zlo + 1.0 and cur_hi > zlo:      # 格子在区左侧
                cand = zlo - 2.0
                if cand > cur_lo:
                    return cur_lo, cand
            if lo_cell >= zhi - 1.0 and cur_lo < zhi:      # 格子在区右侧
                cand = zhi + 2.0
                if cand < cur_hi:
                    return cand, cur_hi
            return cur_lo, cur_hi

        for si in range(len(seeds)):
            rects = list(seed_cell_rects[si])
            for d in draws:
                rb = d["rect"]
                if rb.width > W * 0.5 or rb.height > H * 0.5:
                    continue
                dr = [rb.x0, rb.y0, rb.x1, rb.y1]
                if any(_center_in(dr, z["rect"]) for z in zones):
                    continue          # 剔除区内的矢量不得撑框
                si2, dd = nearest_seed(dr)
                if si2 == si and dd <= FAR_ATTACH_PT:
                    rects.append(dr)
            xs, ys = [], []
            for r in rects:
                xs += [r[0], r[2]]
                ys += [r[1], r[3]]
            bb = [min(xs), min(ys), max(xs), max(ys)]
            # —— 剔除区收边：格子整体在区一侧、而归属矢量把框撑进区时才收缩
            cb = cells_bb(seeds[si]["cells"])
            for z in zones:
                zr = z["rect"]
                bb[0], bb[2] = _zone_shrink(cb[0], cb[2], zr[0], zr[2], bb[0], bb[2])
                bb[1], bb[3] = _zone_shrink(cb[1], cb[3], zr[1], zr[3], bb[1], bb[3])
            seeds[si]["bbox"] = bb

        # —— 2b) 【v8 部位框空白走廊精修】见 XY_* 常量注释。只切「两侧都够料」的
        #         真走廊；窄碎片并回相邻块；切完做紧贴合并（顶视底部窄条会经此并回）。
        def _rel_rects(bb):
            return [r for r in usable_rects
                    if not (r[2] < bb[0] or r[0] > bb[2] or
                            r[3] < bb[1] or r[1] > bb[3])]

        def _xy_bands(bb, axis):
            lo, hi = (bb[1], bb[3]) if axis == "y" else (bb[0], bb[2])
            span = hi - lo
            if span < 3 * XY_GAP:
                return []
            rel = _rel_rects(bb)
            step = 2.0
            n = max(1, int(span / step) + 1)
            cross = [0] * n
            for r in rel:
                a0, a1 = (r[1], r[3]) if axis == "y" else (r[0], r[2])
                # ⭐ v8.2：实墙判据看【另一轴】跨度 —— p52 实证 y=341 零厚横线
                # (x 跨度 141.8pt) 横躺在 P4|P7 切分带上，旧逻辑按切割轴跨度
                # (0<XY_SPAN) 跳过 → 带被误判空白 → 共享边框的同一部位被切开。
                b0, b1 = (r[0], r[2]) if axis == "y" else (r[1], r[3])
                # 任一轴跨度 >=XY_SPAN 即视为实体：纵贯走廊的长竖线（另一轴=0）
                # 与横躺在带上的长横线（切割轴=0）都必须拦下切分；小刻度点才跳过
                if max(a1 - a0, b1 - b0) < XY_SPAN:
                    continue
                # 带内占据判定：切割轴区间 ±0.5pt 膨胀（零厚线也要命中）
                i0 = max(0, int((a0 - 0.5 - lo) / step))
                i1 = min(n - 1, int((a1 + 0.5 - lo) / step))
                if i1 < i0:
                    i1 = i0
                for i in range(i0, i1 + 1):
                    cross[i] += 1
            bands = []
            i = 0
            while i < n:
                if cross[i] == 0:
                    j = i
                    while j < n and cross[j] == 0:
                        j += 1
                    a, b = lo + i * step, lo + j * step
                    if b - a >= XY_GAP and a > lo + XY_MARGIN * span \
                            and b < hi - XY_MARGIN * span:
                        bands.append((a, b))
                    i = j
                else:
                    i += 1
            return bands

        def _refine(bb, depth=0):
            if depth >= XY_MAX_DEPTH:
                return [bb]
            for axis in ("x", "y"):
                bands = _xy_bands(bb, axis)
                if not bands:
                    continue
                mids = [(a + b) / 2.0 for a, b in bands]
                lo_edge = bb[1] if axis == "y" else bb[0]
                hi_edge = bb[3] if axis == "y" else bb[2]
                edges = [lo_edge] + mids + [hi_edge]
                segs = []
                for i in range(len(edges) - 1):
                    segs.append([bb[0], edges[i], bb[2], edges[i + 1]] if axis == "y"
                                else [edges[i], bb[1], edges[i + 1], bb[3]])
                counts = [sum(1 for r in usable_rects
                              if not (r[2] < sg[0] or r[0] > sg[2] or
                                      r[3] < sg[1] or r[1] > sg[3])) for sg in segs]
                good = [i for i, c in enumerate(counts) if c >= XY_MIN_DRAWS]
                if len(good) < 2:
                    continue          # 切不出两个实块 = 部位内部空洞，不切
                hi_i = 2 if axis == "x" else 3     # 碎片并回相邻合格段
                lo_i = 0 if axis == "x" else 1
                merged, lead = [], None
                for i, sg in enumerate(segs):
                    if i in good:
                        blk = [list(sg), counts[i]]
                        if lead is not None:
                            blk[0][lo_i] = lead[lo_i]
                            lead = None
                        merged.append(blk)
                    elif merged:
                        merged[-1][0][hi_i] = sg[hi_i]
                        merged[-1][1] += counts[i]
                    else:
                        lead = list(sg)
                if len(merged) < 2:
                    continue
                out = []
                for blk, _c in merged:
                    out += _refine(blk, depth + 1)
                return out
            return [bb]

        refined = []
        if not XY_REFINE:
            refined = [{"cells": s["cells"], "bb": cells_bb(s["cells"]),
                        "bbox": s["bbox"], "orphan": s.get("orphan", False)} for s in seeds]
        else:
            for s in seeds:
                for rbb in _refine(s["bbox"]):
                    inner = [c for c in s["cells"]
                             if _center_in([c[1] * GRID_PT, c[0] * GRID_PT,
                                            (c[1] + 1) * GRID_PT, (c[0] + 1) * GRID_PT], rbb)]
                    nd = sum(1 for r in usable_rects
                             if not (r[2] < rbb[0] or r[0] > rbb[2] or
                                     r[3] < rbb[1] or r[1] > rbb[3]))
                    refined.append({"cells": inner or list(s["cells"]), "bb": cells_bb(inner)
                                    if inner else cells_bb(s["cells"]),
                                    "bbox": rbb, "orphan": s.get("orphan", False),
                                    "ndraws": nd})
        # 精修块合并：仅当小框被大框深度包含（contain>=0.5）→ 同一部位被走廊
        # 切碎的碎片（顶视底部条并回顶视主体）。⚠️ 不能用「相交即并」：
        # 正面|侧视沿切分线 x=272.65 贴着走，相交即并会退回合并前。
        changed = True
        while changed:
            changed = False
            out = []
            while refined:
                cur = refined.pop(0)
                hit = None
                for j, o in enumerate(refined):
                    a, b = cur["bbox"], o["bbox"]
                    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
                    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
                    inter = ix * iy
                    if inter <= 0:
                        continue
                    aa = (a[2] - a[0]) * (a[3] - a[1])
                    ba = (b[2] - b[0]) * (b[3] - b[1])
                    if inter / max(1e-9, min(aa, ba)) >= MERGE_REFINE_CONTAIN:
                        hit = j
                        break
                if hit is None:
                    out.append(cur)
                else:
                    o = refined.pop(hit)
                    nb = [min(cur["bbox"][0], o["bbox"][0]), min(cur["bbox"][1], o["bbox"][1]),
                          max(cur["bbox"][2], o["bbox"][2]), max(cur["bbox"][3], o["bbox"][3])]
                    nd = sum(1 for r in usable_rects
                             if not (r[2] < nb[0] or r[0] > nb[2] or
                                     r[3] < nb[1] or r[1] > nb[3]))
                    refined.insert(0, {"cells": cur["cells"] + o["cells"], "bb": cells_bb(cur["cells"] + o["cells"]),
                                       "bbox": nb, "orphan": cur["orphan"] and o["orphan"],
                                       "ndraws": nd})
                    changed = True
            refined = out
        seeds = refined

        from vpdf import blocks as B
        from vpdf import normalize as N

        def sem_check(bb_norm, texts, n_img=0, area_pct=None):
            """二级文本语义补刀（本地收紧版）"""
            # ⚠️ 不直接用 B._block_is_signblock：其「日期+人名」启发式会把产品视图误杀为会签栏
            def signblock_safe(ts):
                joined = " ".join(ts)
                if any(d in joined for d in B.SIGNBLOCK_DEPTS):
                    return True
                ns = joined.replace(" ", "")
                if B.DATE_RE.search(ns) and any(
                        h in joined for h in ("标准化", "会签", "审核", "批准", "校对", "设计",
                                              "更改单号", "文件编号", "物料编码")):
                    return True
                return False

            def titlebar_safe(bn, ts):
                if any(B.TITLE_NAME_RE.search(t) for t in ts):
                    return True
                # CODE_RE 单独不再成指纹：打标值（QHR45/200512）同样形似图号，
                # 必须与标题栏词/位置互相印证（p62 实证：顶部面板视图因 QHR45 被误杀）
                code_hit = any(B.CODE_RE.match(t.strip()) for t in ts)
                hits = sum(1 for t in ts if t in B.TITLEBLOCK_WORD_HINTS)
                x0, y0, x1, y1 = bn
                h_ratio = (y1 - y0)
                w_ratio = (x1 - x0)
                narrow_edge = h_ratio > 0.80 and w_ratio < 0.25 and ((x0 < 0.06) or (x1 > 0.94))
                if code_hit and (hits >= 1 or y0 > 0.72 or narrow_edge):
                    return True
                if hits >= 4:
                    return True
                if y0 > 0.72 and hits >= 2:
                    return True
                return False

            sem = []
            if titlebar_safe(bb_norm, texts):
                sem.append("titlebar")
            if signblock_safe(texts):
                sem.append("signblock")
            # 贴纸/铭牌版式区（用户拍板：非产品部位一律舍弃）——p67 实证
            # 「后盖铭牌贴纸内容」是贴纸物件设计稿，非产品视图
            joined_all = " ".join(texts)
            if "贴纸" in joined_all and ("内容" in joined_all or "版式" in joined_all):
                sem.append("sticker_spec")
            # 二维码格式说明/放大示意（用户既定舍弃清单第 4 类变体）：独立短标签
            # + 二维码图案、无产品轮廓 —— p61 实证「二维码放大图片」0.8% 小块
            # （v6 剔除区指纹需字段标签≥2，此变体无字段故漏网）。
            # 三重防误杀豁免：①块内含嵌入图像（p50/p52 面板部位含「下盖此处激光
            # 打标二维码」但 img=1，基线扫描全库仅此两处含关键词）；②任一文本含
            # ≥4 位连续数字（打标内容必伴流水号/型号）；③面积 >=3%（真部位最小
            # 实测 p56 侧视 5.9%、p61 P6 仅 0.8%）。
            if texts and n_img == 0 and area_pct is not None and area_pct < 3.0 \
                    and all(("二维码" in t or "放大" in t) for t in texts) \
                    and not any(re.search(r"\d{4,}", t) for t in texts):
                sem.append("qr_note")
            if B._block_is_techreq(texts):
                sem.append("techreq")
            # 纯说明句块（技术要求/说明文字）：几何一级常把「技术要求」表头丢掉，
            # _block_is_techreq 失去表头便失效 → 按「纯中文说明句占比」补判。
            # ⚠️ 不能用 _looks_marking 反证（指令句含「服务热线」会被误判为打标内容）。
            pure = [t for t in texts if B._is_pure_explanation(t)]
            if (len(pure) >= 3 and len(pure) >= 0.6 * len(texts)) or \
                    (len(texts) >= 2 and len(pure) == len(texts)):
                sem.append("techreq_pure")
            return sem

        blocks, rejected, skipped = [], [], []
        # —— 3) 部位 = 种子；成员 = item 中心落在部位 bbox 内（含被几何丢弃的项）
        member_of = {}
        for si, s in enumerate(seeds):
            bb = s["bbox"]
            for idx, it in enumerate(items):
                if in_zone(it["rect"]):
                    continue
                if _center_in(it["rect"], bb):
                    prev = member_of.get(idx)
                    if prev is None:
                        member_of[idx] = si
                    else:
                        # 极少见的 bbox 重叠：归最近者
                        d0 = _rect_dist([it["rect"][0], it["rect"][1], it["rect"][2], it["rect"][3]], seeds[prev]["bbox"])
                        d1 = _rect_dist([it["rect"][0], it["rect"][1], it["rect"][2], it["rect"][3]], bb)
                        if d1 < d0:
                            member_of[idx] = si

        for si, s in enumerate(seeds):
            bb = s["bbox"]
            # —— v8 最小部位尺寸门槛：短边 <MIN_PART_SIDE 或面积 <MIN_PART_AREA
            #    = 文字/logo 碎块（p56 实证 Shintech 公司名 25.6×72pt 短边 25.6），
            #    真值部位最短边实测 p62 P4=55.3 / p67 侧视=68 / p56 全部 >100。
            short = min(bb[2] - bb[0], bb[3] - bb[1])
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            if short < MIN_PART_SIDE or area < MIN_PART_AREA:
                skipped.append({"bbox": [round(v, 2) for v in bb],
                                "reason": "too_small",
                                "short": round(short, 1), "area": round(area, 1)})
                continue
            members = [items[i] for i, owner in member_of.items() if owner == si]
            if not members and len(s["cells"]) < ITEMLESS_MIN_CELLS \
                    and s.get("ndraws", 0) < XY_MIN_DRAWS:
                # v8：精修切出的段自带 >=XY_MIN_DRAWS 矢量背书（s["ndraws"]），
                # 不得按 no_item 丢弃 —— p62 实证：面板底部段被切出后被误杀，
                # 部位底部 18pt 内容丢失且邻位 P3 顶部被掏空。
                skipped.append({"bbox": [round(v, 2) for v in bb],
                                "reason": "no_item", "cells": len(s["cells"])})
                continue        # 格子少又无内容 = 标注碎片，防幽灵框
            # 孤儿升格种子无强核背书 → 必须自证部位级矢量密度（p67 实证：
            # 侧视条 147 条 ✔；走廊尺寸簇仅 44~80 条 ✘）
            if s.get("orphan"):
                nv = sum(1 for r in usable_rects if _center_in(r, bb))
                if nv < ORPHAN_MIN_DRAWS:
                    skipped.append({"bbox": [round(v, 2) for v in bb],
                                    "reason": "orphan_sparse", "cells": len(s["cells"]),
                                    "n_draws": nv})
                    continue
            # 无 item 但格子足够多（>=ITEMLESS_MIN_CELLS）→ 纯图形部位：
            # p62 实证：面板/侧视图的打标文本与尺寸标注全部「转曲」，文本层为空，
            # 但 45/7 个密集轮廓格子就是产品视图本身。
            bb_norm = [float(v) for v in N.norm_bbox(bb, W, H)]
            texts = [x["text"] for x in members if x["kind"] == "text" and x["text"]]
            sem = sem_check(bb_norm, texts,
                            n_img=sum(1 for x in members if x["kind"] == "image"),
                            area_pct=100.0 * (bb[2] - bb[0]) * (bb[3] - bb[1]) / (W * H))
            if sem:
                rejected.append({"bbox": [round(v, 2) for v in bb],
                                 "semantic": sem, "texts": texts[:12]})
                continue
            blocks.append({
                "idx": len(blocks),
                "bbox_pt": [round(v, 2) for v in bb],
                "bbox_norm": [round(bb[0] / W, 6), round(bb[1] / H, 6),
                              round((bb[2] - bb[0]) / W, 6), round((bb[3] - bb[1]) / H, 6)],
                "area_pct": round(100.0 * (bb[2] - bb[0]) * (bb[3] - bb[1]) / (W * H), 2),
                "n_items": len(members),
                "n_text": sum(1 for x in members if x["kind"] == "text" and x["text"]),
                "n_image": sum(1 for x in members if x["kind"] == "image"),
                "n_outline_cells": len(s["cells"]),
                "texts": texts[:40],
            })

        # —— 4) 松散残留：不被任何部位包含、又通过了几何一级的 item → 旧聚类兜底
        leftover = [dict(it) for it in kept if it["_i"] not in member_of]
        n_loose_blocks = 0
        for g in cluster(leftover, THR_PT):
            mem = [leftover[i] for i in g]
            gi_items = [x for x in mem if x["kind"] != "outline"]
            if not gi_items:
                continue
            n_img = sum(1 for x in gi_items if x["kind"] == "image")
            xs, ys = [], []
            for x in mem:
                xs += [x["rect"][0], x["rect"][2]]
                ys += [x["rect"][1], x["rect"][3]]
            bbox = [min(xs), min(ys), max(xs), max(ys)]
            bb_norm = [float(v) for v in N.norm_bbox(bbox, W, H)]
            texts = [x["text"] for x in gi_items if x["text"]]
            sem = sem_check(bb_norm, texts, n_img=n_img,
                            area_pct=100.0 * (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / (W * H))
            has_support = (n_img > 0) or \
                          (len(gi_items) >= 2 and
                           100.0 * (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / (W * H) >= 0.10)
            if sem or not has_support:
                rejected.append({"bbox": [round(v, 2) for v in bbox],
                                 "semantic": sem or ["no_support"], "texts": texts[:12]})
                continue
            blocks.append({
                "idx": len(blocks),
                "bbox_pt": [round(v, 2) for v in bbox],
                "bbox_norm": [round(bbox[0] / W, 6), round(bbox[1] / H, 6),
                              round((bbox[2] - bbox[0]) / W, 6), round((bbox[3] - bbox[1]) / H, 6)],
                "area_pct": round(100.0 * (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / (W * H), 2),
                "n_items": len(gi_items),
                "n_text": sum(1 for x in gi_items if x["kind"] == "text" and x["text"]),
                "n_image": n_img, "n_outline_cells": 0,
                "texts": texts[:40],
            })
            n_loose_blocks += 1

        # —— 3b) 语义拒块碎片过滤：与任一语义拒块 bbox 重叠 >=50% 的块 =
        #         被拒实体（贴纸/标题栏…）的内部残片（p67 实证：贴纸版式区被
        #         走廊切分劈成两块，无文字的下半块逃过语义检查 → 幽灵框）
        def _ovr(a, b):
            ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            aa = (a[2] - a[0]) * (a[3] - a[1])
            return (ix * iy / aa) if aa > 0 else 0.0

        blocks = [b for b in blocks
                  if not any(_ovr(b["bbox_pt"], rj["bbox"]) >= 0.5
                             for rj in rejected)]

        # —— 3c) 成对 bbox 重叠让位：两部位终框相交时，格子多者沿重叠主轴收缩
        #   p67 实证：顶视（弱格生长+撑框后 y1=179.4）压住正面顶边 139.6；
        #   PCB(52格) 压住下视顶带 288~312。让位后顶视 y1→137.6、PCB y1→286。
        for _ in range(len(blocks)):
            changed = False
            for i in range(len(blocks)):
                for j in range(i + 1, len(blocks)):
                    a, b = blocks[i]["bbox_pt"], blocks[j]["bbox_pt"]
                    ix = min(a[2], b[2]) - max(a[0], b[0])
                    iy = min(a[3], b[3]) - max(a[1], b[1])
                    if ix <= 0 or iy <= 0:
                        continue
                    aa = (a[2] - a[0]) * (a[3] - a[1])
                    ab = (b[2] - b[0]) * (b[3] - b[1])
                    if ix * iy < 0.04 * min(aa, ab):
                        continue          # 轻微擦边不处理
                    src, dst = (blocks[i], blocks[j]) \
                        if blocks[i]["n_outline_cells"] >= blocks[j]["n_outline_cells"] \
                        else (blocks[j], blocks[i])
                    s, d = src["bbox_pt"], dst["bbox_pt"]
                    # 沿【重叠较小】的轴让位：小重叠轴收缩即可分离两框；
                    # 大重叠轴通常是包含关系（如大框 y 向罩住竖条），收缩无效。
                    # p50 实证：P0(下盖背视) x 向只压侧视 25.6pt，y 向重叠 216pt ——
                    #   旧判据 iy>=ix 走纵向收缩永远无法分离；改沿 x 收左缘 363.6 即分离。
                    if iy <= ix:          # y 重叠小 → 纵向让位
                        if (s[1] + s[3]) <= (d[1] + d[3]):   # src 在上 → 收下缘
                            cand = d[1] - 2.0
                            if cand - s[1] >= 0.6 * (s[3] - s[1]) and cand < s[3]:
                                s[3] = cand
                                changed = True
                        else:                                  # src 在下 → 收上缘
                            cand = d[3] + 2.0
                            if s[3] - cand >= 0.6 * (s[3] - s[1]) and cand > s[1]:
                                s[1] = cand
                                changed = True
                    else:                 # x 重叠小 → 横向让位
                        if (s[0] + s[2]) <= (d[0] + d[2]):   # src 在左 → 收右缘
                            cand = d[0] - 2.0
                            if cand - s[0] >= 0.6 * (s[2] - s[0]) and cand < s[2]:
                                s[2] = cand
                                changed = True
                        else:                                  # src 在右 → 收左缘
                            cand = d[2] + 2.0
                            if s[2] - cand >= 0.6 * (s[2] - s[0]) and cand > s[0]:
                                s[0] = cand
                                changed = True
            if not changed:
                break
        blocks.sort(key=lambda z: -z["area_pct"])
        for i, b in enumerate(blocks):
            b["idx"] = i

        # —— 可视化 + 每个部位单独裁图
        pix = page.get_pixmap(dpi=140)
        viz = os.path.join(outdir, "p%d_overview.png" % pno)
        draw_viz(viz, pix, W, H, blocks, dropped, img_rects, rejected,
                 zones=[z["rect"] for z in zones] if zones else None, skipped=skipped)

        for b in blocks:
            m = 8
            r = fitz.Rect(max(0, b["bbox_pt"][0] - m), max(0, b["bbox_pt"][1] - m),
                          min(W, b["bbox_pt"][2] + m), min(H, b["bbox_pt"][3] + m))
            try:
                cp = page.get_pixmap(dpi=200, clip=r)
                cp.save(os.path.join(outdir, "p%d_part_%02d.png" % (pno, b["idx"] + 1)))
            except Exception as e:
                print("  [warn] 裁剪失败 P%d: %s" % (b["idx"] + 1, e))

        results.append({"page_index": pno, "width": round(W, 2), "height": round(H, 2),
                        "source": source, "n_drawings": len(draws),
                        "n_items": len(items), "n_kept": len(kept), "n_dropped": len(dropped),
                        "n_components": len(comps), "n_seeds": len(seeds),
                        "n_dash_group_lines": len(dash_rects),
                        "exclusion_zones": [{"kind": z["kind"],
                                             "rect": [round(v, 2) for v in z["rect"]]}
                                            for z in zones],
                        "qr_spec_zone": [round(v, 2) for v in zone] if zone else None,
                        "skipped_seeds": skipped,
                        "n_blocks_final": len(blocks), "n_loose_blocks": n_loose_blocks,
                        "semantic_rejected": rejected,
                        "blocks": blocks})
        print("  第%d页: items=%d 组件=%d 种子=%d(去重后) → 部位=%d(松散=%d) 语义拒=%d 跳过=%d%s"
              % (pno, len(items), len(comps), len(seeds), len(blocks), n_loose_blocks,
                 len(rejected), len(skipped),
                 ("  [剔除区:%s]" % ",".join(z["kind"] for z in zones)) if zones else ""))
    doc.close()      # ★ 只读关闭，未做任何保存

    # —— 安全留证：运行后哈希比对
    sha_after = sha256(pdf)
    intact = (sha_before == sha_after)
    print("原图 sha256 (运行后) : %s" % sha_after)
    print("源文件未被修改       : %s" % ("✔ 是（前后哈希一致）" if intact else "✘ 否！"))

    report = {
        "profile_id": sel["id"], "drawing_key": sel["drawing_key"],
        "source_pdf": pdf,
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_intact": intact,
        "rules": {"T_DRAWS": T_DRAWS, "T_BORDER": T_BORDER, "THR_PT": THR_PT,
                  "VISION_DPI": VISION_DPI, "GRID_PT": GRID_PT,
                  "GRID_MIN_DRAWS": GRID_MIN_DRAWS, "MIN_COMP_CELLS": MIN_COMP_CELLS,
                  "FAR_ATTACH_PT": FAR_ATTACH_PT, "MERGE_IOU": MERGE_IOU,
                  "MERGE_CONTAIN": MERGE_CONTAIN},
        "pages": results,
    }
    rp = os.path.join(outdir, "report.json")
    with io.open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n产物目录: %s" % outdir)
    print("  report.json / p0_overview.png / p0_part_XX.png ...")
    print("REPORT=%s" % rp)


if __name__ == "__main__":
    main()
