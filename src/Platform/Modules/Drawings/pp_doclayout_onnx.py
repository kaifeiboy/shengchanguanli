#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pp_doclayout_onnx.py - PP-DocLayoutV3 版面检测（ONNX Runtime 运行时）
====================================================================

为什么是 ONNX：
  本机 CPU 为 Intel i5-2430M (Sandy Bridge, 2011)，仅支持到 AVX、无 AVX2。
  PaddlePaddle 3.x 的 Inference 预测器（create_predictor / static.Executor）
  在 JIT 编译推理内核时发射 AVX2 指令 → 确定性 0xC0000005 崩溃，无法修复。
  而 PP-DocLayoutV3 模型本身（PaddleOCR 官方）是用户指定的切图模型。

  解法：用 ONNX Runtime 跑【同一个 PP-DocLayoutV3 模型】（从 PaddleOCR 官方模型
  经 Paddle2ONNX 转换，结构/权重一致），绕开 Paddle 的 AVX2 JIT。onnxruntime
  在本机 AVX-only CPU 上经运行时指令分发正常工作（RapidOCR 实证）。

预处理（与 PaddleOCR-VL PP-DocLayoutV3 完全一致，已用 4K3GR ground truth 实证）：
  Resize 800x800 (keep_ratio=False, BICUBIC) → /255（无 ImageNet 归一化）→ CHW。

输出：模型导出时已将 DETR 后处理（NMS + 解码 + 坐标映射）融进图，
  输出为 (N,7) = [cls_id, score, x1, y1, x2, y2, order]，坐标已映射回原图空间。
"""
import os
import sys
import re
import numpy as np

# ── 模型路径解析（优先相对项目 data/models，回退绝对路径）──
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_CANDIDATES = [
    os.path.normpath(os.path.join(_HERE, "..", "..", "..", "data", "models", "PP-DocLayoutV3.onnx")),
    "E:/workaaa/shengchanguanli/data/models/PP-DocLayoutV3.onnx",
]
MODEL_PATH = None
for _c in _MODEL_CANDIDATES:
    if os.path.exists(_c):
        MODEL_PATH = _c
        break

# PP-DocLayoutV3 label_list（0-based 索引）
LABELS = ["abstract", "algorithm", "aside_text", "chart", "content", "display_formula",
          "doc_title", "figure_title", "footer", "footer_image", "footnote",
          "formula_number", "header", "header_image", "image", "inline_formula",
          "number", "paragraph_title", "reference", "reference_content", "seal",
          "table", "text", "vertical_text", "vision_footnote"]

# 保留的版式标签：产品部件视图（image）+ 图/示意图（chart）
KEEP_CLS = {14: "image", 3: "chart"}

# 舍弃规则阈值（与 segment_blocks_auto.py 原规则一致）
# 2026-07-24 微调：原 0.60 会把 YCWA15NCBQ 等图纸上 score≈0.45-0.55 的
# 真实工程视图（面积/宽高比正常、不含二维码）误杀。0.453 与下一候选 0.360
# 之间存在明显空档，降到 0.43 可精准保留真工程图而不带回噪声（0.36 仍丢弃）。
MIN_SCORE = 0.34        # 放宽：保留 VK01 #2 等 0.352 真实低分视图；下游几何/二维码/面积门控兜底噪点
MIN_AREA_RATIO = 0.003  # 面积占整页 < 0.3% 极小碎块丢弃
MAX_AREA_RATIO = 0.40   # 面积占整页 > 40% 整页轮廓误检丢弃
MAX_ASPECT_RATIO = 10.0 # 长边/短边 > 10 的极端扁长/细高块丢弃（非工程部件视图）

# 二维码过滤：含二维码且面积 ≤ 此阈值的块视为说明/推广内容（非工程部件视图）
# 理由：PP-DocLayoutV3 把二维码区域也归为 image 类（图形），但二维码不是产品部件
QR_CODE_MAX_AREA_RATIO = 0.025  # 占整页面积 ≤ 2.5% 的二维码块直接舍弃（兜住纯二维码小图）

# 尺寸文字过滤"面积门控"：仅当块占整页面积 < 此阈值时才用"无尺寸数字"判丢弃。
# 大块工程视图即使 OCR 漏检数字也保留（防误杀 VK01 #6 这类真视图）；
# 小块（Logo/图标/警示图）仍按原逻辑判定，保留对品牌 Logo 的舍弃。
DIM_NO_DIGIT_MAX_AREA_RATIO = 0.05

# ───────────────────────────────────────────────────────────────────────────
# 舍弃规则阈值（补充 · 集中管理）
# 设计哲学：ONNX 模型已能区分版面中的「独立图块」（与 PaddleOCR-VL 输出一致），
# 后期处理【只做舍弃、不做合并】。凡低于置信/尺寸/语义门槛的候选一律舍弃；
# 仅「被过度覆盖大框吞并的精确子视图」作为例外被保留（大框本身被舍弃，不重新合并）。
# ───────────────────────────────────────────────────────────────────────────

# 1) 分数门槛
RAW_SCORE_FLOOR = 0.12   # 原始模型输出分数下限：低于此视为纯噪声，直接舍弃（不进任何后续规则）

# 5) 过度覆盖（大框吞并多独立视图）舍弃规则
#    判定：某大框内含 ≥ MIN_CHILDREN 个互不包含的独立子视图，且其并集占该大框
#    面积 ≥ UNION_RATIO → 该大框为"合并伪检"，整框舍弃，子视图保留（不合并）。
OVERCOVER_MIN_CHILDREN   = 2      # 至少几个独立子视图才判为过度覆盖
OVERCOVER_CHILD_CONTAIN  = 0.85   # 子视图至少有 85% 落在大框内才算"被吞并"
OVERCOVER_UNION_RATIO    = 0.60   # 子视图并集 / 大框面积 阈值
OVERCOVER_CHILD_MIN_AREA = 0.004  # 子视图自身最小面积占页比（过滤微型噪点子框）
OVERCOVER_DISTINCT_IOU   = 0.30   # 子视图间 IoU 上限（互不包含才算独立）

# 6) 子检测冗余舍弃（去重，非合并）：块 ≥ DROP_CONTAIN 落在另一保留块内 → 舍弃内层块。
SUBDET_DROP_CONTAIN = 0.70

# 7) 高置信工程图保护：模型 score ≥ 此阈值且含任意长线 → 视为工程视图保留。
#    用于 OCR 漏检数字的高置信真视图（P1HVQ 顶/侧视图、YKWA17 主面板等，score 普遍
#    ≥0.85）；Logo 放大图/标签块 score 多 ≈0.65 < 0.85，不命中 → 被舍弃。
SCORE_KEEP_ENGINEERING = 0.85

# 8) 块内分裂（Split）：模型偶发将两个独立视图合并为单框输出（无更细子框可 rescue）。
#    对保留块做内部结构边界检测——全宽/全高空白带 或 贯穿性细密分隔线——若存在则沿
#    边界一分为二。这是"反合并"操作，与"只舍弃不合并"目标一致（保证输出为独立图块）；
#    它不合并任何已区分的块，只拆分模型过合并的单框。子块仍须过面积/宽高比校验。
SPLIT_MIN_DIM        = 120    # 块短边 < 此值不尝试分裂
SPLIT_GAP_EPS        = 0.006  # 空白带密度阈值（列/行墨迹占比 < 此视为空白）
SPLIT_GAP_MIN_RATIO  = 0.04   # 空白带最小宽度占该方向尺寸
SPLIT_SIDE_INK_MIN   = 0.020  # 分裂后每侧平均墨迹密度下限（防一侧纯空白）
SPLIT_LINE_DENSITY   = 0.20   # 分隔线密度阈值（须够密，防误把尺寸线当分裂边界）
SPLIT_LINE_MAX_W     = 0.04   # 分隔线最大宽度占该方向尺寸
SPLIT_LINE_SPAN_MIN  = 0.85   # 分隔线贯穿率下限（视图边界必须贯穿整块，尺寸线仅局部）
SPLIT_SUB_MIN_DIM    = 60     # 子块最小边长(px)

_SESS = None


def _get_session():
    """懒加载并缓存 ONNX 会话（单进程只加载一次）。

    确定性推理：单线程 + 顺序执行，避免 ONNX Runtime 多线程浮点求和顺序
    导致阈值边界块（score≈MIN_SCORE）跨次运行抖动。这是"方案成熟、可弃用
    v6 红框保底"的硬前提——同一张图必须每次得到完全相同的切图结果。
    """
    global _SESS
    if _SESS is None:
        if MODEL_PATH is None:
            raise RuntimeError("PP-DocLayoutV3.onnx 未找到，请确认 data/models/ 下存在该文件")
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _SESS = ort.InferenceSession(
            MODEL_PATH, sess_options=so, providers=["CPUExecutionProvider"])
    return _SESS


def is_available():
    """ONNX 运行时与模型是否可用。"""
    if MODEL_PATH is None:
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _qr_finder_count(gray):
    """统计二值图中符合 QR 定位图案(7:5:3 嵌套同心方块)的数量。

    QR 码固定含 3 个定位图案（左上/右上/左下），每个为「黑方框→白环→黑中心」
    的 3 级嵌套同心方块（模块比 ≈7:5:3）。用轮廓层级(RETR_TREE)检测这种严格
    嵌套结构，比 OpenCV 整图解码更鲁棒——对低分辨率、点阵渲染、轻微失真的 QR
    仍能识别，且不依赖解码出具体内容。

    返回找到的定位图案个数（>=3 基本可判定为 QR 码）。
    """
    try:
        import cv2
    except Exception:
        return 0
    h, w = gray.shape
    if min(h, w) < 80:  # 小块上采样，使定位图案可达检测尺度
        gray = cv2.resize(gray, (w * 2, h * 2))
    _, binv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, hierarchy = cv2.findContours(binv, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None or len(contours) < 1:
        return 0
    hierarchy = hierarchy[0]
    cnts = contours
    count = 0
    for i, c in enumerate(cnts):
        child = hierarchy[i][2]
        if child < 0:
            continue
        grand = hierarchy[child][2]
        if grand < 0:
            continue
        x0, y0, w0, h0 = cv2.boundingRect(c)
        x1, y1, w1, h1 = cv2.boundingRect(cnts[child])
        x2, y2, w2, h2 = cv2.boundingRect(cnts[grand])
        # 必须严格嵌套（子整体位于父内）
        if not (x0 <= x1 and y0 <= y1 and x0 + w0 >= x1 + w1 and y0 + h0 >= y1 + h1):
            continue
        if not (x1 <= x2 and y1 <= y2 and x1 + w1 >= x2 + w2 and y1 + h1 >= y2 + h2):
            continue
        r1 = w1 / max(1, w0)  # 子/父 宽比 ≈0.71
        r2 = w2 / max(1, w1)  # 孙/子 宽比 ≈0.60
        if 0.45 < r1 < 0.85 and 0.35 < r2 < 0.78:
            if (max(w0, h0) / max(1, min(w0, h0)) < 1.7 and
                    max(w2, h2) / max(1, min(w2, h2)) < 1.7):
                count += 1
    return count


def _is_qr_code_dominant(pil_image, x0, y0, x1, y1, page_area):
    """检测裁剪区域内是否包含二维码。

    双保险：
      1) OpenCV QRCodeDetector 解码（对清晰 QR 有效）；
      2) 定位图案几何检测 _qr_finder_count（对低分辨率点阵 QR / 解码失败更鲁棒）。
    仅当块面积占整页 ≤ QR_CODE_MAX_AREA_RATIO 时才判定为「应舍弃的纯二维码小图」，
    避免误弃含 QR 的大工程视图（如 #0/#3 内嵌镭雕标识区）。

    返回 True 表示该块含二维码且面积足够小，应作为说明/推广内容舍弃。
    """
    try:
        import cv2
    except Exception:
        return False
    # 面积门控：大块（含 QR 的工程视图）直接保留，不做 QR 判定
    block_area = (x1 - x0) * (y1 - y0)
    ratio = block_area / page_area if page_area > 0 else 0
    if ratio > QR_CODE_MAX_AREA_RATIO:
        return False
    # 裁剪候选区域
    crop = pil_image.crop((x0, y0, x1, y1))
    arr = np.asarray(crop.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    # 1) OpenCV 解码（仅当成功解出内容才信）
    try:
        detector = cv2.QRCodeDetector()
        ret = detector.detectAndDecode(gray)
        data = ret[0] if isinstance(ret, (list, tuple)) else ret
        points = ret[1] if isinstance(ret, (list, tuple)) and len(ret) > 1 else None
        if data and points is not None:
            return True
    except Exception:
        pass
    # 2) 定位图案几何检测（解码失败时的兜底）
    if _qr_finder_count(gray) >= 3:
        return True
    return False


# 强尺寸信号：公差(11±0.5)、直径/半径符号(⌀/Ø/φ/R/直径/半径) —— 明确工程尺寸标线
_DIM_STRONG_RE = re.compile(
    r'\d+\s*[±]\s*\d+'              # 公差：11±0.5
    r'|(?:⌀|Ø|φ|Φ|直径|半径|R\s*\d)'  # 直径/半径符号
)
# 弱尺寸信号：孤立短纯数字（16 / 8 / 39.4 / 175），用于无公差但仍有多尺寸标线的视图
# 刻意排除长字母数字混合编码（BFF0001/200512 等——标签流水号，非尺寸标线）
_DIM_WEAK_RE = re.compile(r'(?:^|[^0-9A-Za-z])\d{1,4}(?:\.\d+)?(?:[^0-9A-Za-z]|$)')


def _long_line_counts(pil_image_crop):
    """统计裁剪区中「长直线段」数量（横 h、竖 v 各计）。

    长线定义：长度 ≥ 0.35×块长边（接近块边长的尺寸线/轮廓线）。去除外围 4% 薄边，
    避免装饰边框/扫描边干扰。返回 (hl, vl)。
    """
    try:
        import cv2
    except Exception:
        return (0, 0)
    try:
        arr = np.asarray(pil_image_crop.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        m = int(min(h, w) * 0.04)            # 去外围薄边，避免装饰边框/扫描边干扰
        if h - 2 * m > 0 and w - 2 * m > 0:
            gray = gray[m:h - m, m:w - m]
        _, bin_img = cv2.threshold(gray, 0, 255,
                                   cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        edges = cv2.Canny(bin_img, 50, 150, apertureSize=3)
        min_len = max(40.0, 0.35 * max(h, w))
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=15,
                                minLineLength=int(min_len), maxLineGap=20)
        hl = vl = 0
        if lines is not None:
            for ln in lines:
                coords = ln[0] if getattr(ln, "ndim", 0) == 2 else ln
                x1, y1, x2, y2 = coords
                dx, dy = x2 - x1, y2 - y1
                length = (dx * dx + dy * dy) ** 0.5
                if length < min_len:
                    continue
                ang = abs(np.arctan2(dy, dx))
                if ang < 0.26 or ang > 2.88:       # 近似水平 (±15°)
                    hl += 1
                elif abs(ang - np.pi / 2) < 0.26:  # 近似垂直
                    vl += 1
        return hl, vl
    except Exception:
        return (0, 0)


def _has_drawing_primitives(pil_image_crop):
    """工程图线框特征：横竖双向长直线段各 ≥1（产品轮廓/尺寸线）。

    返回 True 表示具备工程图线框（OCR 漏检尺寸时的安全网）；纯文字笔划/装饰边框
    为单向，不命中。
    """
    hl, vl = _long_line_counts(pil_image_crop)
    return hl >= 1 and vl >= 1


def _lacks_dimension_text(pil_image_crop, block_area_ratio=None, score=None):
    """判断裁剪区是否非工程部件视图（应舍弃）。

    判定优先级（满足任一「工程视图」条件即返回 False=保留）：
    1. 面积门控：块占整页 ≥ DIM_NO_DIGIT_MAX_AREA_RATIO → 直接保留（防误杀大视图）。
    2. OCR 强尺寸信号：公差±/直径半径符号 → 工程尺寸标线。
    3. OCR 弱尺寸信号 ≥2：多个孤立尺寸数字 → 工程视图。
    4. OCR 弱尺寸信号 =1 且有长线（横或竖）→ 有尺寸数字+轮廓 → 工程视图。
    5. 高置信工程图：模型 score ≥ SCORE_KEEP_ENGINEERING(0.85) 且含任意长线 →
       工程视图（OCR 漏检数字也保留，如 P1HVQ 顶/侧视图、YKWA17 主面板）。
    6. 纯线框图：OCR 无输出（空）且具备工程线框（双向长线 或 高置信+长线）→ 保留
       （如 VK01 侧视图：低分但轮廓清晰）。

    关键区分：Logo 放大图/标签说明块（如 YCWA17#5「YORK VRP 放大图」）多为单向短
    笔划 + 中等模型 score(≈0.65 < 0.85)，无尺寸数字 → 不命中 2~6，落入舍弃。而真
    工程视图 score 普遍 ≥0.85 或具备双向长线（低分 VK01 侧视），故保留。

    返回 True 表示「非工程视图→应舍弃」，False 表示「工程视图或无法判断→保留」。
    """
    if block_area_ratio is not None and block_area_ratio >= DIM_NO_DIGIT_MAX_AREA_RATIO:
        return False  # 大块直接保留，跳过判定（防误杀真视图）
    hl, vl = _long_line_counts(pil_image_crop)
    has_any = hl + vl >= 1                     # 任意方向长线（轮廓/尺寸线）
    has_both = hl >= 1 and vl >= 1            # 双向长线（强工程轮廓特征）
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        # OCR 不可用：双向长线才保留，否则保守舍弃（防误杀纯线框图）
        return not has_both
    try:
        engine = RapidOCR()
        arr = np.asarray(pil_image_crop.convert("RGB"))
        result, _ = engine(arr)
        if not result or len(result) == 0:
            # OCR 空：纯线框工程图（双向长线 或 高置信+长线）保留，否则舍弃
            if has_both:
                return False
            if score is not None and score >= SCORE_KEEP_ENGINEERING and has_any:
                return False
            return True
        full_text = " ".join(line[1][0] for line in result)
        if _DIM_STRONG_RE.search(full_text):
            return False  # 强尺寸信号 → 工程视图，保留
        weak = len(_DIM_WEAK_RE.findall(full_text))
        if weak >= 2:
            return False  # ≥2 个孤立尺寸数字 → 工程视图，保留
        if weak >= 1 and has_any:
            return False  # 1 个尺寸数字 + 轮廓 → 工程视图，保留
        # 有文字但无数字尺寸信号（Logo/放大图/标签）：仅高置信工程图保留
        if score is not None and score >= SCORE_KEEP_ENGINEERING and has_any:
            return False
        return True  # 无尺寸信号、非高置信工程图 → 非工程视图 → 舍弃
    except Exception:
        if has_both:
            return False
        if score is not None and score >= SCORE_KEEP_ENGINEERING and has_any:
            return False
        return True


def _box_area(b):
    return (b[4] - b[2]) * (b[5] - b[3])


def _box_inter(a, b):
    ix0 = max(a[2], b[2]); iy0 = max(a[3], b[3])
    ix1 = min(a[4], b[4]); iy1 = min(a[5], b[5])
    return max(0, ix1 - ix0) * max(0, iy1 - iy0)


def _box_contains(inner, outer):
    """inner 有多少比例落在 outer 内（按 inner 面积归一）。"""
    a = _box_area(inner)
    return _box_inter(inner, outer) / a if a > 0 else 0.0


def _try_split_block(pil_image, x0, y0, x1, y1, page_area):
    """块内分裂：沿全宽/全高空白带或贯穿性细密分隔线将一块分为两块。

    模型偶发把两个独立视图合并为单框（无更细子框可 rescue）。本函数对保留块做
    内部结构边界检测——优先找贯穿整块的空白带，其次找贯穿性细密分隔线（两视图
    间的边框线）——若存在则沿边界一分为二。

    这是"反合并"操作（拆分模型过合并的单框），不合并任何已区分的块，与
    "只舍弃不合并"目标一致。返回 [(x0,y0,x1,y1),...] 页坐标（2 个子块）；
    无法分裂返回 None。仅做一次二分裂。
    """
    try:
        import cv2
    except Exception:
        return None
    w, h = x1 - x0, y1 - y0
    if min(w, h) < SPLIT_MIN_DIM:
        return None
    crop = pil_image.crop((x0, y0, x1, y1))
    arr = np.asarray(crop.convert("L"))
    ch, cw = arr.shape
    _, b = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    b = cv2.morphologyEx(b, cv2.MORPH_CLOSE, k)
    ink = (b > 0).astype(np.float32)
    col_d = ink.mean(axis=0)   # 每列墨迹占比
    row_d = ink.mean(axis=1)   # 每行墨迹占比

    def _find_boundary(proj, dim, span_axis):
        """找分裂边界位置（该方向坐标）。span_axis=0→列投影(竖切左右),
        1→行投影(横切上下)。优先空白带，其次贯穿性密线。"""
        # 1) 全宽/全高空白带
        i = 0
        best_gap = None
        while i < dim:
            if proj[i] < SPLIT_GAP_EPS:
                j = i
                while j < dim and proj[j] < SPLIT_GAP_EPS:
                    j += 1
                gw = j - i
                if gw >= SPLIT_GAP_MIN_RATIO * dim:
                    li = float(proj[:i].mean()) if i > 0 else 0.0
                    ri = float(proj[j:].mean()) if j < dim else 0.0
                    if li >= SPLIT_SIDE_INK_MIN and ri >= SPLIT_SIDE_INK_MIN:
                        if best_gap is None or gw > best_gap[1]:
                            best_gap = ((i + j) // 2, gw)
                i = j
            else:
                i += 1
        if best_gap is not None:
            return best_gap[0]
        # 2) 贯穿性细密分隔线（两视图间的边框/分割线）
        line_max_w = max(3, int(SPLIT_LINE_MAX_W * dim))
        i = 0
        best_line = None
        while i < dim:
            if proj[i] >= SPLIT_LINE_DENSITY:
                j = i
                while j < dim and j < i + line_max_w and proj[j] >= SPLIT_LINE_DENSITY * 0.5:
                    j += 1
                lw = j - i
                if 2 <= lw <= line_max_w:
                    # 该线在另一方向的贯穿率：每行/列该带是否有墨
                    if span_axis == 0:
                        span = float(ink[:, i:j].max(axis=1).mean())
                    else:
                        span = float(ink[i:j, :].max(axis=0).mean())
                    if span >= SPLIT_LINE_SPAN_MIN:
                        li = float(proj[:i].mean()) if i > 0 else 0.0
                        ri = float(proj[j:].mean()) if j < dim else 0.0
                        if li >= SPLIT_SIDE_INK_MIN and ri >= SPLIT_SIDE_INK_MIN:
                            dens = float(proj[i:j].mean())
                            if best_line is None or dens > best_line[1]:
                                best_line = ((i + j) // 2, dens)
                i = j
            else:
                i += 1
        if best_line is not None:
            return best_line[0]
        return None

    # 先尝试竖向切（左右）
    sp = _find_boundary(col_d, cw, 0)
    if sp is not None:
        return [(x0, y0, x0 + sp, y1), (x0 + sp, y0, x1, y1)]
    # 再尝试横向切（上下）
    sp = _find_boundary(row_d, ch, 1)
    if sp is not None:
        return [(x0, y0, x1, y0 + sp), (x0, y0 + sp, x1, y1)]
    return None


def _discard_overcoverage_parents(cands, page_area):
    """舍弃规则·过度覆盖：丢弃「吞并多个独立子视图」的大框，保留子视图本身。

    模型对两个并列独立视图常同时输出：1 个高分过度覆盖大框 + 2 个低分精确子框。
    本规则【只舍弃大框、不合并/不拆分】——把判定为过度覆盖的父框从候选中剔除，
    其子视图（精确子框）作为正常候选保留（后续仍须过几何/语义校验）。

    判定依据（见模块级 OVERCOVER_* 阈值）：
      · 叶视图：面积 ≥ OVERCOVER_CHILD_MIN_AREA 且不被任何其它候选框包含；
      · 过度覆盖父框：内含 ≥ OVERCOVER_MIN_CHILDREN 个互不包含(OVERCOVER_DISTINCT_IOU)
        的独立叶视图，且这些子视图至少有 OVERCOVER_CHILD_CONTAIN 落在其内、
        并集占该大框面积 ≥ OVERCOVER_UNION_RATIO。

    返回 [(score, cls, x0, y0, x1, y1, rescued), ...]，rescued=True 表示该块是被保留的
    子视图（因父框被舍弃而豁免 MIN_SCORE 硬门槛）。
    """
    n = len(cands)
    if n == 0:
        return []

    # 1) 标记「叶视图」：面积达标 且 不被任何其它候选框包含（≥ OVERCOVER_CHILD_CONTAIN）
    is_leaf = [False] * n
    for i in range(n):
        if _box_area(cands[i]) < OVERCOVER_CHILD_MIN_AREA * page_area:
            continue
        leaf = True
        for j in range(n):
            if j == i:
                continue
            if _box_area(cands[j]) <= 0:
                continue
            if _box_contains(cands[j], cands[i]) >= OVERCOVER_CHILD_CONTAIN:
                leaf = False
                break
        is_leaf[i] = leaf

    # 2) 判定过度覆盖父框并标记其子视图
    discarded_parent = set()
    child_of = [-1] * n
    for i in range(n):
        if is_leaf[i]:
            continue  # 叶本身不可能是过度覆盖父框
        inside = [j for j in range(n)
                  if is_leaf[j] and _box_contains(cands[j], cands[i]) >= OVERCOVER_CHILD_CONTAIN]
        # 互不包含的独立叶视图计数
        distinct = []
        for j in inside:
            L = cands[j]
            overlapping = False
            for k in distinct:
                M = cands[k]
                li = _box_inter(L, M) / max(1, _box_area(L))
                mi = _box_inter(L, M) / max(1, _box_area(M))
                if li >= OVERCOVER_DISTINCT_IOU or mi >= OVERCOVER_DISTINCT_IOU:
                    overlapping = True
                    break
            if not overlapping:
                distinct.append(j)
        if len(distinct) < OVERCOVER_MIN_CHILDREN:
            continue
        union = sum(_box_area(cands[j]) for j in inside)
        if _box_area(cands[i]) > 0 and union >= OVERCOVER_UNION_RATIO * _box_area(cands[i]):
            discarded_parent.add(i)
            for j in inside:
                child_of[j] = i

    # 3) 输出：剔除父框，保留子视图（打 rescued 标记）
    result = []
    for i in range(n):
        if i in discarded_parent:
            continue
        c = cands[i]
        result.append((c[0], c[1], c[2], c[3], c[4], c[5], child_of[i] >= 0))
    return result


def detect(pil_image, W, H):
    """对整页 PIL 图检测「需要匹配的图块」（产品部件视图）。

    参数:
        pil_image: 全页渲染 PIL 图（RGB），尺寸应为 (W, H)
        W, H:      渲染图宽高（与 pil_image 一致）
    返回:
        [(x0,y0,x1,y1), ...] 原图像素坐标；无有效块或不可用时返回 None

    后期处理原则【只做舍弃、不做合并】：
      ONNX 模型已能区分版面中的独立图块（与 PaddleOCR-VL 输出一致），本函数
      仅按明确的舍弃规则剔除不合格候选，绝不把已区分的独立图块重新合并。

    流程：
      1) 候选构建：score ≥ RAW_SCORE_FLOOR 且为 KEEP_CLS 的模型输出；
      2) 舍弃规则·过度覆盖：丢弃吞并 ≥2 独立子视图的大框，保留子视图本身；
      3) 逐块舍弃规则：独立视图需 score ≥ MIN_SCORE；并过几何/二维码/尺寸文字门槛；
      4) 舍弃规则·子检测冗余：块 ≥ SUBDET_DROP_CONTAIN 落在另一保留块内 → 舍弃内层块。
    """
    if not is_available():
        return None
    try:
        import cv2
    except Exception:
        return None

    ih, iw = pil_image.height, pil_image.width
    arr = np.asarray(pil_image.convert("RGB"))

    # 预处理：BICUBIC resize 到 800x800 → /255（无 ImageNet 归一化）→ CHW
    resized = cv2.resize(arr, (800, 800), interpolation=cv2.INTER_CUBIC)
    blob = resized.astype(np.float32) / 255.0
    blob = blob.transpose(2, 0, 1)[None, ...]          # 1,3,800,800
    im_shape = np.array([[800.0, 800.0]], np.float32)
    scale_factor = np.array([[800.0 / ih, 800.0 / iw]], np.float32)

    sess = _get_session()
    inames = [i.name for i in sess.get_inputs()]
    onames = [o.name for o in sess.get_outputs()]
    # 输入顺序固定：im_shape, image, scale_factor
    feed = {inames[0]: im_shape, inames[1]: blob, inames[2]: scale_factor}
    out = sess.run(onames, feed)[0]
    rows = np.asarray(out, dtype=np.float32).reshape(-1, 7)

    page_area = float(W * H)

    # 1) 候选构建（RAW_SCORE_FLOOR 放宽下限，让低分精确子框也能参与过度覆盖判定）
    cands = []  # (score, cls, x0, y0, x1, y1)
    for r in rows:
        cls, score, x1, y1, x2, y2, order = r
        if score < RAW_SCORE_FLOOR:
            continue
        if int(round(cls)) not in KEEP_CLS:
            continue
        x0, y0, xa, yb = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
        if xa <= x0 or yb <= y0:
            continue
        cands.append((float(score), int(round(cls)), x0, y0, xa, yb))

    # 2) 舍弃规则·过度覆盖：丢弃吞并大框，保留子视图本身（不合并）
    kept = _discard_overcoverage_parents(cands, page_area)

    # 3) 逐块舍弃规则（分数/几何/二维码/尺寸文字门槛）
    scored = []  # (x0, y0, x1, y1)
    for (score, cls, x0, y0, xa, yb, rescued) in kept:
        if not rescued and score < MIN_SCORE:
            continue  # 独立视图置信不足 → 舍弃
        w, h = xa - x0, yb - y0
        area = w * h
        ratio = area / page_area if page_area > 0 else 0
        if ratio < MIN_AREA_RATIO or ratio > MAX_AREA_RATIO:
            continue
        if max(w, h) / max(1, min(w, h)) > MAX_ASPECT_RATIO:
            continue
        if _is_qr_code_dominant(pil_image, x0, y0, xa, yb, page_area):
            continue
        crop_for_ocr = pil_image.crop((x0, y0, xa, yb))
        if _lacks_dimension_text(crop_for_ocr, ratio, score):
            continue
        # 存为 6 元组 (0,0,x0,y0,x1,y1) 以复用 _box_area/_box_contains 几何判定
        scored.append((0, 0, x0, y0, xa, yb))

    # 4) 舍弃规则·子检测冗余（去重，非合并）
    result = []
    for i, b in enumerate(scored):
        drop = False
        for j, o in enumerate(scored):
            if j == i:
                continue
            if _box_contains(b, o) >= SUBDET_DROP_CONTAIN:
                drop = True
                break
        if not drop:
            result.append((b[2], b[3], b[4], b[5]))

    return result if result else None


# 供 segment_blocks_auto.detect_auto_regions 直接调用
def detect_regions(color, W, H):
    return detect(color, W, H)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and os.path.exists(sys.argv[1]):
        from PIL import Image
        img = Image.open(sys.argv[1]).convert("RGB")
        regs = detect(img, img.width, img.height)
        print("MODEL_PATH", MODEL_PATH)
        print("regions", regs)
    else:
        print("usage: pp_doclayout_onnx.py <image.png>")
