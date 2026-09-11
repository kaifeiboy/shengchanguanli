# -*- coding: utf-8 -*-
"""
库存尾数模块 · 拍照识别管线（v2 全新实现，独立于平台其它模块的任何脚本）

三个子命令：
  prepare   上传照片后：自动检测文档四角 → 透视畸变矫正 → 白平衡 → 输出可框选的处理图
  rewarp    用户手动拖动四角 / 切换白平衡后：复用会话原图重新矫正（手机不必重传）
  recognize 只识别用户框选的 ROI（可多个），按业务规则解析为「编码 + 材料信息」

业务规则（用户明确）：
  · 「子件编码」→ 编码，规律是 10 位数字
  · 「子件名称」→ 材料信息，为文字描述等其它信息
  · 连续框选多条 → 按多条信息分别录入（同一个框内识别到多个 10 位编码时也会自动拆条）

输出：stdout 仅一段 JSON：{"success": true/false, ...}
      第三方库（RapidOCR/onnxruntime）的打印全部被吞掉，避免污染 JSON。
"""
import argparse
import io
import json
import os
import re
import sys

# ---- 关键：先接管 stdout，防止第三方库 print 污染 JSON 输出 ----
_REAL_STDOUT = sys.stdout
sys.stdout = io.StringIO()

import numpy as np          # noqa: E402
import cv2                  # noqa: E402

CODE_RE = re.compile(r'(?<!\d)(\d{10})(?!\d)')
# 紧贴编码：表格「编码列 + 代号列」之间无空格时，OCR 会把 10 位编码与 6-12 位代号连成
# 一串长数字（如 1102040009+213050206 → 1102040009213050206）。此时前 10 位即编码。
TIGHT_CODE_RE = re.compile(r'(?<!\d)(\d{10})(?=\d{6,12}(?!\d))')
CODE_LABELS = ('子件编码', '子件编号', '物料编码', '物料编号', '材料编码',
               '零件编码', '零件号', '料号', '编码', '编号', 'code')
NAME_LABELS = ('子件名称', '物料名称', '材料名称', '零件名称', '材料信息',
               '品名', '名称', 'name')
# 表头/噪声词：整行等于这些词时丢弃，避免把表头当材料信息
NOISE_EQ = set(['子件编码', '子件名称', '子件代号', '编码', '名称', '数量', '单位', '序号',
                '规格', '型号', '备注', '库号', '物料编码', '物料名称', '物料代号', '单位数量'])


# ============================ 基础 IO ============================

def imread_unicode(path):
    """支持中文路径读图。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def imwrite_unicode(path, img, quality=92):
    """支持中文路径写图。"""
    ext = os.path.splitext(path)[1] or '.jpg'
    params = [int(cv2.IMWRITE_JPEG_QUALITY), quality] if ext.lower() in ('.jpg', '.jpeg') else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise RuntimeError('图片编码失败')
    buf.tofile(path)


def find_src(session_dir):
    """在会话目录中找原图（prepare 时按上传扩展名落盘）。"""
    for name in sorted(os.listdir(session_dir)):
        low = name.lower()
        if low.startswith('src.') and low.rsplit('.', 1)[-1] in ('jpg', 'jpeg', 'png', 'bmp', 'webp'):
            return os.path.join(session_dir, name)
    raise RuntimeError('会话原图不存在，请重新拍照')


def find_contours_compat(binary):
    """OpenCV 3/4/5 findContours 返回值差异兼容。"""
    res = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    return res[0] if len(res) == 2 else res[1]


# ============================ 透视畸变矫正 ============================

def order_corners(pts):
    """把 4 点排成 左上→右上→右下→左下。"""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def _quad_from_min_rect(cnt):
    """approxPolyDP 失败时的兜底：minAreaRect 给最小旋转矩形的 4 顶点，按
    order_corners 同序（左上→右上→右下→左下）输出。"""
    rect = cv2.minAreaRect(cnt)
    box = cv2.boxPoints(rect)
    return order_corners(box.astype(np.float32))


def _is_plausible_paper(pts, img_shape):
    """四角点合理性过滤：边长需 > 短边 0.18 倍，且长宽比落在 [0.3, 3.5]，面积占图像 ≥ 0.18。"""
    h, w = img_shape[:2]
    p = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    sides = [np.linalg.norm(p[(i + 1) % 4] - p[i]) for i in range(4)]
    long_side = max(sides)
    short_side = min(sides)
    min_side = min(w, h) * 0.18  # 边长下限，避免拿到桌面杂物小矩形
    if short_side < min_side:
        return False
    ratio = long_side / max(short_side, 1e-6)
    if ratio < 0.3 or ratio > 3.5:
        return False
    area = abs(cv2.contourArea(p.astype(np.float32)))
    area_all = float(w * h)
    if area < area_all * 0.18 or area > area_all * 0.995:
        return False
    return True


def detect_document(img):
    """
    自动检测拍摄的单据/标签边界四角 —— 二阶段策略。
      阶段1（主）：缩小 → 灰度 → 闭运算抹掉文字 → Canny → 近似多边形找 4 边形。
      阶段2（兜底）：阶段1 失败时，对最大几个轮廓直接用 minAreaRect 求最小旋转矩形，
                  通过 _is_plausible_paper 边长+面积合理性过滤，再做透视变换。
    返回原图坐标系的 4 点；检测不到返回 None（此时不做矫正，避免把好照片切坏）。

    注意（2026-09-04 H5 原图验证）：核 (9,9) 在纸张被裁切/有手指遮挡的实际车间
    照片上会把纸/表边界断成 < 0.2% 面积的小碎片，阶段1/2 都拿不到。改为 (3,3) 后
    大矩形（纸或表）能稳定成为最大轮廓（~28% 面积），阶段2 的 minAreaRect 兜底
    即可拿到合理四角。核太大反而把真边界"吃掉"，与初衷相反。
    """
    h, w = img.shape[:2]
    long_side = max(h, w)
    scale = 1000.0 / long_side if long_side > 1000 else 1.0
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1.0 else img.copy()

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    # 核 (3,3) 只抹非常细碎的噪点，保留纸/表的大边界；之前的 (9,9) 会把被裁切/有
    # 遮挡的纸边界一起腐蚀掉，导致所有候选轮廓都 < 0.2% 面积、双阶段都漏检。
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    edges = cv2.Canny(closed, 40, 130)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    cnts = find_contours_compat(edges)
    if not cnts:
        return None
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:8]
    area_all_small = float(small.shape[0] * small.shape[1])

    # ----- 阶段1：approxPolyDP 直接找 4 边形 -----
    for c in cnts:
        peri = cv2.arcLength(c, True)
        if peri <= 0:
            continue
        # 0.03*peri 略放宽（真实拍图边不平直），让接近 4 边的轮廓也能凑成 4 顶点
        approx = cv2.approxPolyDP(c, 0.03 * peri, True)
        if len(approx) != 4:
            continue
        area = abs(cv2.contourArea(approx))
        if area < area_all_small * 0.22 or area > area_all_small * 0.995:
            continue
        if not cv2.isContourConvex(approx):
            continue
        pts = approx.reshape(4, 2).astype(np.float32) / float(scale)
        pts[:, 0] = np.clip(pts[:, 0], 0, w - 1)
        pts[:, 1] = np.clip(pts[:, 1], 0, h - 1)
        quad = order_corners(pts)
        # 用合理性过滤做最后一道闸门（拒绝长得不像纸张的 4 边）
        if not _is_plausible_paper(quad, img.shape):
            continue
        return quad

    # ----- 阶段2：minAreaRect 兜底 -----
    for c in cnts:
        peri = cv2.arcLength(c, True)
        if peri <= 0:
            continue
        area_rect_full = float(cv2.contourArea(c))
        if area_rect_full < area_all_small * 0.22:
            continue
        quad = _quad_from_min_rect(c)
        # 注意：_quad_from_min_rect 返回的是 small 图坐标，转回原图必须 /scale
        # （与阶段1 的 /float(scale) 方向一致）。之前误写 *scale，导致四角留在
        # small 坐标内、_is_plausible_paper 永远 False、兜底永远失效。
        quad_full = quad / float(scale)
        if not _is_plausible_paper(quad_full, img.shape):
            continue
        # 包到图像范围内
        quad_full[:, 0] = np.clip(quad_full[:, 0], 0, w - 1)
        quad_full[:, 1] = np.clip(quad_full[:, 1], 0, h - 1)
        return order_corners(quad_full)

    return None


def warp_by_corners(img, corners):
    """按四角做透视变换，输出正视矩形图。"""
    tl, tr, br, bl = order_corners(corners)
    wa = np.linalg.norm(br - bl)
    wb = np.linalg.norm(tr - tl)
    ha = np.linalg.norm(tr - br)
    hb = np.linalg.norm(tl - bl)
    out_w = int(round(max(wa, wb)))
    out_h = int(round(max(ha, hb)))
    if out_w < 40 or out_h < 40:
        raise RuntimeError('框选的四角范围过小，无法矫正')
    # 上限保护：避免超大图把内存打满
    max_side = 3000
    if max(out_w, out_h) > max_side:
        r = max_side / float(max(out_w, out_h))
        out_w = max(40, int(out_w * r))
        out_h = max(40, int(out_h * r))
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype=np.float32), dst)
    return cv2.warpPerspective(img, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


# ============================ 白平衡 ============================

def wb_gray_world(img):
    """灰世界白平衡：三通道均值拉平。车间黄光/冷白光下最稳。"""
    f = img.astype(np.float32)
    means = [max(f[:, :, i].mean(), 1e-6) for i in range(3)]
    gray = float(np.mean(means))
    for i in range(3):
        f[:, :, i] *= (gray / means[i])
    return np.clip(f, 0, 255).astype(np.uint8)


def wb_white_patch(img, pct=0.02):
    """白点白平衡：取每通道最亮 2% 像素均值作为白点归一（纸张为白底时效果更贴真实）。"""
    f = img.astype(np.float32)
    for i in range(3):
        ch = f[:, :, i]
        thr = np.percentile(ch, 100.0 * (1.0 - pct))
        sel = ch[ch >= thr]
        ref = float(sel.mean()) if sel.size > 0 else 255.0
        if ref < 1e-6:
            ref = 255.0
        f[:, :, i] = ch * (245.0 / ref)
    return np.clip(f, 0, 255).astype(np.uint8)


def _auto_pick_wb(img):
    """根据图像整体偏色智能挑白平衡算法。
    在白底纸张（最亮像素通道均值 ≥ 230）且不偏暖光（红蓝均差 ≤ 14）时用白点法（视觉差最明显）；
    其余（黄光/低照度/暗背景）用灰世界法（最稳）。"""
    b, g, r = cv2.split(img.astype(np.float32))
    # 最亮 2% 像素的通道均值判断"是否有可用的白底"
    white_ref = float(np.percentile(np.maximum(np.maximum(r, g), b), 98.0))
    # RGB 偏色：R+B-2G 反映黄/蓝偏移（R+B 高 → 偏黄暖光）
    rb_imbalance = float(np.mean(r) + np.mean(b) - 2.0 * np.mean(g))
    if white_ref >= 230.0 and abs(rb_imbalance) <= 14.0:
        return 'white'
    return 'gray'


def apply_wb(img, mode):
    """白平衡入口。'auto' 会按图像偏色自动选 white/gray；其它显式值透传。
    'none' 不过任何白平衡；'white' 白点法；其它默认走灰世界（最稳）。"""
    if mode == 'none':
        return img
    if mode == 'auto':
        mode = _auto_pick_wb(img)
    if mode == 'white':
        return wb_white_patch(img)
    return wb_gray_world(img)


def enhance_for_view(img):
    """
    轻度增强，仅用于预览与后续 OCR：L 通道 CLAHE 提升文字对比度，
    不做二值化（二值化会在光照不均的车间照片上丢字）。
    """
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


def limit_size(img, max_side=2200):
    """限制处理图长边，兼顾手机加载速度与 OCR 精度。"""
    h, w = img.shape[:2]
    if max(h, w) <= max_side:
        return img
    r = max_side / float(max(h, w))
    return cv2.resize(img, (int(w * r), int(h * r)), interpolation=cv2.INTER_AREA)


# ============================ OCR ============================

_ENGINE = None


def get_engine():
    """惰性创建 RapidOCR（模型加载较慢，一个进程只建一次）。"""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def ocr_lines(img):
    """
    识别一张（ROI）图，返回 [{'text','score','cx','cy','h'}]，按上→下、左→右排序。
    """
    engine = get_engine()
    res, _ = engine(img)
    out = []
    if not res:
        return out
    for item in res:
        try:
            box, text, score = item[0], item[1], item[2]
        except Exception:
            continue
        t = (text or '').strip()
        if not t:
            continue
        pts = np.asarray(box, dtype=np.float32).reshape(-1, 2)
        ys = pts[:, 1]
        xs = pts[:, 0]
        out.append({
            'text': t,
            'score': float(score) if score is not None else 0.0,
            'cx': float(xs.mean()),
            'cy': float(ys.mean()),
            'h': float(ys.max() - ys.min())
        })
    out.sort(key=lambda d: (round(d['cy'] / max(d['h'], 1.0)), d['cx']))
    return out


def prep_roi_for_ocr(roi):
    """
    ROI 预处理：提升真实车间照片（低对比、轻微模糊、手机压缩噪点）的识别率。
      · L 通道 CLAHE 增强对比；
      · 温和 unsharp 锐化（补回被模糊/压缩吃掉的字迹边缘）。

    注意（2026-09-04 真实照片验证）：非局部均值去噪 fastNlMeansDenoisingColored
    会把「示」等笔画细节磨掉导致识别成「宗」（提示→提宗），属于明确有害步骤，
    已移除。unsharp 强度 1.5→1.2，避免过度锐化产生字边伪影。
    之后再做小字放大（与原策略一致），避免小字号漏识别。
    """
    # 对比增强 + 温和锐化（先做空间增强，再放大，避免把噪点一起放大）
    try:
        lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        roi = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    except Exception:
        pass
    try:
        blur = cv2.GaussianBlur(roi, (0, 0), 1.5)
        roi = cv2.addWeighted(roi, 1.2, blur, -0.2, 0)
    except Exception:
        pass

    h, w = roi.shape[:2]
    if h <= 0 or w <= 0:
        return roi
    min_h, min_w = 32, 180
    if h < min_h:
        scale_h = min_h / h
        roi = cv2.resize(roi, None, fx=scale_h, fy=scale_h, interpolation=cv2.INTER_CUBIC)
    h, w = roi.shape[:2]
    if w < min_w:
        scale_w = min_w / w
        roi = cv2.resize(roi, None, fx=scale_w, fy=scale_w, interpolation=cv2.INTER_CUBIC)
    if max(roi.shape[:2]) > 1800:
        f = 1800.0 / max(roi.shape[:2])
        roi = cv2.resize(roi, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    return roi


# ============================ 解析：编码 + 材料信息 ============================

def extract_code(text):
    """从文本中提取 10 位「子件编码」。先标准（前后非数字），再紧贴（编码后紧跟 6-12 位代号）。"""
    if not text:
        return ''
    m = CODE_RE.search(text)
    if m:
        return m.group(1)
    m = TIGHT_CODE_RE.search(text)
    if m:
        return m.group(1)
    return ''


def strip_label(text, labels):
    """去掉行首标签词与紧随的分隔符，返回值部分。"""
    t = text.strip()
    for lb in labels:
        idx = t.find(lb)
        if idx >= 0:
            t = t[idx + len(lb):]
            break
    return t.lstrip(' :：=·.、|　').strip()


# 紧贴的「编码(10位)+代号(6-12位)」整对（16-22 位连续数字），清洗时整体去掉
_TIGHT_CODE_PAIR_RE = re.compile(r'(?<!\d)\d{10}\d{6,12}(?!\d)')


def clean_info(text):
    """材料信息清洗：去掉 10 位编码、去掉紧贴的「编码+代号」连串、去掉孤立标签词与多余符号；
    纯数字行/纯符号行返回空。"""
    if not text:
        return ''
    t = _TIGHT_CODE_PAIR_RE.sub(' ', text)  # 先整体去掉「编码+代号」紧贴对
    t = CODE_RE.sub(' ', t)                 # 再去掉残留的独立 10 位编码
    for lb in CODE_LABELS + NAME_LABELS:
        t = t.replace(lb, ' ')
    t = re.sub(r'[|丨│┃\t]+', ' ', t)
    t = re.sub(r'\s{2,}', ' ', t).strip(' :：=·、,，.。|　')
    # 纯数字 / 纯符号 / 纯日期 / 纯经手人字段 → 视为量化字段，不进材料信息
    if not t:
        return ''
    if re.fullmatch(r'[\d\s\.\-/]+', t):
        return ''
    if re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}.*', t):
        return ''
    if re.match(r'^(经手人|操作人|仓管员|记录员)[：:]\s*\S+\s*$', t):
        return ''
    return t.strip()


# 子件代号：位于「编码」与「名称」之间的独立数字代号列（多为 9 位，亦可为 6-12 位纯数字）。
_DAIMA_RE = re.compile(r'^\s*\d{6,12}(?=[\s　])')

def strip_daicode(text):
    """去掉材料信息开头的子件代号（如 '215010116 包装箱...' → '包装箱...'）。

    业务规则：子件编码→编码，子件名称→材料信息；子件代号是另一独立字段，
    不应混入材料信息。识别出的行常为「编码 代号 名称」三列，去掉 10 位编码后
    别名仍残留「代号」，这里去掉行首 6-12 位纯数字代号（其后须有空白，避免误伤
    诸如「3M」「100UF」这类以数字开头的真实名称）。
    """
    t = (text or '').strip()
    m = _DAIMA_RE.search(t)
    if m:
        return t[m.end():].strip()
    return t

def finalize_info(raw):
    """材料信息收尾清洗：去编码/标签/噪声后，再去掉行首子件代号，只保留子件名称。"""
    return strip_daicode(clean_info(raw))


def is_meaningful_info(t):
    """材料信息有效性：至少 2 个字符，且不是纯数字/纯符号，且不是表头词。"""
    if not t or len(t) < 2:
        return False
    if t in NOISE_EQ:
        return False
    if re.fullmatch(r'[\d\s\.\-/]+', t):
        return False
    return bool(re.search(r'[\u4e00-\u9fffA-Za-z]', t))


def group_rows(lines):
    """按 y 把识别结果聚成文本行（同一行的多个片段合并）。

    容差收紧：之前 0.65*unit 把视觉相邻的两行合并成一串，造成数字串位 + 材料信息互相吞噬。
    改为 0.5*unit（最低 4.5），让相邻行更可靠地分开。
    """
    if not lines:
        return []
    hs = [d['h'] for d in lines if d['h'] > 0]
    unit = float(np.median(hs)) if hs else 12.0
    tol = max(unit * 0.5, 4.5)
    rows = []
    for d in sorted(lines, key=lambda x: x['cy']):
        if rows and abs(d['cy'] - rows[-1]['cy']) <= tol:
            rows[-1]['items'].append(d)
            n = len(rows[-1]['items'])
            rows[-1]['cy'] = sum(i['cy'] for i in rows[-1]['items']) / n
        else:
            rows.append({'cy': d['cy'], 'items': [d]})
    out = []
    for r in rows:
        segs = sorted(r['items'], key=lambda x: x['cx'])
        out.append({
            'text': ' '.join(s['text'] for s in segs).strip(),
            'segs': [s['text'] for s in segs],
            'cy': r['cy']
        })
    return out


def parse_by_labels(rows):
    """
    优先走标签匹配：识别到「子件编码 / 子件名称」这类表头式排版时最准。
    返回 (code, info) 任一为空表示未命中。
    """
    code, info = '', ''
    for r in rows:
        t = r['text']
        if not code and any(lb in t for lb in CODE_LABELS):
            val = strip_label(t, CODE_LABELS)
            code = extract_code(val) or extract_code(t)
        if not info and any(lb in t for lb in NAME_LABELS):
            val = finalize_info(strip_label(t, NAME_LABELS))
            if is_meaningful_info(val):
                info = val
    return code, info


def parse_rows(rows):
    """
    把若干文本行解析为一条或多条 {code, materialInfo}。
    · 行内含 10 位编码的行数 >= 2 → 认为用户框了多条记录，按行拆条
      （编码同行的文字作材料信息；同行没有文字时向下/向上就近取一行文字）
    · 否则整块合成一条：编码取第一个 10 位数字，材料信息取剩余有意义文字拼接
    """
    if not rows:
        return []

    code_rows = [i for i, r in enumerate(rows) if extract_code(r['text'])]

    # ---- 多条：每个含编码的行各成一条 ----
    if len(code_rows) >= 2:
        items = []
        used_info_rows = set()
        for i in code_rows:
            text = rows[i]['text']
            code = extract_code(text)
            # 行内除编码外的有意义文字：优先取同行，再就近取一行（先下再上）
            inline = finalize_info(text)
            info = inline if is_meaningful_info(inline) else ''
            if not info:
                for j in list(range(i + 1, len(rows))) + list(range(i - 1, -1, -1)):
                    if j in code_rows or j in used_info_rows:
                        continue
                    cand = finalize_info(rows[j]['text'])
                    if is_meaningful_info(cand):
                        info = cand
                        used_info_rows.add(j)
                        break
            items.append({'code': code, 'materialInfo': info if is_meaningful_info(info) else ''})
        return items

    # ---- 单条：先标签、再兜底 ----
    code, info = parse_by_labels(rows)
    if not code:
        for r in rows:
            c = extract_code(r['text'])
            if c:
                code = c
                break
    if not info:
        parts = []
        for r in rows:
            c = finalize_info(r['text'])
            if is_meaningful_info(c):
                parts.append(c)
        info = ' '.join(parts).strip()
        info = re.sub(r'\s{2,}', ' ', info)
    return [{'code': code, 'materialInfo': info}]


# ============================ 子命令 ============================

def cmd_prepare(args):
    img = imread_unicode(args.src)
    if img is None:
        raise RuntimeError('照片无法解析，请重新拍照')
    src_h, src_w = img.shape[:2]

    corners = None
    warped = False
    out = img
    if args.auto_warp == '1':
        try:
            corners = detect_document(img)
        except Exception:
            corners = None
        if corners is not None:
            try:
                out = warp_by_corners(img, corners)
                warped = True
            except Exception:
                out = img
                warped = False

    out = apply_wb(out, args.wb)
    out = enhance_for_view(out)
    out = limit_size(out)
    imwrite_unicode(os.path.join(args.dir, 'warp.jpg'), out)

    h, w = out.shape[:2]
    payload = {
        'success': True,
        'width': int(w), 'height': int(h),
        'srcWidth': int(src_w), 'srcHeight': int(src_h),
        'warped': bool(warped),
        'wb': args.wb,
        'corners': ([[float(p[0]), float(p[1])] for p in corners] if corners is not None else [])
    }
    return payload


def cmd_rewarp(args):
    # 角点基于「当前显示图」（warp.jpg）坐标系：直接基于 warp.jpg 重矫正，
    # 与前端点击/框选所用坐标系完全一致，避免把角点误当作原图坐标系而错位。
    # 仅当完全没有角点（纯白平衡微调）时才回退到原图坐标。
    if args.corners:
        img = imread_unicode(os.path.join(args.dir, 'warp.jpg'))
        if img is None:
            src = find_src(args.dir)
            img = imread_unicode(src)
    else:
        src = find_src(args.dir)
        img = imread_unicode(src)
    if img is None:
        raise RuntimeError('照片不存在，请重新拍照')

    warped = False
    out = img
    if args.no_warp != '1' and args.corners:
        pts = json.loads(args.corners)
        if not isinstance(pts, list) or len(pts) != 4:
            raise RuntimeError('四角参数必须为 4 个点')
        arr = np.array([[float(p[0]), float(p[1])] for p in pts], dtype=np.float32)
        h, w = img.shape[:2]
        arr[:, 0] = np.clip(arr[:, 0], 0, w - 1)
        arr[:, 1] = np.clip(arr[:, 1], 0, h - 1)
        out = warp_by_corners(img, arr)
        warped = True

    out = apply_wb(out, args.wb)
    out = enhance_for_view(out)
    out = limit_size(out)
    imwrite_unicode(os.path.join(args.dir, 'warp.jpg'), out)
    h, w = out.shape[:2]
    return {'success': True, 'width': int(w), 'height': int(h), 'warped': bool(warped), 'wb': args.wb}


def cmd_recognize(args):
    path = os.path.join(args.dir, 'warp.jpg')
    img = imread_unicode(path)
    if img is None:
        raise RuntimeError('处理后的照片不存在，请重新拍照')
    H, W = img.shape[:2]

    rois = json.loads(args.rois)
    if not isinstance(rois, list) or not rois:
        raise RuntimeError('未收到框选区域')

    items = []
    for idx, r in enumerate(rois):
        try:
            x, y, w, h = [int(round(float(v))) for v in (r[0], r[1], r[2], r[3])]
        except Exception:
            continue
        # 少量外扩，避免贴边切掉笔画
        pad = max(2, int(min(w, h) * 0.04))
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(W, x + w + pad)
        y1 = min(H, y + h + pad)
        if x1 - x0 < 8 or y1 - y0 < 8:
            items.append({'roiIndex': idx, 'code': '', 'materialInfo': '', 'lines': []})
            continue

        roi = prep_roi_for_ocr(img[y0:y1, x0:x1])
        try:
            lines = ocr_lines(roi)
        except Exception as e:
            raise RuntimeError('识别引擎异常：%s' % e)

        rows = group_rows(lines)
        raw = [r0['text'] for r0 in rows]
        parsed = parse_rows(rows)
        for p in parsed:
            items.append({
                'roiIndex': idx,
                'code': p.get('code', ''),
                'materialInfo': p.get('materialInfo', ''),
                'lines': raw
            })

    return {'success': True, 'count': len(items), 'items': items}


def main():
    ap = argparse.ArgumentParser(description='库存尾数拍照识别管线')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('prepare')
    p1.add_argument('--src', required=True)
    p1.add_argument('--dir', required=True)
    p1.add_argument('--wb', default='auto')
    p1.add_argument('--auto-warp', dest='auto_warp', default='1')

    p2 = sub.add_parser('rewarp')
    p2.add_argument('--dir', required=True)
    p2.add_argument('--wb', default='auto')
    p2.add_argument('--corners', default='')
    p2.add_argument('--from-warp', dest='from_warp', default='0')
    p2.add_argument('--no-warp', dest='no_warp', default='0')

    p3 = sub.add_parser('recognize')
    p3.add_argument('--dir', required=True)
    p3.add_argument('--rois', required=True)

    args = ap.parse_args()
    if args.cmd == 'prepare':
        return cmd_prepare(args)
    if args.cmd == 'rewarp':
        return cmd_rewarp(args)
    return cmd_recognize(args)


if __name__ == '__main__':
    try:
        result = main()
    except Exception as ex:
        result = {'success': False, 'error': str(ex)}
    finally:
        sys.stdout = _REAL_STDOUT
    print(json.dumps(result, ensure_ascii=False))
