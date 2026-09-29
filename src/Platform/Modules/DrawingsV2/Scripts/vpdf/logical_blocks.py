# -*- coding: utf-8 -*-
"""
逻辑图块提取器 —— 产出 `logical-blocks/1` 契约（方案 §0.5 / §2 / §4）。

设计要点（对应 docs/逻辑图块方案_2026-09-26.md）：
- 图块 = 元素逻辑集合 + 包围盒（**非物理切块**）：PDF 只读，元素保留**全局归一化坐标**。
- 几何层复用 v8.4 部位分离（tools/_extract_separate.py，八图基线已固化）。
- 本模块新增：元素 5 分类（a/b/c/d/e）、块以打标内容命名（name/name_fp）、
  空块判定 has_marking（文本层 + 图形 + 局部高分辨率 OCR 三路证据，§2.2）、view_hint。

用法（CLI）：
    python logical_blocks.py --pdf=<pdf> [--json-out=<path>] [--no-ocr]
    python logical_blocks.py --pid=67
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

HERE = os.path.dirname(os.path.abspath(__file__))     # .../Scripts/vpdf
SCRIPTS = os.path.dirname(HERE)                       # .../DrawingsV2/Scripts
# vpdf → Scripts → DrawingsV2 → Modules → Platform → src → 项目根（6 级）
PROJECT = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", ".."))
SEPARATE = os.path.join(PROJECT, "tools", "_extract_separate.py")
PY = sys.executable

SCHEMA = "logical-blocks/1"
ALGO = "v8.4"
SEPARATOR = "｜"          # §0.5.3.1 命名分隔符
NAME_MAX = 120            # 块名总长上限
ITEM_MAX = 40             # 单条文本上限
LOW_CONF = 0.60           # OCR 低置信阈值（低于此记 low_conf，命中走黄）


# ---------------- 归一化 / 命名（§0.5.3.1） ----------------

def _halfwidth(s: str) -> str:
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize(s: str) -> str:
    """命中检索用的归一化：全角转半角 + 小写 + 仅保留字母数字。"""
    return re.sub(r"[^0-9a-z]", "", _halfwidth(s or "").lower())


def _clip(s: str, n: int = ITEM_MAX) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def build_name(texts: List[str], view_hint: str = "", dup_seq: int = 0) -> str:
    """按阅读序拼接块名（§0.5.3.1 A）。"""
    parts, total = [], 0
    for t in texts:
        t = _clip(t)
        if not t:
            continue
        add = len(t) + (1 if parts else 0)
        if total + add > NAME_MAX - 8:          # 预留 "…(+k 条)" 与消歧后缀
            break
        parts.append(t)
        total += add
    name = SEPARATOR.join(parts)
    if not name:
        return ""
    if dup_seq > 0:
        name = "%s #%d" % (name, dup_seq + 1)
    return name


# ---------------- 元素分类（§2，5 类） ----------------

def _is_dim_text(t: str) -> bool:
    """d 类：CAD 尺寸/标注（旧的一维字符判据）。

    ⚠ **已不再作为主判据**（2026-09-27 L0+L1 改造），保留仅为兼容与对照。
    新链路一律走 :func:`classify_text` —— 实证表明纯字符维度不可判定：
    p50 的 `200512` 与 `1250001` 同字体同字号同行，字符形态完全同分布。
    """
    s = (t or "").strip()
    if not s:
        return False
    if re.fullmatch(r"[\d\.\+\-±×xX/°′″,\s]*\d[\d\.\+\-±×xX/°′″,\s]*", s):
        return True
    if re.match(r"^[RrCc]?\d+(\.\d+)?$", s):
        return True
    if s in {"Ø", "φ", "R", "C"} or re.match(r"^[ØφRCSr]\s?\d", s):
        return True
    return False


# ==============================================================================
# L0 + L1 分类改造（2026-09-27 用户授权实施）
#
# 【为什么要改】实证：p50 的 `200512` 与 `1250001` 同字体(CenturyGothic)·同字号(10.97)·同行，
#   字符形态完全同分布 —— **只看字符串这个维度不可判定**。旧规则不仅误杀真内容，它自认为
#   "对"的样本也是错的：`200512` 的同行伙伴是 `B280001`/`QHRW5`，本就是打标内容。
#
# 【L1 分类体系】d 类拆三类，把"为什么排除"写准确：
#   dim  = 线性尺寸（有量纲证据：Ø/R/C/±/°/mm/带小数）
#   meta = 图纸元信息（日期·版本·修订记录·标注序号）——不该参与匹配，但**它并不是尺寸**
#   note = 说明性文字（工艺说明词，用户拍板：即使分进图块也须舍弃）
#
# 【L0 三路上下文证据】全部取自 PDF 自带信息，每张图纸内部自举，不需要跨图纸先验：
#   ① 簇传播  ：同 (font, size) 印刷样式簇内存在「明确内容成员」→ 形态模糊成员整簇拉回 text
#   ② 标签伴随：邻近 ≤ max(45pt, 8×字号) 存在以「：」结尾或含标签词的中文串 → 该串是标签的值
#   ③ 量纲边界：≥7 位纯整数不可能是 mm 线性尺寸（那已是百公里级）→ 不许判 dim，改判 meta
#      （这条是物理先验，不是「≥7 位就豁免」的凑数名单）
# ==============================================================================

_NOTE_HINTS = ("说明", "注：", "备注", "技术要求", "注意", "此处", "放大", "详见", "见图", "比例",
               # ↓ 工艺说明词（2026-09-27 用户实证补充：「丝印」这类工艺词是说明，非打标内容）
               "丝印", "丝网", "移印", "烫金", "镭雕", "激光打标", "打标内容", "喷码", "刻字",
               "印刷", "LOGO位置", "logo位置", "图案位置",
               # ↓ 2026-09-29 真实照片复验补：A/B 作业面视图标签 + 刻印工艺说明 + 以实际生产为准类
               #   （这些被分进图块后属说明性/视图标识，非打标内容，须舍弃，否则虚增高「缺标」红）
               "刻印", "居中", "作业", "视图", "剖面", "以实际", "位置居中", "说明文字",
               "铭牌贴", "贴纸", "贴于", "粘贴")


def _is_note_text(t: str) -> bool:
    """d 类：说明性文字（用户拍板：即使分进图块也须舍弃）。"""
    s = (t or "").strip()
    if not s:
        return False
    if any(h in s for h in _NOTE_HINTS) and len(s) < 40:
        return True
    return False


# ① 日期/版本号形态（图纸元信息，不是尺寸）
_META_DATE_RE = re.compile(r"^\d{2,4}[./\-]\d{1,2}[./\-]\d{1,2}$")

# ② 强 dim 形态：必须带「量纲」证据，而不是「看着像数字」
_DIM_STRONG_RES = (
    re.compile(r"^[ØΦφ⌀]\s?\d"),                                  # 直径符号
    re.compile(r"^[RrSsCcMm][Rr]?\s?\d+(\.\d+)?$"),                # 半径/球径/倒角/螺纹 R5 C2 M8
    re.compile(r"^\d+(\.\d+)?\s?(mm|cm|m|°|′|″|%)$"),              # 显式单位
    re.compile(r"±"),                                              # 公差
    re.compile(r"^\d{1,5}\.\d{1,4}$"),                             # 线性尺寸多为小数值
    re.compile(r"^\d{1,5}\s?[xX×]\s?\d{1,5}(\s?[xX×]\s?\d{1,5})?$"),  # 乘法式尺寸 12x45
    re.compile(r"^\d{1,4}°$"),                                     # 角度
)

# ③ 「标签」判据：以「：」结尾，或含标签词 —— 用于②号上下文证据
_LABEL_WORDS = ("热线", "电话", "TEL", "FAX", "编号", "料号", "图号", "型号", "序列号",
                "二维码", "条码", "版本", "日期", "地址", "网址", "邮箱", "EMAIL", "SN")


def _is_label_text(t: str) -> bool:
    """是否为「标签」串（`服务热线：`、`下盖二维码格式：`、`内容以实际生产为准` → False）。"""
    s = (t or "").strip()
    if not s:
        return False
    if re.search(r"[：:]\s*$", s):
        return True
    u = s.upper()
    return any(w in u for w in _LABEL_WORDS)


def _is_content_shape(s: str) -> bool:
    """字符形态上**明确是打标内容**（确定性形态，不需要上下文）。"""
    if re.search(r"[\u4e00-\u9fff]{2,}", s):
        return True                                     # ≥2 连续中文
    if re.search(r"[A-Za-z]", s) and re.search(r"\d", s) and len(s) >= 4:
        return True                                     # 字母+数字混合 ≥4（型号码 B280001、QHRW5）
    if re.search(r"[-_/.]", s) and len(s) >= 5:
        # ⚠ 排除「纯数字 + 单一小数点 + 空白」的残损尺寸（OCR 噪声，实证：p67 `83. 2`）。
        #   它满足「含分隔符且 ≥5」的字面条件，但没有任何结构化编码特征。
        if re.fullmatch(r"[\d\s.]+", s):
            return False
        return True                                     # 结构化编码（400-860-1111、02-HYXC-VH01GZ）
    if re.fullmatch(r"[A-Za-z]{4,}", s):
        return True                                     # 英文词 ≥4（YORK、VRF）
    return False


class PageTextCtx:
    """整页文本层上下文 —— L0 ①②号证据的数据源。

    只在「文本层（a 类）」span 上有意义；OCR 来源（b/e 类）没有 font/精确坐标，
    因此走不到①②，只会用到③（量纲边界）与字符形态，属可接受降级（宁黄勿漏）。
    """

    def __init__(self, spans: List[Tuple[str, Any, str, float]]):
        self._spans = spans                      # [(text, fitz.Rect, font, size)]
        clusters: Dict[Tuple[str, float], List[str]] = {}
        for t, r, f, sz in spans:
            clusters.setdefault((f, sz), []).append(t)
        # 簇内只要有任一「确定性内容」成员 → 该印刷样式整体视为内容簇
        self._content_clusters = {k for k, v in clusters.items()
                                  if any(_is_content_shape(x) for x in v)}
        self._labels = [(t, r) for t, r, f, sz in spans if _is_label_text(t)]

    def in_content_cluster(self, font: Optional[str], size: Optional[float]) -> bool:
        if not font or not size:
            return False
        return (font, round(float(size), 2)) in self._content_clusters

    def near_label(self, rect: Any, size: Optional[float]) -> bool:
        """矩形窗口内是否存在标签串（用矩形窗而非圆距离，兼容「同行但较远」排版）。"""
        if rect is None:
            return False
        rad = max(45.0, 8.0 * float(size or 0))
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        for t, r in self._labels:
            ox = (r.x0 + r.x1) / 2
            oy = (r.y0 + r.y1) / 2
            if abs(ox - cx) <= rad and abs(oy - cy) <= rad:
                return True
        return False


def classify_text(t: str, ctx: Optional["PageTextCtx"] = None,
                  font: Optional[str] = None, size: Optional[float] = None,
                  rect: Any = None) -> str:
    """元素分类（L0+L1）。返回 text / dim / meta / note。

    :param ctx:  整页文本层上下文；None 时退化为纯字符规则（OCR 来源走这条路）
    :param font: span 字体名（a 类才有）
    :param size: span 字号（a 类才有）
    :param rect: span 包围盒（a 类才有）
    """
    s = (t or "").strip()
    if not s:
        return "text"

    # --- 0) 说明性文字优先（用户拍板：即使分进图块也须舍弃） ---
    if _is_note_text(s):
        return "note"

    # --- 0.5) 括号/方括号包裹的尺寸（如 `(2.6)`）：剥离外壳后走【强 dim】判据 ---
    #   【2026-09-29 真实照片复验】A/B 作业面块内 `(2.6)` 这类公差标注被括号破坏纯数字形态，
    #   旧链路误判 text → 参与匹配虚增红。剥离括号后 `2.6` 命中强 dim（带小数线性尺寸）判 non-participate。
    #   ⚠ 只用 _DIM_STRONG_RES（带量纲/小数形态），**不用**宽松的 _is_dim_text 兜底——
    #     否则会把带括号的电话号码 `(400-620-6607)` 这类纯数字/短横串也误判为 dim 而误删真实内容。
    s2 = s.strip("()[]（）【】")
    if s2 and s2 != s:
        for r in _DIM_STRONG_RES:
            if r.search(s2) or r.match(s2):
                return "dim"

    # --- 1) meta：日期 / 版本号形态（它可能只是碰巧像数字，但绝不是线性尺寸） ---
    if _META_DATE_RE.match(s):
        return "meta"

    # --- 2) 强 dim：必须带量纲证据 ---
    if any(r.search(s) or r.match(s) for r in _DIM_STRONG_RES):
        return "dim"

    # --- 2.5) 标注序号：孤立的 1~2 位纯数字（装配气泡序号 / 页码 / 修订行号）。
    #           实测风险：它们与内容同簇会被①号证据误拉回 text，但照片侧永远找不到 → 白送一个红。
    #           打标内容不会是单个 1~2 位数字，故先于上下文裁决拦截。
    if re.fullmatch(r"\d{1,2}", s):
        return "meta"

    # --- 3) 形态明确是内容 → text（无需上下文） ---
    if _is_content_shape(s):
        return "text"

    # --- 4) 形态模糊（多为纯数字）→ 交给上下文裁决 ---
    if ctx is not None:
        if ctx.in_content_cluster(font, size):
            return "text"                       # ① 簇传播
        if ctx.near_label(rect, size):
            return "text"                       # ② 标签伴随

    # --- 5) 量纲边界：≥7 位纯整数不可能是 mm 线性尺寸 → 不许判 dim，落 meta（不确定） ---
    if re.fullmatch(r"\d{7,}", s):
        return "meta"

    # --- 6) 其余（≤6 位纯数字 / 极短串）维持旧行为 ---
    return "dim" if _is_dim_text(s) else "text"


# ---- OCR 噪声判据（2026-09-27 用户实证驱动）----
# 背景：块级局部 OCR 会把图纸上的装饰纹、结构线误识别成短串（实证：p67 `RK+n 2`、
# p52 `GD`/`GYD`）。这些串被当成打标内容 → 假「有内容」→ 比对时产生**假黄**，
# 属于用户明确要求杜绝的「无内容块造成的干扰」。
# ⚠ 本判据**只作用于 OCR 来源（b/e 类）**；文本层（a 类）提取可靠，不受约束，
#    以免误杀真实打标内容（宁黄勿漏）。
_ILLEGAL_CHARS = set("+'`~*&%$#@!?<>{}[]|\\^")


def _is_valid_marking(t: str) -> bool:
    """OCR 来源文本是否算【有效打标内容】。"""
    s = (t or "").strip()
    if len(s) < 2:
        return False
    if any(c in _ILLEGAL_CHARS for c in s):
        return False                                   # 乱码特征（RK+n 2 / 2'0+88）
    if re.search(r"[\u4e00-\u9fff]{2,}", s):
        return True                                    # ≥2 连续中文
    if re.fullmatch(r"[A-Za-z]{1,3}", s):
        return False                                   # 纯字母 ≤3 位 → 噪声（GD / GYD）
    if re.search(r"[A-Za-z]", s) and re.search(r"\d", s) and len(s) >= 4:
        return True                                    # 字母+数字混合 ≥4（型号码）
    if re.fullmatch(r"\d{3,}", s):
        return True                                    # 纯数字 ≥3 位
    if re.search(r"[-_./:]", s) and len(s) >= 5:
        return True                                    # 结构化编码（02-HYXC-VH01GZ）
    if re.fullmatch(r"[A-Za-z]{4,}", s):
        return True                                    # 英文词 ≥4 位：保守保留（宁黄勿漏）
    return False


def _norm_text(s: str) -> str:
    """文本提取「采信规则」用的归一化：小写 + 去空白与常见标点（保留汉字）。
    用于把文本层原文与 OCR 结果放在同一可比集合里判定「是否同一处文本」。"""
    s = (s or "").lower()
    return re.sub(r"[\s\-.,:;()/\\|]", "", s)


def _ocr_conflicts_text_layer(tn: str, a_norm: set) -> bool:
    """采信规则核心判定：OCR 候选 tn 是否与文本层原文重叠。
    """
    if not tn:
        return True                                    # 空串视为噪声，不计入
    if tn in a_norm:
        return True
    for a in a_norm:
        if not a:
            continue
        if a in tn and len(a) >= max(3, 0.6 * len(tn)):
            return True
    return False


def _core_norm(s: str) -> str:
    """OCR 重复变体去重用的强归一化：小写 + 仅保留字母/数字/汉字（去尽所有标点、括号、空白）。
    例：`()NFC便捷控制` 与 `(NFC便捷控制` 均归为 `nfc便捷控制` → 判定为同一处文本的双检出。"""
    s = (s or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)


# ---------------- 块级局部 OCR（b/e 类，§2.2 铁律参数） ----------------

_OCR = None
_OCR_FAILED = False


def _get_ocr():
    """复用 vpdf/fallback.py 里已调优的 RapidOCR 单例。
    ⚠ 不要自己 new RapidOCR：Det 组 kwargs 必须同时带 det_model_path，
    否则 UpdateParameters 访问 det_dict['model_path'] 直接 KeyError（既有坑）。
    且 det_limit_side_len 必须保持 736（降档会用召回换时延，已实证否决）。"""
    global _OCR, _OCR_FAILED
    if _OCR is not None or _OCR_FAILED:
        return _OCR
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from fallback import _get_ocr as _fallback_get_ocr
        _OCR = _fallback_get_ocr()
    except Exception as e:
        print("  [warn] RapidOCR 不可用，b/e 类识别降级为空: %s" % e)
        _OCR_FAILED = True
    return _OCR


def ocr_block(page, rect, dpi_list=(300, 400), pad_px=64, mask_rects=None):
    """块包围盒局部高分辨率 OCR（双分辨率投票）。返回 [(text, conf)]。

    mask_rects: 文本层 span 包围盒（page 坐标系，remove_rotation 之后）列表。
        传入后在 OCR 前将其**涂白**，使 OCR 只识别曲线(转曲)文字，避免把 a 类
        文本（含不参与尺寸/说明）重复判为 b 类（修复 has_a 门禁漏提，2026-09-29）。
    """
    ocr = _get_ocr()
    if ocr is None:
        return []
    import numpy as np
    import cv2
    best: Dict[str, float] = {}
    for dpi in dpi_list:
        try:
            pix = page.get_pixmap(dpi=dpi, clip=rect)
        except Exception:
            continue
        # ⚠ 不要用 cv2.imdecode(pix.samples)：对 PyMuPDF 原始样本返回 None，
        #    异常又被下面 try 吞掉 → 表现为「OCR 静默 0 检出」，极难排查。
        #    直接按 (h, w, n) reshape 才可靠（fitz 颜色序为 RGB）。
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n >= 3:
            img = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY if pix.n == 3 else cv2.COLOR_RGBA2GRAY)
        else:
            img = arr[:, :, 0] if arr.ndim == 3 else arr
        if img is None or img.size == 0:
            continue
        # 涂白文本层 span：避免 a 类文本被 OCR 重复识别为 b 类曲线文字
        if mask_rects:
            scale = float(dpi) / 72.0
            for mr in mask_rects:
                m = mr & rect
                if not m or m.is_empty:
                    continue
                ix0 = max(0, int(round((m.x0 - rect.x0) * scale)))
                iy0 = max(0, int(round((m.y0 - rect.y0) * scale)))
                ix1 = min(img.shape[1], int(round((m.x1 - rect.x0) * scale)))
                iy1 = min(img.shape[0], int(round((m.y1 - rect.y0) * scale)))
                if ix1 > ix0 and iy1 > iy0:
                    img[iy0:iy1, ix0:ix1] = 255
        # RapidOCR 对极端长宽比条带 0 检出 → 四周补白边
        img = cv2.copyMakeBorder(img, pad_px, pad_px, pad_px, pad_px,
                                 cv2.BORDER_CONSTANT, value=255)
        try:
            res, _ = ocr(img)
        except Exception:
            continue
        for item in (res or []):
            txt = (item[1] or "").strip()
            conf = float(item[2]) if len(item) > 2 else 0.0
            if not txt:
                continue
            if txt not in best or conf > best[txt]:
                best[txt] = conf
    return [(k, v) for k, v in best.items()]


# ---------------- view_hint 推断（§3.1，10 值） ----------------

def _quadrant_rescan(page, bb, W, H, mask_rects=None):
    """2x2 象限高分辨率补扫（象限间重叠 20%）。
    【2026-09-27 实证驱动】p67-P2 的 YORK logo（4 位字母）在整块 300/400dpi 下
    漏检（只出 VRF 0.57），但右下角小区域 300dpi 即可检出（0.72）。
    触发条件：整块 OCR 无有效内容（将判空）→ 补扫保召回，避免误杀小 logo 部位。
    ⚠ 这是在【保召回】而非降参数换时延，不违反既有铁律；只对将判空的块触发，
      每块仅 4 次小区域 600dpi OCR，开销可控（离线建库阶段）。
    mask_rects: 同 ocr_block，补扫前同样涂白文本层 span，避免重复检出 a 类。"""
    x0, y0, x1, y1 = bb
    w, h = x1 - x0, y1 - y0
    ow, oh = w * 0.6, h * 0.6
    quads = [
        fitz.Rect(x0, y0, x0 + ow, y0 + oh),                       # 左上
        fitz.Rect(x1 - ow, y0, x1, y0 + oh),                       # 右上
        fitz.Rect(x0, y1 - oh, x0 + ow, y1),                       # 左下
        fitz.Rect(x1 - ow, y1 - oh, x1, y1),                       # 右下
    ]
    seen: set = set()
    out = []
    for q in quads:
        q = fitz.Rect(max(0, q.x0), max(0, q.y0), min(W, q.x1), min(H, q.y1))
        if q.is_empty or q.get_area() < 50:
            continue
        for txt, conf in ocr_block(page, q, dpi_list=(600,), mask_rects=mask_rects):
            if txt not in seen:
                seen.add(txt)
                out.append((txt, conf))
    return out


def infer_view_hint(texts: List[str], w: float, h: float, area_pct: float,
                    n_img: int, n_draw: int) -> str:
    blob = " ".join(texts)
    ratio = (w / h) if h else 1.0
    if re.search(r"\b[RrCcUu]\s?\d{1,4}\b", blob) and re.search(r"\d{3,}", blob) and n_draw > 200:
        return "PcbBoard"
    if n_img > 0 and not texts:
        return "QrFace"
    if re.search(r"二维码|QR", blob):
        return "QrFace"
    if re.search(r"服务热线|热线|400-|400\d", blob) and area_pct < 6.0:
        return "Nameplate"
    if re.search(r"顶视|上盖|顶面", blob):
        return "TopView"
    if re.search(r"底视|底座|下盖|底面", blob):
        return "Bottom"
    if re.search(r"背视|接线|端子|DC\s?\d|LOW VOLTAGE|禁止强电", blob, re.I):
        return "Back"
    if re.search(r"侧视|侧面", blob):
        return "Side"
    if ratio < 0.35 or ratio > 2.8:
        return "Side"
    if area_pct >= 7.0:
        return "Front"
    return "Unspecified"


# ---------------- 元素提取（PDF 只读，全局归一化坐标） ----------------

def _elements_in_block(page, bb, W, H, img_rects, ocr_on=True, meta: Optional[Dict] = None,
                        ctx: Optional["PageTextCtx"] = None):
    """返回块内元素列表（kind / text / conf / bbox_norm / participate / source）。
    meta（可选）回填审计信息：ocr_raw = 局部 OCR 原始检出条数（含被判为 d 类的）。"""
    els: List[Dict[str, Any]] = []
    meta = meta if meta is not None else {}
    meta.setdefault("ocr_raw", 0)
    rect = fitz.Rect(*bb)
    # ⚠ 2026-09-29：块级 OCR 区域（含 6pt 内边距）；同时收集文本层 span 包围盒，
    #    供 OCR 前涂白（mask_rects），避免把 a 类文本重复识别为 b 类曲线文字
    #    （修复 has_a 门禁致转曲打标条整条漏提）。
    pad = 6.0
    orect = fitz.Rect(max(0, bb[0] - pad), max(0, bb[1] - pad),
                      min(W, bb[2] + pad), min(H, bb[3] + pad))
    mask_rects: List[Any] = []

    # a/d 类：文本层
    try:
        d = page.get_text("dict")
    except Exception:
        d = {"blocks": []}
    for b in d.get("blocks", []):
        for ln in b.get("lines", []) or []:
            for sp in ln.get("spans", []) or []:
                txt = (sp.get("text") or "").strip()
                if not txt:
                    continue
                r = fitz.Rect(*sp["bbox"])
                if not (r & orect).is_empty:
                    mask_rects.append(r)        # 文本层 span：供 OCR 前涂白
                if not (r & rect).is_empty and (r & rect).get_area() >= 0.5 * r.get_area():
                    # L0+L1：a 类有 font/size/rect，走「形态 + 上下文三证据」完整判据
                    kind = classify_text(txt, ctx,
                                         font=sp.get("font"), size=sp.get("size"), rect=r)
                    els.append({
                        "kind": kind, "text": txt, "ocr_conf": 1.0,
                        "bbox_norm": [round(sp["bbox"][0] / W, 6), round(sp["bbox"][1] / H, 6),
                                      round((sp["bbox"][2] - sp["bbox"][0]) / W, 6),
                                      round((sp["bbox"][3] - sp["bbox"][1]) / H, 6)],
                        "participate": 1 if kind == "text" else 0,
                        "source": "text_layer",
                    })

    # c 类：图标 / QR（只记位置，不提取内容）
    for ir in img_rects:
        if (ir & rect).is_empty:
            continue
        inter = ir & rect
        if inter.get_area() < 0.5 * ir.get_area():
            continue
        w, h = ir.width, ir.height
        square = 0.75 <= (w / h if h else 1) <= 1.33
        els.append({
            "kind": "qr" if square else "icon", "text": "", "ocr_conf": None,
            "bbox_norm": [round(ir.x0 / W, 6), round(ir.y0 / H, 6),
                          round(w / W, 6), round(h / H, 6)],
            "participate": 0,            # c 类：标示走灰，但**计入 has_marking**
            "source": "image",
        })

    # b/e 类：块级局部高分辨率 OCR（§2.2 铁律：文本层空 ≠ 无打标）
    # ⚠ 2026-09-29 修复：取消 `has_a` 门禁（原「块内有参与文本即跳过整块 OCR」
    #    导致转曲打标条整条漏提：63 B1/B3、66 B0/B4、67 B2 实证）。
    #    现改为 ocr_on 时一律尝试 OCR，但【屏蔽文本层 span（mask_rects）】+
    #    【文本层原文去重（_ocr_conflicts_text_layer）】双保险，确保：
    #      ▶ 文本层(a 类，含被判 d 类的尺寸/说明)为【权威基准】，一律保留、永不被改写；
    #      ▶ OCR 仅作补充——凡与文本层原文重叠的 OCR 结果一律丢弃，绝不覆盖/替换文本层；
    #      ▶ 仅文本层确实无此内容的部位，OCR 结果才作为 curve_text 入块。
    #    噪声仍由 _is_valid_marking 拦截（§2.2）。整块无有效内容时仍走两级象限补扫（§2.2-⑦）。
    # —— 采信规则：收集文本层原文归一化集合，作为 OCR 去重的权威基准 ——
    a_norm: set = set()
    for e in els:
        if e.get("source") == "text_layer" and e.get("text"):
            a_norm.add(_norm_text(e["text"]))
    if ocr_on:
        for txt, conf in ocr_block(page, orect, mask_rects=mask_rects):
            meta["ocr_raw"] += 1          # 审计：OCR 有检出（即便随后被判为 d 类）
            # OCR 源无 font/坐标 → ctx=None，只用字符形态 + ③量纲边界（宁黄勿漏）
            kind = classify_text(txt)
            if kind != "text":
                continue                  # OCR 出的尺寸/说明同样归 d 类
            if not _is_valid_marking(txt):
                # OCR 噪声串（§2.2）：不计入打标内容，避免后期比对产生假黄
                meta["ocr_noise"] = meta.get("ocr_noise", 0) + 1
                continue
            if _ocr_conflicts_text_layer(_norm_text(txt), a_norm):
                # 采信规则：与文本层原文重叠 → 丢弃 OCR 副本，文本提取严格优先
                meta["ocr_dup_text"] = meta.get("ocr_dup_text", 0) + 1
                continue
            els.append({
                "kind": "curve_text", "text": txt, "ocr_conf": round(float(conf), 3),
                "bbox_norm": None,        # OCR 结果暂无精确落点（后续 P4 定位再补）
                "participate": 1,
                "source": "outline_ocr",
            })
        # 两级 OCR（§2.2-⑦）：整块无有效内容 → 2x2 象限 600dpi 补扫，防小 logo 漏检误杀
        if not any(e["kind"] == "curve_text" for e in els) and \
                not any(e["kind"] in ("qr", "icon") for e in els):
            for txt, conf in _quadrant_rescan(page, bb, W, H, mask_rects=mask_rects):
                meta["ocr_raw"] += 1
                meta["quad_rescan"] = 1
                kind = classify_text(txt)
                if kind != "text":
                    continue
                if not _is_valid_marking(txt):
                    meta["ocr_noise"] = meta.get("ocr_noise", 0) + 1
                    continue
                if _ocr_conflicts_text_layer(_norm_text(txt), a_norm):
                    meta["ocr_dup_text"] = meta.get("ocr_dup_text", 0) + 1
                    continue
                els.append({
                    "kind": "curve_text", "text": txt, "ocr_conf": round(float(conf), 3),
                    "bbox_norm": None,
                    "participate": 1,
                    "source": "outline_ocr_quad",   # 补扫来源（审计用）
                })

    # 同区 OCR 重复变体去重（2026-09-29 真实照片复验驱动）：整块 OCR 对同一物理条码/标签
    #   常双检出为两条「归一化完全相同」的读法（如 `()NFC便捷控制` 与 `(NFC便捷控制`），
    #   若都保留会虚增同色计数。此处仅在「_core_norm 完全相同」时合并，保留高置信一条；
    #   ⚠ 不合并仅单字符差异（如 YCWA15NCWQ↔YCWA15NCWO 的 Q/O）——那类在 BlockTextMatcher
    #     选块阶段用「块内变体中和」处理，避免在此处误删正确读法（提取期无照片上下文）。
    ct_idx = [i for i in range(len(els)) if els[i].get("kind") == "curve_text"]
    cores = [_core_norm(els[i].get("text", "")) for i in ct_idx]
    drop = set()
    for x in range(len(ct_idx)):
        if ct_idx[x] in drop:
            continue
        for y in range(x + 1, len(ct_idx)):
            if ct_idx[y] in drop:
                continue
            if cores[x] and cores[x] == cores[y]:
                xi, yi = ct_idx[x], ct_idx[y]
                if (els[xi].get("ocr_conf") or 0) >= (els[yi].get("ocr_conf") or 0):
                    drop.add(yi)
                else:
                    drop.add(xi)
    if drop:
        meta["ocr_dup_variant"] = len(drop)
        els = [cur for i, cur in enumerate(els) if i not in drop]

    return els


# ---------------- 主入口 ----------------

def run_separation(pdf_path: str) -> Dict[str, Any]:
    """调用 v8.4 部位分离（只读），返回其 report.json。"""
    if not os.path.exists(SEPARATE):
        raise FileNotFoundError("部位分离脚本缺失: %s" % SEPARATE)
    proc = subprocess.run([PY, SEPARATE, "--pdf=%s" % pdf_path],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"REPORT=(\S+)", proc.stdout or "")
    if not m:
        raise RuntimeError("部位分离未产出 REPORT:\n%s\n%s" % (proc.stdout[-2000:], proc.stderr[-2000:]))
    with open(m.group(1), encoding="utf-8") as f:
        return json.load(f)


def extract_logical_blocks(pdf_path: str, page_index: int = 0,
                           ocr_on: bool = True) -> Dict[str, Any]:
    """产出 `logical-blocks/1` 契约。"""
    if fitz is None:
        raise RuntimeError("PyMuPDF(fitz) 不可用")
    rep = run_separation(pdf_path)
    pg = rep["pages"][page_index]

    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        page.remove_rotation()
        W, H = page.rect.width, page.rect.height
        img_rects = []
        try:
            for info in page.get_image_info():
                bb = info.get("bbox")
                if bb and len(bb) == 4:
                    img_rects.append(fitz.Rect(*[float(v) for v in bb[:4]]))
        except Exception:
            pass
        n_draw = len(page.get_drawings())
        # L0 数据源：整页文本层 span（与 _elements_in_block 同一坐标系，均在 remove_rotation 之后）
        ctx_spans: List[Tuple[str, Any, str, float]] = []
        try:
            _pd = page.get_text("dict")
        except Exception:
            _pd = {"blocks": []}
        for _b in _pd.get("blocks", []) or []:
            for _ln in (_b.get("lines") or []):
                for _sp in (_ln.get("spans") or []):
                    _t = (_sp.get("text") or "").strip()
                    if _t:
                        ctx_spans.append((_t, fitz.Rect(*_sp["bbox"]),
                                          _sp.get("font") or "", round(float(_sp.get("size") or 0), 2)))
        ctx = PageTextCtx(ctx_spans)


        blocks_out = []
        name_seen: Dict[str, int] = {}
        for b in pg["blocks"]:
            bb = b["bbox_pt"]
            meta: Dict[str, Any] = {}
            els = _elements_in_block(page, bb, W, H, img_rects, ocr_on=ocr_on, meta=meta, ctx=ctx)
            # 阅读序：从上到下、从左到右
            parts = [e for e in els if e["participate"] == 1 and e["text"]]
            parts.sort(key=lambda e: (e["bbox_norm"][1] if e["bbox_norm"] else 9,
                                      e["bbox_norm"][0] if e["bbox_norm"] else 9))
            texts = [e["text"] for e in parts]

            n_img = sum(1 for e in els if e["kind"] in ("qr", "icon"))
            area_pct = b.get("area_pct", 0.0)
            vh = infer_view_hint(texts, bb[2] - bb[0], bb[3] - bb[1], area_pct, n_img, n_draw)

            # has_marking（§2.2）：a/b/e 参与 + c 类（QR/图标算打标内容）
            has_c = n_img > 0
            has_marking = 1 if (parts or has_c) else 0
            empty_reason = None
            if not has_marking:
                # 审计口径：OCR 有检出但全是尺寸/说明 → all_dim_note；
                # OCR 检出全为噪声串 → ocr_noise_only；
                # 文本层与 OCR 皆空 → ocr_empty；未开 OCR → no_text_layer
                _NON_MARKING = ("dim", "note", "meta")   # L1：meta 与 dim/note 同样不算打标内容
                if meta.get("ocr_noise", 0) > 0 and not any(
                        e["kind"] in _NON_MARKING for e in els):
                    empty_reason = "ocr_noise_only"
                elif any(e["kind"] in _NON_MARKING for e in els) or meta.get("ocr_raw", 0) > 0:
                    empty_reason = "all_dim_note"
                else:
                    empty_reason = "ocr_empty" if ocr_on else "no_text_layer"

            low_conf = 0
            if parts and any((e.get("ocr_conf") or 1.0) < LOW_CONF for e in parts):
                low_conf = 1

            name = build_name(texts, vh) if has_marking else ""
            if name:
                seq = name_seen.get(name, 0)
                if seq:
                    name = build_name(texts, vh, dup_seq=seq)
                name_seen[name] = name_seen.get(name, 0) + 1

            blocks_out.append({
                "block_index": b["idx"],
                "bbox_pt": [round(float(v), 2) for v in bb],
                "bbox_norm": [round(float(v), 6) for v in b["bbox_norm"]],
                "area_pct": round(float(area_pct), 3),
                "view_hint": vh,
                "name": name,
                "name_fp": normalize(name),
                "has_marking": has_marking,
                "empty_reason": empty_reason,
                "low_conf": low_conf,
                "n_elements": len(els),
                "n_participate": len(parts),
                "n_image": n_img,
                "elements": els,
            })
    finally:
        doc.close()

    return {
        "schema": SCHEMA,
        "algo_version": ALGO,
        "source_pdf": pdf_path,
        "source_sha256": rep.get("source_sha256_before"),
        "source_intact": rep.get("source_intact"),
        "page_index": page_index,
        "page": {"width": pg["width"], "height": pg["height"], "source": pg.get("source")},
        "n_blocks": len(blocks_out),
        "n_has_marking": sum(1 for b in blocks_out if b["has_marking"]),
        "blocks": blocks_out,
    }


def main(argv: List[str]) -> int:
    pid = None
    pdf = None
    out = None
    no_ocr = False
    json_only = False
    # 同时支持【等号形式 --pdf=】与【分离形式 --pdf <path>】：
    # C# 侧 V2Python 用 ProcessStartInfo.ArgumentList 传参（自动处理空格/引号），
    # 传的是分离形式，若只认等号形式会静默走到 usage 分支并退出码 2。
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a == "--pid" and nxt is not None:
            pid = int(nxt); i += 2; continue
        if a == "--pdf" and nxt is not None:
            pdf = nxt; i += 2; continue
        if a == "--json-out" and nxt is not None:
            out = nxt; i += 2; continue
        if a.startswith("--pid="):
            pid = int(a.split("=")[1])
        elif a.startswith("--pdf="):
            pdf = a.split("=", 1)[1]
        elif a.startswith("--json-out="):
            out = a.split("=", 1)[1]
        elif a == "--no-ocr":
            no_ocr = True
        elif a == "--json-only":
            json_only = True
        i += 1

    if not pdf and pid:
        import sqlite3
        con = sqlite3.connect(os.path.join(PROJECT, "data", "drawingsv2.db"))
        row = con.execute("select pdf_path from v2_drawing_profiles where id=?", (pid,)).fetchone()
        con.close()
        if not row:
            print("profile %d 不存在" % pid)
            return 2
        pdf = row[0]

    if not pdf:
        print("用法: python logical_blocks.py --pdf=<pdf> | --pid=<id>  [--json-out=<path>] [--no-ocr]")
        return 2

    res = extract_logical_blocks(pdf, ocr_on=not no_ocr)
    if json_only:
        # C# 侧 V2Python.RunAsync 用 ExtractJson 切「首个 { 到最后 }」，
        # 若再打印摘要行会被包进 JSON 区间 → 必须独占 stdout。
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if out:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with io.open(out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print("WROTE=%s" % out)
    print("块数=%d  有打标=%d  空块=%d" % (res["n_blocks"], res["n_has_marking"],
                                          res["n_blocks"] - res["n_has_marking"]))
    for b in res["blocks"]:
        print("  P%-2d %-11s has_marking=%d low_conf=%d 元素=%d(参与%d) %s%s"
              % (b["block_index"] + 1, b["view_hint"], b["has_marking"], b["low_conf"],
                 b["n_elements"], b["n_participate"],
                 (b["name"][:60] if b["name"] else "(空块)"),
                 ("  ← " + b["empty_reason"]) if b["empty_reason"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
