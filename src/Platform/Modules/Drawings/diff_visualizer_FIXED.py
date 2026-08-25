# -*- coding: utf-8 -*-
"""
diff_visualizer.py v2 —— 文本级差异标记（已重写）
=================================================
给定照片 + 匹配图块，用 OCR bbox 逐段对比文本，
在图上标记不同位置。

版本: v19.28 (2026-07-31) —— ⭐ 日期码误灰化修复（接 v19.27 图标修复）：_clean_qr_garbage 的 \d{6} 规则在 v19.26 序列号阈值提升(6→8)后误杀独立日期码(200512 等)为空串→step3/step4 自动灰化。修复：(1)仅当文本长度>6 时执行 \d{6} 剥离（嵌入行内才清，独立日期码保留）；(2)清理后若为空但原文本纯数字则回退原文。影响 24 处日期码/数字标记恢复正确匹配。保留 v19.27 图标修复 + v19.26 序列号 + v19.25 QR存在性 + v19.24 序列号规则。
版本: v19.16 (2026-07-30) —— ⭐ 根因修复：二维码垃圾被 OCR 拼进正常行(如'服务热线400-860-1111NNFC')导致假红/黄框。新增 _clean_qr_garbage() 在匹配前剥离行内嵌入的二维码 OCR 垃圾(双N的NFC/S'Oft/QR解码码/日期码)，step3/step4 对称接入；_is_qr_ocr_noise 新增双N的NFC(NNFC)捕获。全图纸通用，不依赖特定产品。v19.15 zxing-cpp 照片QR解码 + LRU缓存保留。
版本: v19.11 (2026-07-29 14:02) —— 禁止强电/地暖阀等打标文字取消灰化纳入比对；DC15/24V等电压规格不再误判序列号；v19.10 电话号码/前导CJK/ol修复保留
v19.8 (2026-07-29 10:14) —— _auto_roi邻近簇合并修复(地暖阀等同面板分离标签区不再被ROI裁掉)；v19.7 OCR清理/CJK容错保留
v19.1 (2026-07-28 17:00) —— 新增序列号/批次码统一跳过规则(全图纸适用)
v16 (2026-07-28) —— 统一双向位置匹配、阈值0.25、红框标示

⭐ 核心设计原则（2026-07-28 正式写入，后续版本必须遵守）：
  ┌────────────────────────────────────────────────────────────┐
  │ 1. 文本差异化是重点                                         │
  │    红=缺标 / 黄=多标 / 绿=一致 / 灰=CAD·LCD·图标内噪声跳过  │
  │    四色框详细标示在输出图上，是质检操作员的核心关注点。       │
  │                                                            │
  │ 2. 图标/二维码不识别内容，只看「当前位置」有没有             │
  │    都有或都没有 → 不标示；只有一方有 → 红框标示（图块相应位置）│
  │    缺图标(块有照片无)→iconMissingRegions；多图标(照片有块无)  │
  │    →iconExtraRegions；两者都在图块上画红框，所有图纸统一适用。 │
  └────────────────────────────────────────────────────────────┘

算法：
  1. 双方 OCR（带 bbox）：照片文本行 vs 图块文本行
  2. 逐行对比：照片某行文本是否在图块中出现？
     - 是 → 匹配（绿色框）
     - 否 → 黄色框（产品有多余文本）
  3. 逐行对比：图块某行文本是否在照片中出现？
     - 是 → 匹配
     - 否 → 检查是否是 CAD 标注 / LCD 屏显示例 / 图标内噪声
       - 是 → 灰色半透明框（跳过，非产品打标差异）
       - 否 → 红色框（产品缺少该文本，真差异）
  4. 图标：仅存在性确认（有/无），不上报为 red/yellow Regions

调用：
  python diff_visualizer.py <photo_path> <block_path> <output_path> [block_tokens_file]

输出（stdout，最后一行 JSON）：
  {"success":true, "redRegions":[{bbox...}], "yellowRegions":[{bbox...}],
   "grayRegions":[{bbox...}], "greenRegions":[{bbox...}],
   "iconMissingRegions":[...], "iconExtraRegions":[...],
   "photoExclusive":["..."], "blockExclusive":["..."], "cadOnly":["..."]}
"""
import sys, os, re, json, traceback
from PIL import Image, ImageDraw


# ── CAD 检测（复刻 C# 的 DimLineRegex + PartLabelStopwords）──
CAD_STOPWORDS = {
    "下盖", "上盖", "上端", "下端", "左端", "右端", "顶部", "底部",
    "背面", "正面", "左侧", "右侧", "侧面", "技术要求", "序号",
    "名称", "材料", "备注", "比例", "单位", "图号", "设计", "审核",
    "批准", "制图", "共", "第", "页", "日期", "版本", "数量", "重量",
    "未注", "公差", "粗糙度", "标记", "处数", "阶段", "更改", "签名",
    "质量", "标准", "说明", "检验", "审查", "型号", "规格", "项目",
    "单位mm", "文件编号", "图纸编号", "物料编码", "发放部门",
    "采购部", "市场部", "技术部", "物控部", "资材部", "生产部",
    "校对", "标准化", "国产化", "镭雕", "激光", "雕刻",
    "BOM", "bom",
    # ⭐ v19 新增：工序/作业类 CAD 标注（非产品打标文字）
    "作业", "工序", "步骤", "工艺",
}

# ── LCD 屏显内容词表（非打标永久文字，是动态显示内容）──
# 这些词出现在 CAD 图纸的 LCD 显示区域示例中，但实物产品不会激光雕刻
LCD_DISPLAY_WORDS = {
    # 常见 LCD 状态词（温控器/线控器面板）
    "设定", "室温", "自动", "运行", "温度", "时间", "时钟",
    "服务", "地址", "系统", "小时", "模式冲突",
    # ⭐ v19.6 补充：实际生产中出现的 LCD 屏显词（来自 did=124 blk3 OCR 实测）
    "预热", "防翠", "无网", "无网感",  # 预热中/无网感等状态
    "点检", "增址", "点检",              # 服务点检/系统增址
    "制冷", "制热", "送风", "除湿",      # 运行模式
    "风速", "静音", "定时", "预约",      # 常见设置项
    "童锁", "屏显", "背光", "室内", "VP",  # 面板元素
    "YORK",                               # LCD 区域品牌水印（非打标文字）
}
# ⭐ v19.6 LCD 子串匹配：OCR 常把相邻字符合并（如"高家盈力无网威"含"无网"）
LCD_DISPLAY_SUBSTRINGS = [
    "无网", "预热", "防翠", "点检", "增址", "模式冲突",
]
LCD_ANNOTATION_PATTERNS = [
    r"试运行", r"热启动", r"点检",  # 技术注释关键词
    r"\d+\.\d{2,}",                  # 如 8.888（多位小数 = LCD 数值）
    r"^\d{2,}\.$",                   # 如 "88."（数字+点 = LCD）
]

def _is_dimension(text):
    """检测是否为尺寸标注行（CAD 规范）
    改进：6位纯数字（如 200512 日期码）不是尺寸
    """
    if not text: return False
    t = text.strip()
    if not t: return False
    # ⭐ v19: 先去除数字内部的空格（OCR 常在数字间插入空格，如 "80. 5" → "80.5"）
    #   也处理小数点后空格（"80. 5"、"3. 8" 等）
    t_clean = re.sub(r'(\d)\s+(\d)', r'\1\2', t)
    t_clean = re.sub(r'(\.)\s+(\d)', r'\1\2', t_clean)
    # 强尺寸符号 ± Φ ⌀ Ø × / 以及 +/- 变体（OCR 可能把 ± 读成 +/-）
    if re.search(r'[±×xXΦ⌀Ø]|/\+\-/', t): return True
    # 短纯数字（1-3 位）：典型尺寸（用 t_clean 处理 OCR 空格）
    if re.match(r'^\d{1,3}$', t_clean): return True
    # 短数字+单位（限制总长≤5位，防止 200512 这类日期码/序列号误判为尺寸）
    if re.match(r'^\d{1,3}(\.\d{0,2})?\s*(mm|cm|kg|M|m)?$', t_clean) and len(t_clean) <= 5: return True
    # CAD 测量值格式：5'0+8 / 1'2+4 / 0'-10 等（含 ' 和 + 的混合）
    if re.search(r"[']\d", t) and re.search(r'[+\-]\d', t): return True
    # 螺纹标注 M2 M3
    if re.match(r'^M\d+(\.\d+)?$', t): return True
    # 括号内数值 (0.5)
    if re.match(r'^\(.+\)$', t): return True
    # 角度 30°/45°
    if '°' in t: return True
    # 范围式 10-20 / 10~20
    if re.match(r'^\d+\s*[-~]\s*\d+', t): return True
    return False


def _is_cjk_garbage(text):
    """⭐ v19.9 新增：检测短 CJK OCR 垃圾文本。

    切图/照片 OCR 在纯色背景、LCD 屏幕区域、或低对比度区域
    经常输出 3-5 个无意义 CJK 字符组合（如「热中防零」「自清浩」「显目图目目」）。
    这些不是产品打标文字，不应参与匹配（既不标黄也不标红）。
    判定条件（需同时满足）：
      1. 纯 CJK 或 CJK+极少量 ASCII（ASCII占比 < 20%）
      2. 长度 3-6 字符
      3. 不含任何已知 LCD 屏显词/注释词/停用词的子串
      4. 不含常见有语义的 2 字 CJK 组合（如 制冷/制热/风速/模式 等）
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    length = len(t)
    if length < 3 or length > 6:
        return False
    # CJK 占比检查
    cjk_count = sum(1 for c in t if '\u4e00' <= c <= '\u9fff')
    ascii_count = sum(1 for c in t if c.isascii())
    if cjk_count == 0:
        return False
    if ascii_count > 0 and ascii_count / length >= 0.2:
        return False
    # 排除：包含已知 LCD/注释词
    known_semantic_words = set()
    for w in LCD_DISPLAY_WORDS:
        known_semantic_words.add(w)
    for sub in LCD_DISPLAY_SUBSTRINGS:
        known_semantic_words.add(sub)
    for w in CAD_STOPWORDS:
        known_semantic_words.add(w)
    # 注释词表
    annotation_words = [
        "此", "仅", "镭雕", "雕刻", "激光", "标识", "标记", "注意", "备注",
        "范围", "按", "参考", "必须", "应", "参照", "以", "上下", "上端",
        "下端", "左端", "右端", "顶部", "底部", "背面", "正面",
        "粘贴", "贴纸", "为准", "实际生产", "生产",
        # ⭐ v19.9: 二维码标签格式说明文字（来自 did=132 blk6 等标签说明块）
        "流水号", "厂商制造", "供应商", "编码", "代码", "产期",
        "二维码", "标签", "格式", "激光打标", "盖底",
    ]
    for w in annotation_words:
        known_semantic_words.add(w)
        if w in t:
            return False  # 含已知语义词 → 不是垃圾，走对应规则
    # 检查是否含有任何已知语义子串（≥2字）
    for word in known_semantic_words:
        if len(word) >= 2 and word in t:
            return False
    # 常见有语义的 2 字 CJK 组合白名单（温控器面板常见功能词）
    meaningful_bigrams = {
        "制冷", "制热", "送风", "除湿", "风速", "静音", "定时", "预约",
        "童锁", "屏显", "背光", "室内", "设定", "室温", "自动", "运行",
        "温度", "时间", "时钟", "服务", "地址", "系统", "小时", "模式",
        "冲突", "预热", "点检", "增址", "开地", "试运", "转应急",
        "风量", "功能", "风向", "睡眠", "辅热", "清洁", "超远",
        "健康", "左右", "节能", "森林", "警报", "除霜", "滤网",
        "无人", "电能", "热启", "集中", "控制",
        # ⭐ v19.11 产品安全/规格/部件打标文字（用户明确要求纳入比对）
        "禁止", "强电", "低压", "高压", "接地", "绝缘",
        "地暖", "阀门", "水阀", "风阀",
        "警告", "注意", "小心", "危险",
    }
    for bg in meaningful_bigrams:
        if bg in t:
            return False
    # 所有检查通过 → 判定为 CJK OCR 垃圾
    return True


def _is_annotation_note(text):
    """检测是否为技术性注释（不是产品标识，也不应标为产品缺失）
    例如：仅JQ国产化、镭雕此标识、技术要求

    ⭐ v19.9 扩展：覆盖二维码标签格式说明文字
    （如「盖底部二维码标签激光打标格式」「流水号」「厂商制造编码」等）
    """
    if not text: return False
    t = text
    # 包含 CAD 停用词 → 注释
    for w in CAD_STOPWORDS:
        if w in t: return True
    # 包含 "标识/标记/注意/备注/此/仅/范围/按/参考/不包/必须/应" 等中文 CAD 注释词
    for w in ["此", "仅", "镭雕", "雕刻", "激光", "标识", "标记", "注意", "备注",
              "范围", "按", "参考", "必须", "应", "参照", "以", "上下", "上端",
              "下端", "左端", "右端", "顶部", "底部", "背面", "正面",
              # ⭐ v19.6 补充：生产环境实际出现的注释文字（来自 did=124 blk1/blk4）
              "粘贴", "贴纸", "为准", "实际生产", "生产",
              # ⭐ v19.9 补充：二维码标签格式说明文字（did=132 blk6 等标签说明块）
              # 这些是描述二维码标签内容的说明性文字，不是产品打标标识本身
              "流水号", "厂商制造", "供应商", "编码", "代码", "产期",
              "二维码", "标签", "格式", "盖底"]:
        if w in t: return True
    return False


def _is_lcd_display_text(text):
    """检测是否为 LCD 屏显动态内容（非激光打标永久文字）。
    CAD 图纸常在 LCD 区域画示例文字（设定/室温/88./自动等），
    实物产品这些内容是动态显示显示的，不应作为「缺标」判定。

    排除条件（有这些特征 = 是打标文字，不是 LCD）：
      - 含型号前缀（PC-P, P1H, QHR 等）
      - 含"服务热线"
      - 同时含字母+数字+中文（如 PC-P1HJQ服务热线：4008601111）
    """
    if not text: return False
    t = text.strip()
    if not t: return False
    # ⭐ 排除明显是打标标识的文字（型号/热线/编码）
    import re
    # 型号前缀模式
    if re.search(r'(PC-|P1H|P1V|QHR|HV[QW])', t, re.IGNORECASE): return False
    if '服务热线' in t: return False
    # 同时含字母+数字+中文 → 打标文字（如 PC-P1HJQ服务热线：4008601111）
    has_alpha = bool(re.search(r'[a-zA-Z]', t))
    has_digit = bool(re.search(r'\d', t))
    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', t))
    if has_alpha and has_digit and has_cjk: return False

    # 1. 精确匹配 LCD 屏显词表
    if t in LCD_DISPLAY_WORDS:
        return True
    for w in LCD_DISPLAY_WORDS:
        if w in t: return True
    # ⭐ v19.6 额外子串匹配：OCR 合并错串兜底（如"高家盈力无网威"→命中"无网"）
    for sub in LCD_DISPLAY_SUBSTRINGS:
        if sub in t: return True
    # 2. LCD 注释模式（试运行/热启动/点检 等）
    for pat in LCD_ANNOTATION_PATTERNS:
        if re.search(pat, t): return True
    # 3. 数字+点特征（如 "88." "8.888" "23." — 典型 LCD 数值显示）
    #    排除正常尺寸标注（已有 ±/° 等符号的会被 _is_dimension 拦截）
    if re.match(r'^\d+\.\d+$', t) and len(t) >= 3:
        return True
    if re.match(r'^\d{2,}\.$', t):
        return True
    # 4. ⭐ 2026-07-28 修正：删除「纯数字(≥4位)一律视为 LCD 屏显」规则。
    #    生产日期码/批次号（如 200512 / 20015 / 28xxxx）是产品永久激光文字，
    #    必须走正常绿/红/黄文本匹配，不得灰化跳过。
    #    仅保留 #3 的小数点 LCD 数值（"88."/"8.888"）作为动态屏显判定——
    #    那才是 CAD 图纸里的 LCD 示例文字，实物不会雕刻。
    return False


def _is_serial_number(text):
    """⭐ v19.1 新增；v19.17 扩展 5 字符短格式：检测是否为序列号/批次码（每台产品不同，不应作为打标比对依据）。

    序列号特征（基于全量 27 张图纸 132 个块的实际数据统计 + v19.17 实拍反馈）：
    - B-code 格式：B + 5-6 位数字（如 B280001, BFFC001, BFFO001）
    - 纯数字日期码：6-10 位纯数字（如 200512, 1250001, 2005120635）
    - 混合格式：1-4 字母 + 5+ 数字，数字占比 ≥40%（如 B280C01）
    - ⭐ v19.17 新增：5 字母+数字混合码（如 QHRW6）

    排除（不视为序列号）：
    - 型号名：YCTA113CGQ, PC-P1HVQA, QHRLA 等（字母占比高或有语义，通常 >8 字符）
    - 接口规格：RS485, RS485-1/2/3
    - 电源参数：220V-50Hz, 400-860-1111
    """
    if not text: return False
    t = text.strip()
    if len(t) < 5: return False

    # 1) 纯数字 8+ 位 → 长序列号/批次号（如 2005120635）
    #    ⭐ v19.25 修正：原阈值 6+ 误判日期码 200512（批次码，照片↔块相同应绿框）。
    #    真实序列号在本产品线均含字母(B-code)，纯数字≥8位才可能是长序号。
    if re.match(r'^\d{8,}$', t): return True

    # 2) B-code: 单字母 B + 5-6 位数字（工厂内部序列号格式，全量统计 ~20 个）
    if re.match(r'^[Bb]\d{5,6}$', t): return True

    # 3) 字母+数字混合（位置无关）：总长 5-10，含字母和数字，数字占比 ≥35%
    #    覆盖 BFFC001(4a3d连续), B280C01(交错B-280-C-01), B28C001(交错)
    #    ⭐ v19.17: len 下限从 6→5，覆盖更多短格式
    if 5 <= len(t) <= 10:
        alpha = sum(1 for c in t if c.isalpha())
        digit = sum(1 for c in t if c.isdigit())
        if alpha >= 1 and digit >= 3:
            ratio = digit / (alpha + digit)
            if ratio >= 0.35:
                excluded = ['RS485', 'RS232', 'CAN', 'MODBUS', 'TCP', 'UDP']
                if re.search(r'[AVWva]', t) and re.search(r'\d', t):
                    return False
                if not any(ex in t.upper() for ex in excluded):
                    return True

    # 4) ⭐ v19.17 新增：5字符"字母主导+尾部数字"短格式（QHRW6 等）
    #     产品标签 QR 码旁的追溯码，每台可能不同。特征：字母开头、α≥3、d≥1。
    #     ⭐ v19.25 收紧：要求尾部 2+ 连续数字（序列号如 AB123），排除型号名如 QH8BY（数字在中间、尾部纯字母）。
    # ⭐ v19.29 扩展（2026-07-31）：从精确 len==5 扩展到 5-7 字符。
    #     实拍反馈 QHRW6(6字符,5α+1d)、QHR45(5字符,3α+2d) 等产品码被 _is_qr_ocr_noise 的
    #     ^[A-Z0-9]{4,12} 宽泛正则误抓为 QR 噪声→灰化（即使两侧相同也应绿框）。
    #     这些码的特征：字母占比高(α≥50%)、数字少(1-3位)、字母开头、长度 5-7。
    for slen in range(5, 8):  # 5, 6, 7
        if len(t) != slen:
            continue
        alpha = sum(1 for c in t if c.isalpha())
        digit = sum(1 for c in t if c.isdigit())
        alpha_ratio = alpha / (alpha + digit) if (alpha + digit) > 0 else 0
        if (alpha >= 3 and digit >= 1 and t[0].isalpha() and
                alpha_ratio >= 0.4 and digit <= 3):
            excluded_short = ['RS485','RS232','CAN','MODBUS','TCP','UDP','GPIO','I2C','SPI']
            if not any(ex in t.upper() for ex in excluded_short):
                return True
        break  # 只匹配当前长度，不继续尝试其他长度

    return False


def _is_serial_number_raw(text):
    """⭐ v19.21 新增：对原始文本（_norm_text 之前）做序列号预检。

    根因：_norm_text() 的「尾部数字剥离」规则（v19.13 电话保护）会破坏
    某些序列号/批次码格式：
      - BFF0001 → 剥离 '001' → 'bff0' → _is_serial_number=False ❌
      - 8DV2654 → 剥离 '54'  → 'dv2'  → _is_serial_number=False ❌

    本函数在 norm_text 之前执行，覆盖这些「字母+长数字尾」的批次码模式。
    与 _is_serial_number 互补：本函数看原始形态，后者看归一化形态。
    """
    if not text:
        return False
    t = text.strip()
    if len(t) < 5 or len(t) > 12:
        return False

    # R1: 字母前缀(2-4位) + 数字尾(4-6位) —— 批次码 BFF0001 / QH8BY / BFFC001
    if re.match(r'^[A-Za-z]{2,4}\d{4,6}$', t):
        return True

    # R2: 字母+数字混合，总数字≥4位，总长 5-12 —— 覆盖各种批次码/序列号原始格式
    #    包括：8DV2654(1d+2a+4d), B280C01(1a+3d+1a+2d), BFF0001(3a+4d已被R1覆盖)
    #    排除明显非序列号的规格参数和型号名
    #    ⭐ v19.25 修正：增加电压/功率规格排除（DC15/24V, AC220V 等含 / 的规格）
    if 5 <= len(t) <= 12:
        alpha = sum(1 for c in t if c.isalpha())
        digit = sum(1 for c in t if c.isdigit())
        if alpha >= 1 and digit >= 4:
            # 电压/功率规格模式排除
            if re.search(r'[AVWavw]\s*\d+[VvWw]|DC\d+[/\d]*[Vv]?|AC\d+[Vv]?', t, re.IGNORECASE):
                return False
            excluded = ['RS485', 'RS232', 'CAN', 'MODBUS', 'TCP', 'UDP',
                         'GPIO', 'I2C', 'SPI', 'DC12V', 'DC24V', 'AC220V',
                         'HITACHI']  # HITACHI 是品牌名(块图OCR常见)
            if not any(ex in t.upper() for ex in excluded):
                return True

    return False


def _is_cad_annotation(text):
    """综合判断是否为 CAD 标注（尺寸 + 注释 + 停用词 + LCD 屏显内容 + 序列号 + CJK垃圾）"""
    if not text: return False
    if _is_dimension(text): return True
    if _is_annotation_note(text): return True
    if _is_lcd_display_text(text): return True  # LCD 动态内容不算缺标
    if _is_serial_number(text): return True  # ⭐ v19.1: 序列号/批次码不算缺标（每台不同）
    if _is_cjk_garbage(text): return True   # ⭐ v19.9: 短 CJK OCR 垃圾不算差异
    return False

def _is_cad_stopword(text):
    """检测是否包含 CAD 停用词（PartLabelStopwords）"""
    if not text: return False
    for w in CAD_STOPWORDS:
        if w in text: return True
    return False


# ── OCR 带 bbox ──
def _ocr_image(img_path, cache_lines=None):
    """调用 ocr_with_bbox.py，返回 lines 列表。
    如果 cache_lines 已提供（预计算好的），直接返回，跳过图块 OCR。
    """
    if cache_lines is not None:
        return cache_lines
    # 直接内联调用 RapidOCR，避免子进程开销
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    try:
        pil = Image.open(img_path).convert("RGB")
        w, h = pil.size
        # ⭐ 缩小图片到最大边 960px，OCR 加速 2-4 倍，文本正确率基本不变
        max_dim = 640
        scale_inv = 1.0  # 逆向缩放系数（bbox 从缩小图 → 原图）
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            scale_inv = 1.0 / scale
            pil = pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img = np.asarray(pil)
        engine = RapidOCR()
        result, _ = engine(img)
        lines = []
        if result:
            for item in result:
                if len(item) < 3: continue
                box, txt, conf = item[0], item[1], item[2] if isinstance(item[2], (int, float)) else (float(item[2]) if item[2] else 0.0)
                corners = [[float(p[0]) * scale_inv, float(p[1]) * scale_inv] for p in box]
                lines.append({"text": txt, "bbox": corners, "conf": conf})
        return lines
    except Exception as e:
        return []


# ── bbox 工具 ──
def _bbox_rect(bbox):
    """bbox 的 4 个角 → (x, y, w, h)"""
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    x, y = int(min(xs)), int(min(ys))
    x2, y2 = int(max(xs)), int(max(ys))
    return x, y, max(x2 - x, 1), max(y2 - y, 1)

def _draw_box(draw, rect, color, width=3):
    """安全画框，(x,y,w,h) → (x0,y0,x1,y1)，防负坐标"""
    x, y, w, h = rect
    if w <= 0 or h <= 0: return
    draw.rectangle([x, y, x + w, y + h], outline=color, width=width)


# ── 文本相似度 ──
def _chars_overlap(a, b):
    """字符集重叠比例（用 Jaccard = |A∩B|/|A∪B|，对称）"""
    sa, sb = set(a.lower()), set(b.lower())
    if not sa or not sb: return 0
    common = sa & sb
    union = sa | sb
    return len(common) / len(union)

def _is_cjk(text):
    """检测文本是否主要包含 CJK 字符（中日韩）。"""
    if not text: return False
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff' or '\uac00' <= c <= '\ud7af')
    return cjk_count >= len(text) * 0.5

def _norm_text(t):
    """文本比对前归一化：全角→半角、± 变体统一、去空格、转小写。
    减少 OCR/字体差异导致的假差异。

    ⭐ v19 新增：OCR 数字/字母混淆校正（型号/序列号场景）。
      小字体激光打标中，OCR 常将数字误读为形似字母：
        1→i/l/I, 0→o/O, 5→s/S, 8→B, 6→b/G, 9→g/q, ts→15 等。
      校正仅在「字母为主的型号串」中执行（含 2+ 字母+数字混合），
      不影响纯中文或纯英文句子（避免 over-correct）。

    ⭐ v19.7 新增：OCR 垃圾前缀/后缀清理。
      切图 OCR 常在文本边缘粘杂质字符：(0净NFC…、…NSWATGNCWO_、(NFC便继控制
      清理规则：前导非语义字符（括号/数字/标点/短CJK垃圾）、尾部下划线/标点。
    """
    if not t: return ""
    t = t.strip()
    # 全角→半角（！→!，Ａ→A，０→0 等）
    t = ''.join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in t)
    # ± 变体统一（OCR 常把 ± 读成 +/- 或 ±）
    t = t.replace('+/-', '±').replace('±', '±').replace('—', '-').replace('－', '-')
    # ⭐ 斜杠/横杠归一化（OCR 常把 DC15/24V 读成 DC15-24V 或反之）
    #   数字间的 / 和 - 统一去掉（DC15/24V ≡ DC15-24V ≡ DC1524V）
    t = re.sub(r'(\d)[/－-](\d)', r'\1\2', t)
    # ⭐ v19.10 数字内点号归一化：LCD 屏显数值 OCR 常读出/漏掉点号
    #   DB 存「8888」但实时 OCR 读作「8.888」（或反之），导致纯数字不匹配
    #   仅去除数字间的点号（保留小数点后跟字母的情况如 "3.5mm" 不受影响）
    t = re.sub(r'(\d)\.(\d)', r'\1\2', t)
    # ⭐ NFC 标识归一化（2026-07-28）：DB 常见双 N 粘连(NNFC)，产品多为单 N(NFC) 或独立成行。
    #   折叠为 NFC，避免「热线+NNFC」vs「热线+NFC」被误判差异（实拍有热线即应判一致）。
    #   仅作用于 N+FC 模式（NFC 标记），不影响型号/尺寸/日期/热线正文等其他匹配。
    t = re.sub(r'N{2,}FC', 'NFC', t, flags=re.IGNORECASE)
    # ⭐ v19 OCR 数字↔字母混淆校正（型号/序列号专用）
    #   条件：字符串包含 ≥2 字母（典型型号格式如 YCWA15NCWQ / PC-P1HVQA / YCWATSNCWQ）
    #   注意：不要求包含数字，因为 OCR 可能已将数字全误读为字母（如 15→TS）
    if re.search(r'[A-Za-z].*[A-Za-z]', t) and len(t) >= 6:
        # 常见混淆对（按优先级排序，先处理长的避免部分匹配）
        ocr_fixes = [
            ('ts', '15'), ('TS', '15'),   # 最常见：小字体 1+5 粘连读成 t+s
            ('s5', '55'), ('S5', '55'),     # s/5 混淆重复
            ('sl', '15'), ('SL', '15'),     # s+l → 1+5
            ('o0', '00'), ('O0', '00'),     # o/0 混淆
            # ⭐ v19.10 移除 ol→01：破坏英文词（VOLTAGE→V01TAGE，SOLUTION→S01UTION）
            #   原始意图是处理型号中 o+l→0+1 混淆，但 str.replace 全局替换误伤英文单词
            #   此场景改由 ASCII 子序列 LCS 规则（_texts_match_strict）覆盖
            # ('ol', '01'), ('OL', '01'),     # o+l → 0+1 (removed v19.10)
            ('il', '11'), ('IL', '11'),     # i/l → 1
            ('l1', '11'), ('L1', '11'),
            ('i1', '11'), ('I1', '11'),
            ('8B', '88'),                   # 8/B 混淆
            ('6b', '66'), ('6G', '66'),     # 6/b/G 混淆
            ('9q', '99'), ('9g', '99'),     # 9/q/g 混淆
            # ⭐ v19.9: B/G 混淆（型号中 b↔g 常误读，如 WATBNCWO ↔ WATGNCWO）
            ('bg', 'gg'), ('gb', 'gg'),
            ('BG', 'GG'), ('GB', 'GG'),
            # ⭐ v19.9: V/W 混淆（LOW VOLTAGE 常被读成 LOWWOLTAGE / LOWVOLTAGE / LON VOLTAGE）
            ('vw', 'vv'), ('wv', 'vv'),
            ('vw', 'wv'),  # 交叉修正
            # ⭐ v19.25 修正：LOWWOLTAGE（空格丢失+W重复）→ LOW VOLTAGE
            #    CAD 切图 OCR 常把 "LOW VOLTAGE" 读成 "LOWWOLTAGE"，需还原。
            ('lowwoltage', 'low voltage'),
            ('LOWWOLTAGE', 'LOW VOLTAGE'),
            # ⭐ v19.9: N/L 混淆（LON vs LOW）
            ('ln', 'lw'), ('LN', 'LW'),
            ('nl', 'wl'), ('NL', 'WL'),
        ]
        for wrong, right in ocr_fixes:
            t = t.replace(wrong, right)

    # ⭐ v19.7 OCR 垃圾前缀/后缀清理
    # 切图时 OCR 常在文本边缘粘杂质字符，需在匹配前剥离：
    #   前导垃圾：(0净NFC…、(NFC便继控制、)NFC便捷控制
    #   尾部垃圾：…NSWATGNCWO_、…BFFO0O1、…88.
    # 规则：仅当剥离后剩余有效内容 ≥ 原串 50% 时才执行（防误剥短文本）
    if len(t) >= 4:
        original = t
        # 前导剥离：开头的括号/数字/标点 + ≤2个CJK垃圾字（如"0净"、"(、"）"）
        t = re.sub(r'^[\(\)\[\]\<\>\{\}\"\'\`\·\.\,\;\:\!\?\*\+\=\\\|\~\`\^\%\#\$\@\&\d]+', '', t)
        # ⭐ v19.10 修复：前导CJK剥离改为仅跟ASCII时触发
        #   旧规则 (?=[A-Za-z0-9\u4e00-\u9fff]{3,}) 会误剥有效中文：
        #     「服务热线」→「务热线」、「禁止强电」→「止强电」
        #   新规则：仅在CJK后紧跟ASCII(型号/数字)时剥离，纯中文文本不受影响
        t = re.sub(r'^[\u4e00-\u9fff]{1,2}(?=[A-Za-z0-9]{3,})', '', t)
        # 尾部剥离：下划线/点/括号/标点尾巴（如 "_"、")"、"."、"00"）
        t = re.sub(r'[_\.\)\]\}\>]+$', '', t)
        # ⭐ v19.13 修复：尾部数字剥离排除长数字串（电话/热线/长序号）
        #   v19.10 的 (?<!) 仅保护「紧邻冒号」的数字段，无法保护电话末尾：
        #     「服务热线：400-620-6607」→ 去杠后「服务热线:4006206607」，末尾 607 不在冒号后 → 被截成 4006206
        #   新规则：若整串含 ≥7 位连续数字（电话/热线/长序号），整段保护、不剥离任何尾部数字；
        #         仅对「无长数字串」的短垃圾尾巴(如 …88 / …00)保留剥离，避免误伤型号尾号。
        # ⭐ v19.29 修复（2026-07-31）：原规则对短产品码(≤8字符)过度剥离。
        #   型号/追溯码如 QHR45(5字符)、8EQ0024(7字符) 尾部含 2-3 位数字是正常组成部分，
        #   不是 OCR 垃圾尾巴。剥离后残串仅剩 2-3 字符（如 qhr/eq0），导致：
        #     ① _is_serial_number 因长度不足无法识别 ② _is_qr_ocr_noise 用 ^[A-Z0-9]{4,12} 误判为 QR 噪声→灰化
        #   修复：仅对 >8 字符的文本执行尾部数字剥离（电话/长描述通常 >8 字符；产品码 ≤8 字符）。
        _orig_before_strip = t
        if (len(t) > 8 and
            re.search(r'[A-Za-z\u4e00-\u9fff]', t) and
            not re.search(r'\d{7,}', t)):
            t = re.sub(r'(?<![:：,，])\d{2,3}$', '', t)
        # 安全检查：剥离后不能太短（从 40% 收紧到 55%，防止短码被剥到残废）
        if len(t) < len(_orig_before_strip) * 0.55:
            t = _orig_before_strip  # 回滚

    # 去所有空白
    t = re.sub(r'\s+', '', t)
    return t.lower()

def _longest_common_substring(s1, s2):
    """返回最长公共子串（经典 DP，O(n*m)）。
    用于检测 OCR 行合并/拆分：如照片「PC-P1HVQA服务热线400-860-1111」
    vs 块「服务热线400-860-1111NNFC」的公共核「服务热线400-860-1111」。"""
    if not s1 or not s2: return ""
    m, n = len(s1), len(s2)
    # 用一维 DP 滚动数组省空间
    dp = [0] * (n + 1)
    max_len = 0
    end_pos = 0
    for i in range(1, m + 1):
        prev = 0
        for j in range(1, n + 1):
            temp = dp[j]
            if s1[i - 1] == s2[j - 1]:
                dp[j] = prev + 1
                if dp[j] > max_len:
                    max_len = dp[j]
                    end_pos = i
            else:
                dp[j] = 0
            prev = temp
    return s1[end_pos - max_len:end_pos]


def _texts_match_strict(pt, bt):
    """严格文本匹配（比对前先归一化：全角/半角、±、空格、大小写）：
    - 子串情况（CJK）：短的至少 2 字符即可（如"模式""定时"有完整语义）
    - 子串情况（ASCII）：短串必须覆盖长串 ≥ 40% 才算真子串匹配
      （防 OCR 切出的碎片如「NNFC」假匹配长串「服务热线400-860-1111NNFC」）
    - 非子串：字符 Jaccard ≥ 80% 且长度差异不超 50%
    - OCR 行合并/拆分：最长公共子串覆盖双方均 ≥ 50% 且长度 ≥ 6 → 匹配
      （例：照片「PC-P1HVQA服务热线400-860-1111」vs 块「服务热线400-860-1111NNFC」
       公共核「服务热线400-860-1111」覆盖双方均 >50% → 应匹配）
    """
    if not pt or not bt: return False
    pt = _norm_text(pt); bt = _norm_text(bt)
    if not pt or not bt: return False
    # CJK 文本（中文等）：2 个字符就有完整语义（如"模式""定时"）
    # ASCII 文本：至少 3 字符防单字母噪声
    min_len = 2 if (_is_cjk(pt) or _is_cjk(bt)) else 3
    if len(pt) < min_len or len(bt) < min_len: return False

    # 子串匹配：短文本是长文本的一部分
    # ⭐ v5 修复：ASCII 子串必须覆盖长串 ≥ 40%，防止碎片假匹配
    #   反例：「NNFC」(4字) 是「服务热线400-860-1111nnfc」(20+字) 的子串 → 不应匹配
    #   正例：「PC-P1HVQA」(9字) 是「pc-p1hvqa服务热线...」(24字) 的子串，覆盖率 37.5%
    #         但它是【前缀】且长度≥6 → 型号前缀匹配 ✓
    is_cjk_text = _is_cjk(pt) or _is_cjk(bt)
    if pt in bt or bt in pt:
        if is_cjk_text:
            return True  # CJK 子串：2 字即有意义（如"上盖"在"上盖板"中）
        # ASCII 子串：要求短串覆盖长串 ≥ 40%（防碎片假匹配）
        shorter = min(len(pt), len(bt))
        longer = max(len(pt), len(bt))
        if shorter / longer >= 0.4:
            return True
        # ⭐ 前缀/后缀例外：短串 ≥ 6 字符且是长串的精确前缀或后缀
        #    （如「PC-P1HVQA」是合并行前缀、「200512」是日期码后缀）
        #    这是有意义的独立标识符碎片，不是噪声。
        if shorter >= 6:
            shorter_str, longer_str = (pt, bt) if len(pt) <= len(bt) else (bt, pt)
            if longer_str.startswith(shorter_str) or longer_str.endswith(shorter_str):
                return True
        # 覆盖率不够且非前缀/后缀 → 不算子串匹配，交给后续 Jaccard/LCS 处理

    # 非子串：要求字符 Jaccard ≥ 80% 且长度差异不超 50%
    longer = max(len(pt), len(bt))
    shorter = min(len(pt), len(bt))
    if shorter / longer < 0.5: return False
    if _chars_overlap(pt, bt) >= 0.8:
        return True

    # ⭐ v19.7 CJK OCR 单字误差容错（2026-07-29）
    #   切图/照片 OCR 对中文常出现单字误读（便→捷、继→捷、防→放 等），
    #   导致 Jaccard 在 65%~80% 之间、低于通用 80% 阈值但不匹配。
    #   条件（需同时满足）：
    #   1. CJK 文本（含中文 ≥ 30%）
    #   2. 长度相似（短/长 ≥ 70%，排除"长串包含短串碎片"的情况）
    #   3. 字符重叠 ≥ 65%（约容许 1-2 个 CJK 字误差 / 6-8 字串）
    #   4. 最长公共子串覆盖双方均 ≥ 55%（确保语义核一致）
    if is_cjk_text and shorter / longer >= 0.7:
        overlap = _chars_overlap(pt, bt)
        if overlap >= 0.65:
            lcs = _longest_common_substring(pt, bt)
            if len(lcs) >= max(4, shorter * 0.55):
                cover_pt = len(lcs) / len(pt)
                cover_bt = len(lcs) / len(bt)
                if cover_pt >= 0.55 and cover_bt >= 0.55:
                    return True

    # ⭐ v19.9 ASCII 型号 OCR 单字替换容错（2026-07-29）
    #   小字体激光打标中，OCR 常将单个字母误读为形似字母（b↔g, v↔w, n↠r 等），
    #   导致连续子串 LCS 断裂（如 WATBN**CWO** vs WATGN**CWO** 的最长公共子串只有 NCWO=4）。
    #   但用「非连续」LCS（子序列）可发现实质匹配（watncwo=7, 覆盖率 87%/70%）。
    #   条件（需同时满足）：
    #   1. 非 CJK 文本（纯/主 ASCII，如型号 YCWA15NCWQ / WATBNCWO）
    #   2. 双方长度均 ≥ 6（短型号才有意义）
    #   3. 长度比 ≥ 0.6（排除碎片假匹配）
    #   4. 字母占比 ≥ 60%（确认是型号/编码类文本）
    #   5. Jaccard ≥ 55%（约容许 1-2 个单字替换 / 8-10 字串）
    #   6. 真实 LCS(子序列) 覆盖双方均 ≥ 65%
    if not is_cjk_text and shorter >= 6 and longer >= 6 and shorter / longer >= 0.6:
        alpha_pt = sum(1 for c in pt if c.isalpha())
        alpha_bt = sum(1 for c in bt if c.isalpha())
        if alpha_pt / len(pt) >= 0.6 and alpha_bt / len(bt) >= 0.6:
            jaccard = _chars_overlap(pt, bt)
            if jaccard >= 0.55:
                # 计算真实 LCS（最长公共子序列，非连续）
                m, n = len(pt), len(bt)
                dp = [[0] * (n + 1) for _ in range(m + 1)]
                for i in range(1, m + 1):
                    for j in range(1, n + 1):
                        if pt[i - 1] == bt[j - 1]:
                            dp[i][j] = dp[i - 1][j - 1] + 1
                        else:
                            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
                lcs_len = dp[m][n]
                cover_pt = lcs_len / len(pt)
                cover_bt = lcs_len / len(bt)
                if cover_pt >= 0.65 and cover_bt >= 0.65:
                    return True

    # ── OCR 行合并/拆分容错（LCS 最长公共子串）──
    lcs = _longest_common_substring(pt, bt)
    if len(lcs) >= 6:
        cover_pt = len(lcs) / len(pt)
        cover_bt = len(lcs) / len(bt)
        if cover_pt >= 0.5 and cover_bt >= 0.5:
            return True

    return False

# ── QR / 图标检测 ──
_QR_PATTERNS = {"QR", "二维码", "QR码", "条码"}
def _is_qr_or_icon(text):
    """检测是否为二维码/图标（不作为文本差异对比）。
    规则：
    - 显式 [QR:] 标记 → QR
    - 包含"二维码"/"QR码"/"条码" → QR
    - NFC 仅当文本长度 ≤ 6 且内容为纯字母时 → NFC 图标
    - 其他文本段不视为图标（避免"服务热线400-860-1111NNFC"被误杀）
    """
    if not text: return False
    t = text.strip()
    if not t: return False
    # 显式 [QR:] 标记
    if t.startswith("[QR:") or t.startswith("[QR]"): return True
    for p in _QR_PATTERNS:
        if p in t: return True
    # NFC 仅当是短纯字母（≤6字符），如 "NFC" "NFCC" 等
    if len(t) <= 6 and t.isalpha():
        if "NFC" in t.upper(): return True
    return False


# ── QR 解码内容 / OCR 噪声 识别（文本比对应跳过，仅由图标位置判定）──
# 已知型号前缀：用于区分"型号标签（应比对）"与"QR 解码内容（应跳过）"。
# 型号标签通常以这些前缀开头；QR 解码出的序列号/日期码则不带。
_MODEL_PREFIXES = ("PC-", "P1", "VK", "YC", "YK", "HS", "HY", "WF", "VR")

def _is_qr_content_text(text):
    """判断文本是否为二维码解码内容（序列号/日期码等）。
    符合用户规则『二维码不识别具体内容，只判定位置是否有内容』——此类文本
    应从『文本比对』中跳过，由图标位置比对（紫色=到位/红色=缺图标）负责。

    规则：纯大写字母数字码(4-12) 或 6 位日期码，且不以已知型号前缀开头。
      - QHRW6 / 200512 / B28C001 / 74P4305 → True（跳过）
      - PC-P1HVQA / VK01（型号标签）      → False（保留比对）
    """
    if not text: return False
    t = text.strip()
    if not t: return False
    for p in _MODEL_PREFIXES:
        if t.upper().startswith(p.upper()):
            return False
    # 纯大写字母/数字码（无中文、无小写、无空格、无连字符、无标点）
    if re.match(r'^[A-Z0-9]{4,12}$', t):
        return True
    # 6 位纯数字（日期码，如 200512）
    if re.match(r'^\d{6}$', t):
        return True
    return False

def _has_label_feature(text):
    """判断文本是否含'标签条专属'特征（型号/服务热线/NFC/二维码等）。

    用途：跨块溢出过滤。照片 OCR 检出的型号/热线/NFC 等标签条文本，
    其 bbox 常因整行识别过大而跨界覆盖尺寸/序列号块，导致这些块误报
    '照片有、块无'的黄框（假多标）。纯尺寸/序列号块本不应含标签条文本，
    此类 yellow 应跳过。
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    # 型号特征：≥2字母后接数字（YCWA15NCWQ / QHRLA / WATBNCWO 等）
    if re.search(r"[A-Za-z]{2,}-?[A-Za-z0-9]*[0-9]", t):
        return True
    # 服务热线 / NFC / 二维码 等标签条专属词
    if "服务热线" in t or "NFC" in t or "二维码" in t or "热线" in t:
        return True
    return False


def _block_has_label_feature(block_lines):
    """块图 DB 文本是否含任意标签条特征（型号/热线/NFC 等）。"""
    return any(_has_label_feature(bl.get("text", "")) for bl in block_lines)


def _is_ocr_noise(text):
    """判断是否为 OCR 噪声行（应跳过，不算差异）。
    典型：含撇号/引号等 RapidOCR 把 QR/图标点阵误读的伪影（如 S'Oft），
    或极短且无语义的乱码。
    """
    if not text: return False
    t = text.strip()
    if not t: return False
    # 含撇号/引号 → OCR 伪影（S'Oft 等）
    if "'" in t or '"' in t:
        return True
    # 极短(<2) 且非纯数字/非纯中文 → 噪声（≥2 字母如 OK/NF 不算噪声）
    if len(t) < 2 and not re.match(r'^[\d]+$', t) and not _is_cjk(t):
        return True
    return False


# ── 图标区域检测（模型无关：QR 定位符聚类 + pyzbar 条形码 + NFC 密矩形）──

def _is_qr_ocr_noise(text):
    """⭐ v19.14 判断文本是否为二维码区域 OCR 噪声（应灰化跳过）。
    触发：已知QR伪影词表 / 纯大写字母数字码(4-12位) / 6位日期码。
    排除：型号标签/服务热线/中文/CAD标注。
    ⭐ v19.16 双N的NFC畸变(NNFC/NNNFC)是二维码区域 OCR 垃圾，先于排除规则捕获。
    """
    if not text: return False
    t = text.strip()
    if not t or len(t) < 2: return False
    if t.lower() in {"s'oft", "soft", "s0ft", "oft", "qr_code", "qr code"}: return True
    # ⭐ v19.16 双N的NFC（二维码区域OCR畸变，如 NNFC）算二维码噪声，不放行
    if re.search(r'N+NFC', t, re.IGNORECASE): return True
    if _is_cjk(t): return False
    if any(kw in t for kw in ("服务热线", "热线", "二维码")): return False
    if "NFC" in t: return False  # 合法 NFC 文本（NFC便捷控制等）不算二维码噪声
    if any(s in t for s in ("±", "°", "φ")): return False
    for p in _MODEL_PREFIXES:
        if t.upper().startswith(p.upper()): return False
    if re.match(r'^[A-Z0-9]{4,12}$', t):
        # ⭐ v19.20 排除纯字母短码(4-6位)：QHRLA/QHRW 等产品代码前缀，
        #    不是 QR 编码内容(QR 内容通常含数字或更长)。避免误杀序列号组件。
        if re.match(r'^[A-Z]{4,6}$', t): return False
        # ⭐ v19.28 排除纯数字码(4-12位)：日期码(200512)、批次号(100095)等是产品打标，
        #    不是 QR 噪声。v19.26 将 _is_serial_number 阈值提升至 8 位后，这些数字不再被
        #    序列号路径捕获，落入本规则被误杀→灰化。混合字母数字码(如 B280001)仍判为 QR 噪声。
        if re.match(r'^\d{4,12}$', t): return False
        # ⭐ v19.29 排除字母主导短码(5-7位)：QHRW6/QHR45/74P3998 等产品标识码，
        #    字母占比 ≥40% 且长度 ≤7 → 是型号/追溯码而非 QR 编码内容。
        #    QR 编码内容通常数字占比高或更长（如 URL/hash）。
        if 5 <= len(t) <= 7:
            alpha = sum(1 for c in t if c.isalpha())
            if alpha >= len(t) * 0.4:
                return False
        return True
    # ⭐ v19.28 移除纯 6 位数字规则（原 ^\d{6}$）：该规则将日期码/批次号(如 200512)
    #    误判为 QR 时间戳噪声。v19.26 将 _is_serial_number 阈值提升至 8 后，这些数字
    #    不再被序列号路径捕获，落入本函数被误杀→灰化。纯数字 QR 噪声极罕见，
    #    而 6 位日期码在产品打标中普遍存在。宁可漏过极少数数字 QR 噪声，不可误杀日期码。
    return False


def _clean_qr_garbage(text):
    """⭐ v19.16 从一行文本中剥离【嵌入的二维码区域 OCR 垃圾】，返回净化文本。
    解决根因：二维码垃圾(如 NNFC / S'Oft)常被 OCR 拼进正常行
    （例：'服务热线400-860-1111NNFC'），导致整行既没被灰化、又匹配不上 → 假红框。
    策略：只移除『绝不可能误伤合法内容』的片段，不动型号/电话/中文等正常文本：
      · N+NFC  —— 双N的NFC畸变，合法文本不会有（NFC便捷控制是单N）
      · S['']?oft —— S'Oft 伪影，合法文本不会有
      · (?<!\d)\d{6}(?!\d) —— 独立 6 位日期码（二维码时间戳），绝不会出现在电话/型号中
    适用范围：全部图纸、所有拼合场景，不依赖特定图纸。

    ⭐ v19.28 修复（2026-07-31）：v19.26 将 _is_serial_number 纯数字阈值从 6→8 后，
       200512 等日期码不再被识别为序列号，落入本函数 \d{6} 规则被剥成空串 → step3/step4
       空串自动灰化（line 1919/2010），导致"相同日期码被误灰化"回归。
       修复：若清理后结果为空但原始输入是纯数字串（独立日期码/批次号），保留原文，
       让它进入正常匹配流程（两侧都有→绿框，仅一侧有→红/黄框）。
       仅当 \d{6} 嵌入在更长文本行内时才执行剥离（如 "AB200512CD" → "AB CD"）。
    """
    if not text:
        return text
    original = text
    t = text
    # 1) 固定畸变模式（不依赖整词判定，且绝不会误伤合法内容）
    t = re.sub(r"N+NFC", " ", t, flags=re.IGNORECASE)      # 双N的NFC（二维码区域OCR畸变）
    t = re.sub(r"S['']?oft", " ", t, flags=re.IGNORECASE)  # S'Oft / Soft 伪影
    # 2) 独立 6 位日期码（二维码时间戳；(?<!\d)(?!\d) 确保不是更长数字串的一部分，
    #    因此绝不误伤电话号码/序列号中的数字段）
    # ⭐ v19.28：仅在原始文本长度 > 6 时才执行替换（即日期码嵌入在更长行内时才剥离）
    if len(original.strip()) > 6:
        t = re.sub(r"(?<!\d)\d{6}(?!)",
                   lambda m: " " if _is_qr_ocr_noise(m.group(0)) else m.group(0), t)
    t = re.sub(r"\s+", " ", t).strip()
    # ⭐ v19.28：若清理结果为空但原文本是纯数字（独立日期码），保留原文
    if not t and original.strip() and re.match(r'^\d+$', original.strip()):
        return original.strip()
    return t

def _qr_finders(gray):
    """检测 QR 定位符（黑白黑同心方块），返回 [(cx,cy,w,h), ...]。"""
    import numpy as np
    import cv2
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 15, 8)
    inv = cv2.bitwise_not(bw)
    contours, _ = cv2.findContours(inv, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    finders = []
    for cnt in contours:
        x, y, bw2, bh2 = cv2.boundingRect(cnt)
        if bw2 < 8 or bh2 < 8:
            continue
        ar = bw2 / bh2 if bh2 > 0 else 0
        if not (0.6 < ar < 1.7):
            continue
        area = bw2 * bh2
        if area < 40 or area > 0.04 * w * h:
            continue
        # 验证同心：中心区域应是黑（内方块），整体偏暗
        cx, cy = x + bw2 // 2, y + bh2 // 2
        inner = gray[max(0, cy - bh2 // 4):cy + bh2 // 4,
                     max(0, cx - bw2 // 4):cx + bw2 // 4]
        if inner.size == 0:
            continue
        if np.mean(inner) >= 120:
            continue  # 中心不够黑 → 不是定位符
        finders.append((cx, cy, bw2, bh2))
    return finders


def _cluster_finders(finders, W, H):
    """把彼此靠近的定位符聚成一组（一个 QR 通常 3 个），返回 QR 外框 [(x,y,w,h), ...]。"""
    if not finders:
        return []
    sides = [max(f[2], f[3]) for f in finders]
    thr = 3 * max(20, int(sum(sides) / len(sides)))
    used = [False] * len(finders)
    clusters = []
    for i in range(len(finders)):
        if used[i]:
            continue
        group = [finders[i]]
        used[i] = True
        for j in range(len(finders)):
            if used[j]:
                continue
            fi, fj = finders[i], finders[j]
            d = ((fi[0] - fj[0]) ** 2 + (fi[1] - fj[1]) ** 2) ** 0.5
            if d < thr:
                group.append(fj)
                used[j] = True
        clusters.append(group)
    rects = []
    for g in clusters:
        xs = [f[0] for f in g]
        ys = [f[1] for f in g]
        side = max(max(f[2], f[3]) for f in g)
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        size = span + side * 1.5
        sz = int(size)
        rects.append((int(cx - sz / 2), int(cy - sz / 2), sz, sz))
    return rects


def _dense_rects(gray, min_side=22, max_ar=2.2, min_density=0.35):
    """检测密集深色矩形（NFC/logo/条形码区），返回 [(x,y,w,h,density), ...]。"""
    import cv2
    h, w = gray.shape
    inv = cv2.bitwise_not(gray)
    _, th = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < min_side or bh < min_side:
            continue
        if bw > w * 0.7 or bh > h * 0.7:
            continue
        ar = bw / bh if bh > 0 else 0
        if ar < 1 / max_ar or ar > max_ar:
            continue
        roi = th[y:y + bh, x:x + bw]
        if roi.size == 0:
            continue
        density = cv2.countNonZero(roi) / (bh * bw)
        if density >= min_density:
            out.append((x, y, bw, bh, density))
    return out


def _merge_rects(rects, iou_thr=0.5):
    """按 IoU 合并重叠框（保留大的）。"""
    if not rects:
        return []
    kept = []
    for b in sorted(rects, key=lambda r: -(r[2] * r[3])):
        overlap = False
        for k in kept:
            xa, ya = max(b[0], k[0]), max(b[1], k[1])
            xb = min(b[0] + b[2], k[0] + k[2])
            yb = min(b[1] + b[3], k[1] + k[3])
            iw, ih = max(0, xb - xa), max(0, yb - ya)
            inter = iw * ih
            if inter == 0:
                continue
            union = b[2] * b[3] + k[2] * k[3] - inter
            if inter / union > iou_thr:
                overlap = True
                break
        if not overlap:
            kept.append(list(b))
    return [tuple(k) for k in kept]


def _overlaps_any(rect, others, iou_thr=0.25, center_thr=0.5):
    """rect 是否与 others 中任意框重叠（IoU 或中心落入）。用于排除与文本重叠的误检。"""
    x, y, w, h = rect
    cx, cy = x + w / 2.0, y + h / 2.0
    for o in others:
        ox, oy, ow, oh = o
        # 中心落入对方
        if ox <= cx <= ox + ow and oy <= cy <= oy + oh:
            return True
        # IoU
        xa, ya = max(x, ox), max(y, oy)
        xb = min(x + w, ox + ow)
        yb = min(y + h, oy + oh)
        iw, ih = max(0, xb - xa), max(0, yb - ya)
        inter = iw * ih
        if inter <= 0:
            continue
        union = w * h + ow * oh - inter
        if inter / union > iou_thr:
            return True
    return False


def _text_overlaps_icon(bbox, icons, expand=0.4):
    """文本 bbox 是否与图标区域重叠（扩展边距后）：用于跳过 QR/NFC 区域内的 OCR 噪声文本。
    expand: 图标区域向外扩展比例，覆盖邻近的误识别噪声（如 QR 码被 OCR 读成 S'Oft）。
    返回 True 时调用方应将该文本行视为"图标内文字/噪声"，标记为灰色并跳过文本对比。
    """
    if not icons:
        return False
    x, y, w, h = bbox
    expanded = []
    for item in icons:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue
        ix, iy, iw, ih = item[:4]
        ew = iw * (1 + 2 * expand)
        eh = ih * (1 + 2 * expand)
        expanded.append((int(ix - expand * iw), int(iy - expand * ih),
                         int(ew), int(eh)))
    return _overlaps_any(bbox, expanded)


def _is_icon_aspect_ratio_ok(w, h):
    """检查宽高比是否在真实图标范围内。
    QR 码 ≈ 1.0，NFC 标签 ≈ 1.0~1.5，Logo ≈ 0.6~1.8。
    散热栅格(竖长条 ar>3.0) / 横向文字条(ar<0.25) / 极端细线 在此被剔除。
    """
    if w <= 0 or h <= 0:
        return False
    ar = w / float(h) if h > w else h / float(w)  # 取 ≤1 的值（短边/长边）
    return ar >= 0.25  # 即宽高比在 0.25 ~ 4.0 之间


# ── ⭐ v19.15 照片图标检测缓存（多块比对场景只跑一次） ──
#   缓存内容：merge + 尺寸/宽高比过滤后的【最终图标列表】（剔除文本重叠前一步），
#   因文本重叠依赖调用方的 text_bboxes，不能跨调用复用，所以这一步放后面做。
from collections import OrderedDict
_ICON_CACHE = OrderedDict()
_ICON_CACHE_MAX = 64

def _photo_cache_key(img_path):
    try:
        st = os.stat(img_path)
        return (img_path, int(st.st_mtime), int(st.st_size))
    except Exception:
        return (img_path, 0, 0)

def _photo_cache_get(key):
    if key in _ICON_CACHE:
        _ICON_CACHE.move_to_end(key)  # LRU touch
        return _ICON_CACHE[key]
    return None

def _photo_cache_set(key, value, size=None):
    # ⭐ v19.22：缓存同时存 (icons, (W,H))，供命中时做"保留 QR"的文字重叠过滤
    _ICON_CACHE[key] = (value, size)
    _ICON_CACHE.move_to_end(key)
    while len(_ICON_CACHE) > _ICON_CACHE_MAX:
        _ICON_CACHE.popitem(last=False)


# ── ⭐ v19.15 zxing-cpp QR 解码（高鲁棒、纯 pip、无 DLL 依赖） ──
def _zxing_available():
    try:
        import zxingcpp  # noqa: F401
        return True
    except Exception:
        return False


def _detect_qr_zxing(pil, W, H, max_one=True):
    """用 zxing-cpp 解码照片中的真实二维码/条码。
    返回 [(x,y,w,h), ...]（原图坐标，scale=1.5 时已回算）。
    策略：1.0x 先扫 ~70ms（多数清晰照一次过），失败再 1.5x 上采样扫 ~200ms。
    参数 max_one=True：保留 v19.14 "全图限 1 个 QR" 规则（产品标签不会同时有 2 个 QR）。
    返回空 list 时调用方应走 finder-cluster 兜底。
    """
    try:
        import zxingcpp
        import cv2
        import numpy as np
    except Exception:
        return []
    try:
        bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        return []
    # ⭐ 关键策略：跑完全部尺度，按面积挑最大 bbox（真 QR 永远比误检的"字符拼接图案"大），
    #   避免一遇小误检就 break 漏掉真 QR。
    candidates = []  # [(area, (x,y,w,h))]
    for scale in (1.0, 1.5):
        try:
            img = bgr if scale == 1.0 else cv2.resize(
                bgr, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_CUBIC)
            # try_downscale 关掉：让外层显式控尺度，避免 zxing 内降一档出误检
            rs = zxingcpp.read_barcodes(img, try_rotate=True,
                                        try_downscale=False, try_invert=True)
        except Exception:
            continue
        for r in rs:
            try:
                p = r.position
                if p is None:
                    continue
                tlx, tly = p.top_left.x, p.top_left.y
                brx, bry = p.bottom_right.x, p.bottom_right.y
                # 防御：手机斜拍/倒置会让 y 轴倒置（tl.y > br.y）。统一按 min/max 规范化。
                x0, x1 = (min(tlx, brx), max(tlx, brx))
                y0, y1 = (min(tly, bry), max(tly, bry))
                x, y = int(x0 / scale), int(y0 / scale)
                w, h = int((x1 - x0) / scale), int((y1 - y0) / scale)
                if w <= 0 or h <= 0:
                    continue
                # 尺寸合理性（占短边 0.5%~30%）
                short = min(W, H)
                rel = max(w, h) / float(short)
                if rel < 0.005 or rel > 0.30:
                    continue
                # 宽高比宽松：斜拍 QR 可呈长条（aspect≥0.30 即接收）
                #   安全靠「按面积挑最大」兜底——真 QR 远大于"字符拼接"误检
                ar = min(w, h) / float(max(w, h)) if max(w, h) > 0 else 0
                if ar < 0.30:
                    continue
                candidates.append((w * h, (x, y, w, h)))
            except Exception:
                continue
    if not candidates:
        return []
    candidates.sort(key=lambda t: -t[0])
    if max_one:
        return [candidates[0][1]]
    return [c[1] for c in candidates]  # all, descending by area


def _icon_has_qr_structure(icon, finders, expand=0.25):
    """⭐ v19.27：判断检测框是否含真 QR 结构（内部 ≥3 个定位符）。
    平滑块/手指/表面误检通常不含定位符；真 QR 必含 3 个定位符。
    用于照片侧校验 zxing 解码的方形候选是否为真实二维码。"""
    try:
        x, y, w, h = icon
        ew, eh = int(w * expand), int(h * expand)
        x0, y0 = x - ew, y - eh
        x1, y1 = x + w + ew, y + h + eh
        n = sum(1 for f in finders if x0 <= f[0] <= x1 and y0 <= f[1] <= y1)
        return n >= 3
    except Exception:
        return False


def _icon_edge_density(gray, icon):
    """⭐ v19.27：检测框内部边缘密度（Canny 强边像素占比 [0,1]）。
    真实印刷标记(QR/条码/logo/NFC)内部有丰富边缘；平滑手指/表面是低纹理块。
    照片侧用于剔除无内部结构的密矩形误检。"""
    try:
        import cv2
        x, y, w, h = [int(v) for v in icon]
        gh, gw = gray.shape
        x = max(0, x); y = max(0, y)
        x2 = min(gw, x + w); y2 = min(gh, y + h)
        roi = gray[y:y2, x:x2]
        if roi.size == 0:
            return 0.0
        roi = cv2.resize(roi, (max(32, w), max(32, h)))
        edges = cv2.Canny(roi, 50, 150)
        return cv2.countNonZero(edges) / float(edges.size)
    except Exception:
        return 0.0


def _detect_icon_regions(img_path, text_bboxes=None, is_photo=False):
    """模型无关的图标检测：QR(zxing-cpp/pyzbar/finder 聚类) + 条形码(pyzbar) + NFC/logo(保守密矩形)。
    返回 [(x,y,w,h), ...] 列表。不加载 RapidOCR，单次秒级。
    text_bboxes: 已知文本区域 [(x,y,w,h), ...]，与其重叠的候选将剔除（避免把文字当图标）。
    is_photo: True=实拍照片（zxing + 缓存 + 收紧密矩形）；False=CAD切块（finder+pyzbar全功能）。

    ⭐ v19.15 改造：
      - 照片侧首选 zxing-cpp 解码（高鲁棒 + 自动多方向/缩放/反转）
      - pyzbar 保留兜底（仅在 zxing 未命中时参与）
      - finder-cluster 仅在【解码器无一命中】时参与（避免 zxing 已命中的正确 QR 被噪声 finder 包围）
      - 照片结果按 (path,mtime,size) LRU 缓存（多块比对场景只跑一次；剔除文本重叠在缓存外）
    """
    # ⭐ 缓存命中：直接复用，重做仅文本重叠过滤（依赖调用方 text_bboxes，无法缓存）
    if is_photo:
        _key = _photo_cache_key(img_path)
        _cached = _photo_cache_get(_key)
        if _cached is not None:
            _cached_icons, _cached_size = _cached if isinstance(_cached, tuple) else (_cached, None)
            tb = [b if len(b) == 4 else _bbox_rect(b) for b in (text_bboxes or [])]
            # ⭐ v19.22 修正：命中缓存时同样保留 QR 类图标（与写入路径一致），
            #    否则"先写(无text)后读(有text)"的两次调用会丢掉 QR→"缺图标"假红框。
            _sz = _cached_size if _cached_size else (100000, 100000)
            return [ic for ic in _cached_icons
                    if _is_qr_like(ic, _sz) or not _overlaps_any(ic, tb)]

    try:
        import numpy as np
        from PIL import Image
        import cv2
        pil = Image.open(img_path).convert("RGB")
        W, H = pil.size
        g = np.array(pil.convert("L"))
        icons = []
        # ⭐ v19.27 照片侧预先算定位符（供给 zxing 方形候选的 QR 结构校验 + finder-cluster 复用）
        _finders = None
        if is_photo:
            try:
                _finders = _qr_finders(g)
            except Exception:
                _finders = []
        # 1) ⭐ v19.15 zxing-cpp 工业级 QR 解码（pure pip，无 DLL）。只在照片侧启动：CAD 块图
        #    没有手机斜拍/反光/模糊等病态，v19.14 finder-cluster 已经够用，加上 zxing 会给每块
        #    多加 70~280ms（CAD 每块各跑一次，没有缓存受益）。照片侧保留 zxing 主位。
        zxing_found = False
        if is_photo and _zxing_available():
            try:
                zx = _detect_qr_zxing(pil, W, H, max_one=True)
                if zx:
                    for _cand in zx:
                        _cw, _ch = _cand[2], _cand[3]
                        _ar = min(_cw, _ch) / float(max(_cw, _ch)) if max(_cw, _ch) > 0 else 0
                        if 0.5 <= _ar <= 2.0:
                            # ⭐ v19.27：方形候选=声称 QR，必须有 3 定位符结构否则是噪声误解码→丢弃
                            if _finders is not None and _icon_has_qr_structure(_cand, _finders):
                                icons.append(_cand)
                                zxing_found = True
                            # else：方形但无 3 定位符（手指/表面/字符拼接）→ 误检，丢弃
                        else:
                            # 非方形（1D 条码等无定位符）→ 直接保留
                            icons.append(_cand)
                            zxing_found = True
            except Exception:
                pass
        # 1.5) pyzbar 兜底（DLL 可用时仍是最快的 decoder）
        pyzbar_found = False
        try:
            from pyzbar.pyzbar import decode
            for d in decode(pil):
                r = d.rect
                icons.append((r.left, r.top, r.width, r.height))
                pyzbar_found = True
        except Exception:
            pass
        # 2) QR 定位符聚类（仅在所有解码器都未命中时走，避免 zxing 真 QR 被噪声 finder 包裹）
        #    v19.14 修复：照片也启用（生产 pyzbar 缺 DLL 不可用，此前照片完全无 QR 检测 →
        #    QR 区域 OCR 文本落入黄框误报）。防误检护栏：仅接受「恰好 3 定位符」的完美聚类，
        #    且全图限 1 个（产品标签最多 1 个 QR），尺寸/宽高比在合理范围。
        #    CAD 块图保持原有逻辑（允许 ≤3 聚类）。
        if not pyzbar_found and not zxing_found:
            try:
                finders = _finders if _finders is not None else _qr_finders(g)
                clusters = _cluster_finders(finders, W, H)
                if is_photo:
                    # ⭐ 照片严格模式：仅保留「恰好含 3 个定位符」的聚类（真 QR 特征）
                    #   且全图最多 1 个 QR（产品标签不会同时有 2 个 QR）
                    strict_clusters = [c for c in clusters if _is_strict_qr_cluster(c, finders, W, H)]
                    if len(strict_clusters) <= 1:
                        for c in strict_clusters:
                            icons.append(c)
                    # else: 多个疑似 QR → 可能是噪声（键盘/栅格），全部丢弃
                else:
                    # CAD 块图：安全上限 3 个（原有逻辑不变）
                    if len(clusters) <= 3:
                        for c in clusters:
                            icons.append(c)
                    # else: 静默丢弃过多聚类，不报错
            except Exception:
                pass
        # 3) NFC/logo 密矩形
        #    v18: 照片路径大幅收紧(min_side 22->36, max_ar 1.8->1.6, min_density 0.35->0.55)
        #    照片底部文字笔画产生25px级小方形密矩形被_is_qr_like误判为QR；CAD保持原参数。
        if is_photo:
            dr_params = dict(min_side=36, max_ar=1.6, min_density=0.55)
        else:
            dr_params = dict(min_side=22, max_ar=1.8, min_density=0.35)
        try:
            for (x, y, bw, bh, _dens) in _dense_rects(g, **dr_params):
                if is_photo:
                    # ⭐ v19.27 照片侧密矩形门控：平滑块(手指/表面)无内部边缘结构→丢弃，
                    # 仅保留真实印刷标记(QR/条码/logo/NFC，内部有丰富边缘)。校准：误检平滑块
                    # edge≈0.045，真实标记(CAD 代理)≥0.107。阈值 0.06 留出安全余量。
                    if _icon_edge_density(g, (x, y, bw, bh)) < 0.06:
                        continue
                icons.append((x, y, bw, bh))
        except Exception:
            pass
        icons = _merge_rects(icons)
        # 尺寸过滤：真实图标（QR/NFC/Logo）不会超过短边 18%（块图 ~80px，照片 ~200px）
        # 旧阈值 W*0.7 在大尺寸照片上放过 300+px 的误检（如桌面/面板被当成图标）
        max_icon = min(W, H) * 0.18
        icons = [(x, y, bw, bh) for (x, y, bw, bh) in icons
                 if bw >= 14 and bh >= 14 and bw <= max_icon and bh <= max_icon]
        # ⭐ 宽高比过滤：真实图标(QR/Logo/NFC)宽高比通常在 0.4~2.5 范围；
        #   散热栅格(竖长条 ar>3.0) / 横向文字条(ar<0.25) 在此被剔除。
        #   此过滤放在文本重叠过滤之前，减少后续计算量。
        icons = [(x, y, bw, bh) for (x, y, bw, bh) in icons
                 if _is_icon_aspect_ratio_ok(bw, bh)]

        # ⭐ v19.15 缓存写入（文本重叠过滤之前）
        if is_photo:
            _photo_cache_set(_key, icons, size=(W, H))

        # 排除与文本重叠的候选（消假阳：文字/表格线不是图标）
        if text_bboxes:
            tb = [b if len(b) == 4 else _bbox_rect(b) for b in text_bboxes]
            # ⭐ v19.22 修正：二维码(zxing 解码)是高分确信图标，且标签文字本就与二维码相邻，
            #    即使与文本 bbox 重叠也必须保留——否则被误删→照片侧 QR 变空→"缺图标"假红框。
            #    仅对普通图标(非 QR)做文字重叠过滤以消假阳。
            icons = [ic for ic in icons
                     if _is_qr_like(ic, (W, H)) or not _overlaps_any(ic, tb)]
        return icons
    except Exception:
        return []


def _photo_qr_rects(img_path):
    """用 pyzbar 解码照片中的真实二维码/条形码，返回其 bbox 列表。
    用于区分照片图标类型：pyzbar 解码的是高可靠二维码，密矩形误检的文字区不会解码。
    """
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
        return [(d.rect.left, d.rect.top, d.rect.width, d.rect.height) for d in decode(Image.open(img_path))]
    except Exception:
        return []


# 图标位置匹配阈值：归一化中心坐标欧氏距离上限。
# 仅判「当前位置」有没有图标，需较严；但手持斜拍透视位移常达 0.25~0.35，故取 0.25 折中。
ICON_MATCH_THRESHOLD = 0.25

# ⭐ 2026-07-28 v17 修复：QR 码特殊匹配阈值（放宽位置+尺寸容忍）
#   QR 在块图用 _qr_finders(定位符聚类) 检测、在照片用 pyzbar(解码) 检测，
#   两种方法产生的 bbox 差异较大，需要比普通图标更宽松的匹配条件。
QR_MATCH_THRESHOLD = 0.38    # 归一化中心距离（QR 允许更大偏移）
QR_SIZE_RATIO_LO = 0.15     # 尺寸比例下限（原 0.33，CAD vs 斜拍可差 6x+）
QR_SIZE_RATIO_HI = 6.5       # 尺寸比例上限（原 3.0）
QR_ASPECT_DIFF_MAX = 0.50    # 宽高比差异上限（原 0.35）


def _is_qr_like(icon, size):
    """判断一个检测框是否像 QR 码（正方形/近正方形，相对尺寸合理）。"""
    x, y, w, h = icon
    if w <= 0 or h <= 0:
        return False
    W, H = size
    # 宽高比接近 1.0（正方形）
    aspect = min(w, h) / max(w, h)
    if aspect < 0.65:
        return False
    # 相对尺寸不过大（QR 不会占整图 25% 以上）
    rel = max(w / float(W), h / float(H)) if W > 0 and H > 0 else 0
    if rel > 0.25:
        return False
    return True


def _is_strict_qr_cluster(cluster, all_finders, img_w, img_h):
    """⭐ v19.27 照片 QR 严格验证：聚类是否为真二维码（非端子/螺丝/栅格/边角金属件误检）。

    真 QR 的 3 个定位符呈【L 型布局】：左上(TL) / 右上(TR) / 左下(BL) 三角均有定位符，
    右下(BR)为数据区无定位符。随机聚成方形的 3 个噪点（如边角金属反光）不满足该布局→拒。
    其余判据（近正方形 / 相对尺寸 2%~20% / 恰好 3 定位符 / 绝对尺寸≥20px）保留。
    """
    try:
        import numpy as np
        cx, cy, cw, ch = cluster
        if cw <= 0 or ch <= 0:
            return False

        # 1) 宽高比检查：QR 是近正方形
        ar = cw / float(ch) if ch > 0 else 0
        if ar < 0.5 or ar > 2.0:
            return False

        # 2) 相对尺寸：QR 占短边 2%~18%
        short = min(img_w, img_h)
        rel_size = max(cw, ch) / float(short) if short > 0 else 0
        if rel_size < 0.02 or rel_size > 0.20:
            return False

        # 4) 绝对尺寸下限：照片中 QR 至少 20px（排除噪点）
        if min(cw, ch) < 20:
            return False

        # 3) 取出聚类内定位符，校验 L 型布局（⭐ v19.27 新增，核心防误检）
        inside = [f for f in all_finders
                  if cx <= f[0] <= cx + cw and cy <= f[1] <= cy + ch]
        if len(inside) != 3:
            return False
        xs = [f[0] for f in inside]
        ys = [f[1] for f in inside]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        xr = (xmax - xmin) or 1
        yr = (ymax - ymin) or 1
        m = 0.35  # 角点容差（占定位符分布范围比例，容忍透视/斜拍偏移）
        occ_tl = occ_tr = occ_bl = False
        for f in inside:
            fx, fy = f[0], f[1]
            if fx <= xmin + m * xr and fy <= ymin + m * yr:
                occ_tl = True
            if fx >= xmax - m * xr and fy <= ymin + m * yr:
                occ_tr = True
            if fx <= xmin + m * xr and fy >= ymax - m * yr:
                occ_bl = True
        # 真 QR 必有 TL+TR+BL 三角（右下为数据区可空）；仅一对角线排列的噪点→拒
        return occ_tl and occ_tr and occ_bl
    except Exception:
        return False


def _compare_icons(block_icons, photo_icons, bsize, psize, photo_path=None,
                   is_override=False):
    """块图标 vs 照片图标：按归一化位置匹配，判定 都有 / 缺 / 多。
    返回 (present, missing, extra)，均为【块图空间】的 (x,y,w,h)。

    ⭐ 2026-07-28 对齐用户规则：
      图标/二维码不识别内容，只识别「当前位置」有没有图标图形。
      - 块位置有 + 照片同位置有 → 都有（present），不标示
      - 块位置有 + 照片同位置无 → 缺图标（missing），红框画在块图该位置
      - 块位置无 + 照片同位置有 → 多图标（extra），红框画在块图
        「照片图标归一化位置映射处」（块图与照片布局不同，位置为近似）
      - 都有或都没有 → 不标示
      - 所有图纸统一适用

    ⭐ 2026-07-28 v17 修复：
      增加 QR 特殊匹配路径（两侧都有正方形类检测→自动配对），解决 QR 被
      同时报"缺"和"多"的问题。普通图标保持原有严格门控。
    ⭐ v19.22 二维码改存在性匹配：块(标签裁剪)与照片(整机)取景不同、坐标不可对应，
      按用户规则"都有就不标"——两侧都检测到 QR 即判都有→不标；仅一侧无 QR 才判缺/多。
      不再依赖位置距离阈值（该阈值在跨取景场景下会误判"缺图标"）。
    """
    bw, bh = bsize
    pw, ph = psize

    def ncenter(ic, W, H):
        x, y, w, h = ic
        return ((x + w / 2.0) / W, (y + h / 2.0) / H)

    def _size_ratio_consistent(b_ic, p_ic,
                               lo=0.33, hi=3.0, aspect_max_diff=0.35):
        """检查两个图标的相对尺寸比例是否一致（跨图匹配门控）。"""
        bw_rel = b_ic[2] / float(bw) if bw > 0 else 0
        bh_rel = b_ic[3] / float(bh) if bh > 0 else 0
        pw_rel = p_ic[2] / float(pw) if pw > 0 else 0
        ph_rel = p_ic[3] / float(ph) if ph > 0 else 0
        if pw_rel <= 0 or ph_rel <= 0:
            return False
        r_w = bw_rel / pw_rel
        r_h = bh_rel / ph_rel
        size_ok = (lo <= r_w <= hi) and (lo <= r_h <= hi)
        if not size_ok:
            return False
        b_aspect = min(b_ic[2], b_ic[3]) / max(b_ic[2], b_ic[3]) if max(b_ic[2], b_ic[3]) > 0 else 0
        p_aspect = min(p_ic[2], p_ic[3]) / max(p_ic[2], p_ic[3]) if max(p_ic[2], p_ic[3]) > 0 else 0
        aspect_diff = abs(b_aspect - p_aspect)
        if aspect_diff > aspect_max_diff:
            return False
        return True

    matched_block = set()
    used_photo = set()

    # ── Phase 1: QR 特殊匹配路径 ──
    # QR 码在两侧的检测方式不同（_qr_finders vs pyzbar），bbox 差异大，
    # 但 QR 是唯一性强的特征（通常每产品只有一个），用宽松条件优先配对。
    #
    # ⭐ v19.25 修复：_is_qr_like 的 rel_size 上限 20% 是为实时检测设计的噪声过滤，
    #    但 DB 里存储的合法图标 bbox 在小尺寸块图上可能占比更高（如 31%），
    #    导致已验证的 QR 图标被误拒→落入 Phase 2 位置匹配失败→假"缺图标"。
    #    当 is_override=True（图标来自 DB/已验证）时，对 block_icons 放宽：
    #    除标准 _is_qr_like 外，额外接受「正方形 + 足够大(>15%短边)」的图标。
    def _block_qr_candidate(ic):
        if _is_qr_like(ic, bsize):
            return True
        if is_override:
            # DB 已验证图标：正方形(ar 0.5~2.0) + 占短边 15%~50% = 大 QR 在小块图上的合理范围
            ar = ic[2] / float(ic[3]) if ic[3] > 0 else 0
            if 0.5 <= ar <= 2.0:
                short = min(bsize)
                rel = max(ic[2], ic[3]) / float(short) if short > 0 else 0
                if 0.15 <= rel <= 0.50:
                    return True
        return False

    block_qr_candidates = [i for i, ic in enumerate(block_icons) if _block_qr_candidate(ic)]
    photo_qr_candidates = [i for i, ic in enumerate(photo_icons) if _is_qr_like(ic, psize)]
    if block_qr_candidates and photo_qr_candidates:
        # ⭐ v19.22 修正：块(标签裁剪/CAD)与照片(整机实拍)是两种取景，二维码位置
        #    不可对应，按位置距离匹配会误判"缺/多"。按用户规则"都有就不用标"——
        #    只要两侧都检测到 QR(存在性)，即判都有→不标；仅当一侧完全没有 QR 才判缺/多。
        for bi in block_qr_candidates:
            matched_block.add(bi)
        for pi in photo_qr_candidates:
            used_photo.add(pi)

    # ── Phase 2: 普通图标位置匹配（非 QR 的剩余图标）──
    for bi, b in enumerate(block_icons):
        if bi in matched_block:
            continue
        bnx, bny = ncenter(b, bw, bh)
        best, bestd = None, 1e9
        for pi, p in enumerate(photo_icons):
            if pi in used_photo:
                continue
            pnx, pny = ncenter(p, pw, ph)
            d = ((bnx - pnx) ** 2 + (bny - pny) ** 2) ** 0.5
            if d < bestd:
                bestd, best = d, pi
        if best is not None and bestd < ICON_MATCH_THRESHOLD:
            if not _size_ratio_consistent(b, photo_icons[best]):
                continue
            matched_block.add(bi)
            used_photo.add(best)

    present = [b for bi, b in enumerate(block_icons) if bi in matched_block]
    # 缺图标：块图坐标，直接画红框
    missing = [b for bi, b in enumerate(block_icons) if bi not in matched_block]
    # 多图标：照片坐标 → 映射回块图空间（近似「图纸相应位置」）
    extra_raw = [p for pi, p in enumerate(photo_icons) if pi not in used_photo]
    extra = []
    for (px, py, pw2, ph2) in extra_raw:
        nx0 = px / float(pw) if pw > 0 else 0
        ny0 = py / float(ph) if ph > 0 else 0
        nw = pw2 / float(pw) if pw > 0 else 0
        nh = ph2 / float(ph) if ph > 0 else 0
        ew = max(int(nw * bw), 1)
        eh = max(int(nh * bh), 1)
        # ⭐ 过滤映射后过小的噪点框（块图空间 <12px 视觉不可见，属照片检测噪点，
        #   不应标红框；真实图标在块图上至少十数像素）
        if ew < 12 or eh < 12:
            continue
        extra.append((int(nx0 * bw), int(ny0 * bh), ew, eh))
    return present, missing, extra


# ── 字符级 diff（定位缺失/多出的具体字符）──
def _lcs_length(a, b):
    """最长公共子序列长度（动态规划）"""
    if not a or not b: return 0
    m, n = len(a), len(b)
    # 用短串做行以省内存
    if m < n: a, b, m, n = b, a, n, m
    dp = [0] * (n + 1)
    for i in range(1, m + 1):
        prev = 0
        for j in range(1, n + 1):
            cur = dp[j]
            if a[i-1] == b[j-1]:
                dp[j] = prev + 1
            else:
                dp[j] = max(dp[j], dp[j-1])
            prev = cur
    return dp[n]

def _char_diff_detail(block_text, photo_text):
    """字符级 diff：返回 (missing_in_photo, extra_in_photo) 字符列表。
    missing_in_photo: 块有但照片没有的字符
    extra_in_photo:   照片有但块没有的字符
    """
    if not block_text: return [], []
    if not photo_text: return list(block_text), []
    b_chars = list(block_text)
    p_chars = list(photo_text)
    # 用 Counter 找字符集差异（最稳健，避免 LCS 错位）
    from collections import Counter
    bc = Counter(b_chars)
    pc = Counter(p_chars)
    missing = []
    for c, n in bc.items():
        if pc[c] < n:
            missing.extend([c] * (n - pc[c]))
    extra = []
    for c, n in pc.items():
        if bc[c] < n:
            extra.extend([c] * (n - bc[c]))
    return missing, extra

def _estimate_char_bbox(line_bbox, full_text, matched_substring):
    """估算块中"未匹配部分"的 bbox（在子串之后的字符）。
    line_bbox: 整行的 (x, y, w, h)
    full_text: 块的完整文本
    matched_substring: 块中匹配上的子串（作为锚点）
    返回 (x, y, w, h) 表示未匹配部分的 bbox
    """
    if not full_text or not matched_substring: return None
    # 找到子串在块中的位置
    idx = full_text.find(matched_substring)
    if idx < 0: return None
    x, y, w, h = line_bbox
    if w <= 0: return None
    # 子串结尾位置
    match_end = idx + len(matched_substring)
    # 未匹配部分从 match_end 开始
    # 估算 bbox：位置 match_end 之后，宽度为剩余字符占比
    if match_end >= len(full_text): return None
    # 子串在原图中的水平范围（粗略按字符等宽估算）
    char_w = w / max(len(full_text), 1)
    start_x = x + int(char_w * match_end)
    end_x = x + int(char_w * len(full_text))
    return (start_x, y, max(end_x - start_x, 3), h)


# ── override 文本 → OCR bbox 映射 ──
def _map_override_to_ocr(override_lines, ocr_lines):
    """把数据库里的块 RawText（按 \\n 切行）逐行匹配到块图 OCR 得到的 bbox。
    策略：优先精确匹配（去空格/小写后相等），其次用子串/Jaccard 找最相似的 OCR 行。
    返回 [{text, bbox, conf}, ...]，顺序与 override_lines 一致。
    """
    if not override_lines:
        return []
    used = set()
    result = []
    for ot in override_lines:
        ot_norm = re.sub(r"\s+", "", ot).lower()
        best = None
        best_score = -1.0
        for i, ocr in enumerate(ocr_lines or []):
            if i in used: continue
            ot_text = (ocr.get("text") or "").strip()
            ocr_norm = re.sub(r"\s+", "", ot_text).lower()
            if not ocr_norm: continue
            # 精确匹配
            if ot_norm == ocr_norm:
                best = (i, ocr); best_score = 2.0; break
            # 子串匹配
            if ot_norm and (ot_norm in ocr_norm or ocr_norm in ot_norm):
                sc = 1.0 + min(len(ot_norm), len(ocr_norm)) / max(len(ot_norm), len(ocr_norm), 1)
                if sc > best_score:
                    best = (i, ocr); best_score = sc
                continue
            # Jaccard
            j = _chars_overlap(ot, ot_text)
            if j > best_score:
                best = (i, ocr); best_score = j
        if best is None and ocr_lines:
            # 没找到匹配 → 用第一个未使用的 OCR 行作为占位
            for i, ocr in enumerate(ocr_lines):
                if i not in used:
                    best = (i, ocr); break
        if best is not None:
            i, ocr = best
            used.add(i)
            result.append({"text": ot, "bbox": ocr.get("bbox", [[0,0],[10,0],[10,10],[0,10]]),
                           "conf": ocr.get("conf", 1.0)})
        else:
            result.append({"text": ot, "bbox": [[0,0],[10,0],[10,10],[0,10]], "conf": 1.0})
    return result


# ── 文字区域检测（RapidOCR text_detector，仅检测不识别，bboxes 准确）──
def _fast_text_detection(img_path):
    """用 RapidOCR 的 text_detector 做文字区域检测，返回 [{text:'', bbox, conf}, ...]。
    不识别文本内容（比完整 OCR 快 5x+），但 bboxes 精确对应实际文字位置。
    """
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
        pil = Image.open(img_path).convert("RGB")
        w, h = pil.size
        max_dim = 960
        scale_back = 1.0
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            pil = pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            scale_back = max(w, h) / max_dim
        img_arr = np.asarray(pil)
        engine = RapidOCR()
        # 只调检测器，跳过识别（快 5x+）。返回 (boxes_array, score)：boxes shape (N,4,2)
        result = engine.text_detector(img_arr)
        boxes_arr = result[0] if result else None
        lines = []
        if boxes_arr is not None:
            for box in boxes_arr:
                # box shape (4, 2): 4 个角点 (x, y)
                xs = [float(p[0]) * scale_back for p in box]
                ys = [float(p[1]) * scale_back for p in box]
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                w_box, h_box = x1 - x0, y1 - y0
                if w_box < 8 or h_box < 6: continue
                if w_box * h_box < 100: continue
                if w_box > w * 0.95: continue
                lines.append({
                    "text": "",
                    "bbox": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                    "conf": 0.5
                })
        return lines
    except Exception as e:
        return [{"text": "", "bbox": [[0, 0], [100, 0], [100, 50], [0, 50]], "conf": 0}]


# ── 自动扶正（deskew）：检测照片倾斜角度并旋转校正 ──
def _deskew_image(pil_img):
    """检测照片中文本行的主导倾斜角，旋转使文本水平。
    算法：
      1. 用 RapidOCR text_detector（仅检测不识别，~200ms）获取文本行 bbox
      2. 取每行顶边的角度，按行宽度加权中位数
      3. 若 |median_angle| > 0.5° 则旋转校正，否则原样返回
    返回 (deskewed_pil, angle_degrees)。
    """
    import math
    import numpy as np
    try:
        from rapidocr_onnxruntime import RapidOCR
        # 缩小加速检测
        max_dim = 800
        scale_back = 1.0
        w, h = pil_img.size
        work = pil_img.convert("RGB")
        if max(w, h) > max_dim:
            s = max_dim / max(w, h)
            scale_back = 1.0 / s
            work = work.resize((int(w * s), int(h * s)), Image.LANCZOS)
        img_arr = np.asarray(work)
        engine = RapidOCR()
        det_result = engine.text_detector(img_arr)
        if det_result is None or det_result[0] is None:
            return pil_img, 0.0
        boxes = det_result[0]  # (N, 4, 2)
        if len(boxes) == 0:
            return pil_img, 0.0
        # 收集每个文本行的角度（顶边方向）和宽度
        angles = []
        for box in boxes:
            # box: [[x0,y0],[x1,y1],[x2,y2],[x3,y3]] — 通常逆时针4角
            # 取最上面的边作为"文本行方向"
            pts = [(float(box[i][0]), float(box[i][1])) for i in range(4)]
            # 找 y 最小的两个点（顶边）
            by_y = sorted(range(4), key=lambda i: pts[i][1])
            top_a, top_b = by_y[0], by_y[1]
            dx = pts[top_b][0] - pts[top_a][0]
            dy = pts[top_b][1] - pts[top_a][1]
            width = math.hypot(dx, dy)
            if width < 5:
                continue
            angle = math.degrees(math.atan2(dy, dx))
            # 归一化到 [-90, +90]（无论从左到右还是从右到左）
            if angle > 90:
                angle -= 180
            elif angle < -90:
                angle += 180
            angles.append((angle, width * scale_back))  # 用原图尺度加权
        if not angles:
            return pil_img, 0.0
        # 加权中位数角度
        angles.sort(key=lambda x: x[0])
        total_len = sum(l for _, l in angles)
        cumul = 0.0
        median_angle = angles[0][0]
        for a, l in angles:
            cumul += l
            if cumul >= total_len * 0.5:
                median_angle = a
                break
        # 只有超过阈值才旋转
        if abs(median_angle) < 0.3:
            return pil_img, 0.0
        # PIL 旋转（按 median_angle 方向转回，expand=True 防裁切）
        #   median_angle < 0 → 文本左高右低 → 图像顺歪 → 需顺时针旋转(负角)校正
        rotated = pil_img.rotate(median_angle, expand=True, resample=Image.BICUBIC,
                                  fillcolor=(255, 255, 255))
        return rotated, round(median_angle, 2)
    except Exception:
        return pil_img, 0.0


# ── 主流程 ──
def run(photo_path, block_path, output_path, block_bbox_cache=None, block_text_override=None, photo_text_override=None, icon_regions_override=None):
    """
    block_bbox_cache: 已 OCR 的块 bbox+text 列表（直接复用，跳过块 OCR）
    block_text_override: 来自数据库的块 RawText（按 \\n 切行后逐行匹配到 OCR bbox）
                         提供后，文本对比以 override 为准，避免块图 OCR 与入库文本不一致导致假差异
    photo_text_override: 来自 C# OCR 的照片文本（避免 Python 端重复 OCR 照片）
                         提供后，仅 OCR 照片取 bbox，文本以 override 为准
    """
    result = {
        "success": False,
        "_debugVersion": "v19.28",  # ⭐ 调试标记：确认生产加载版本
        "redRegions": [],
        "yellowRegions": [],
        "grayRegions": [],
        "greenRegions": [],
        "photoExclusive": [],
        "blockExclusive": [],
        "cadOnly": [],
        "matchedPhotoLines": [],
        "matchedBlockLines": [],
        "missingChars": [],        # 块中缺失的具体字符（字符级 diff）
        "extraChars": [],          # 照片中多出的具体字符
        "iconRegions": [],         # 块期望图标（位置/显示用）
        "iconMissingRegions": [],  # 块有照片无 → 缺图标（红框）
        "iconExtraRegions": [],    # 照片有块无 → 多图标，红框画在块图「照片图标归一化映射位置」
        "silentSkipped": [],       # ⭐ v19.4: 完全隐形跳过的文本（序列号等），JSON上报但图上不画任何框
        "error": ""
    }
    try:
        # ⭐ v19.14 输入预检：提前发现文件问题，避免深层异常
        for label, path in [("photo", photo_path), ("block", block_path)]:
            if not os.path.isfile(path):
                result["error"] = f"文件不存在({label}): {path}"
                return result
        if not os.path.isdir(os.path.dirname(output_path) or "."):
            result["error"] = f"输出目录不存在: {output_path}"
            return result

        # ── 0. 照片自动扶正（消除拍摄角度影响）──
        #   块图是 CAD 生成的，无需扶正；仅对照片做 deskew。
        #   扶正后的图用于 OCR + 图标检测（结果更稳定一致）。
        #   原始照片保持不变（用户要求返回拍照图不做任何标示）。
        photo_pil_raw = Image.open(photo_path).convert("RGB")
        photo_pil_deskewed, deskew_angle = _deskew_image(photo_pil_raw)
        # 将扶正后的照片写入临时文件（OCR/icon 检测需要文件路径）
        import tempfile
        _photo_work = photo_path  # 默认用原图
        if abs(deskew_angle) >= 0.3:
            fd, _photo_work = tempfile.mkstemp(suffix=".jpg", prefix="deskew_")
            os.close(fd)
            photo_pil_deskewed.save(_photo_work, quality=92)
            result["deskewAngle"] = round(deskew_angle, 2)

        # 1. 照片 OCR：⭐ Phase1(v19.18) 统一走 live RapidOCR（与块图同引擎/同预处理）。
        #    生产路径 C# 第6参(photo_text_override) 为空 → 此处直接 live；
        #    非生产路径若传入 override，仍以 live 为主、override 仅作 live 失败时的兜底。
        #    这确保照片侧文本来自 RapidOCR，与块图侧一致，根除 R3(双源不一致)。
        live_photo = _ocr_image(_photo_work)
        if photo_text_override:
            override_photo_lines = [s.strip() for s in re.split(r'[\n\r]+', str(photo_text_override)) if s.strip() and len(s.strip()) >= 2]  # 按\n分行，不拆词
            # live 为主；仅当 live 几乎为空(照片 OCR 失败)才整段回退 override（占位 bbox）
            if len(live_photo) <= 1:
                photo_lines = [{"text": t, "bbox": [[0, 0], [10, 0], [10, 10], [0, 10]], "conf": 1.0} for t in override_photo_lines]
            else:
                photo_lines = live_photo
        else:
            photo_lines = live_photo
        # 块 OCR：⭐ Phase1(v19.18) 统一走 live RapidOCR 为主，DB RawText(override) 仅作兜底。
        #    —— 根除 R2(陈旧 DB 作为唯一基准污染匹配)：之前块文本来自 segmentation 时的
        #       DB RawText(可能小字漏读/人工错改)，与照片 live RapidOCR 不同源 → 系统性假差异。
        #       现在块图始终实时 RapidOCR，DB RawText 只在 live 几乎为空(OCR失败)时回退。
        live_block = _ocr_image(block_path)
        # ⭐ 优先复用 C# 缓存的块 OCR（同为 live RapidOCR，避免每块重复 OCR 拖累全量回归）
        if isinstance(block_bbox_cache, list) and block_bbox_cache and isinstance(block_bbox_cache[0], dict):
            live_block = block_bbox_cache
        if block_text_override and len(live_block) <= 1:
            override_lines = [l.strip() for l in str(block_text_override).split("\n") if l.strip()]
            block_lines = _map_override_to_ocr(override_lines, live_block)
            if not block_lines:
                # 兜底：用 override 文本（无 bbox，后续画框会跳过）
                block_lines = [{"text": t, "bbox": [[0, 0], [10, 0], [10, 10], [0, 10]], "conf": 1.0} for t in override_lines]
        else:
            block_lines = live_block
        if not photo_lines and not block_lines:
            result["error"] = "双方 OCR 均无文本"
            return result

        # 2. 加载图块原图和照片原图
        block_pil = Image.open(block_path).convert("RGB")
        photo_pil = Image.open(_photo_work).convert("RGB")  # ⭐ 用扶正后的照片做对比基准
        
        # 2.5 图标检测与对比（模型无关，不加载 RapidOCR）
        #   - 块图标：优先用「持久化图标框」(icon_regions_override)；否则检测块图
        #   - 照片图标：直接检测照片（pyzbar + QR 定位符 + 密矩形，秒级）
        #   - 对比：按归一化位置匹配，块有照片无 → 缺图标(红)；照片有块无 → 多图标(计数)
        # ⭐ 不比对图标准确性（不解码内容），只判断"当前位置是否有图标"
        block_icons = []
        photo_icons = []
        block_icons_raw = []
        photo_icons_raw = []
        try:
            # 文本 bbox（用于剔除与文字重叠的图标误检）
            block_text_bboxes = [_bbox_rect(bl["bbox"]) for bl in block_lines if bl.get("bbox")]
            # ⭐ 照片图标过滤文字误检：复用 Phase1 已算出的 live_photo（同一次 RapidOCR，
            #   不再重复 OCR），取其 bbox 作为文字过滤依据。否则文字密集区(型号/热线)被
            #   dense_rects 误检为图标 → 假"多标"(黄框)。块侧图标本就过滤文字，此处对称。
            photo_text_bboxes = [_bbox_rect(pl["bbox"]) for pl in live_photo if pl.get("bbox")] or None
            # 原始图标集（不过滤文本重叠）：用于"文本落在图标内→跳过"判断，
            # 防止 QR 区域 OCR 噪声(如 S'Oft)被当成缺标。即便带过滤的检测误删了真实 QR，
            # 此原始集仍保留，可正确捕获邻近噪声文本。
            block_icons_raw = _detect_icon_regions(block_path)
            photo_icons_raw = _detect_icon_regions(_photo_work, is_photo=True)
            if icon_regions_override is not None:
                block_icons = [tuple(icon) for icon in icon_regions_override]
                block_icons_raw = block_icons
            else:
                block_icons = _detect_icon_regions(block_path, text_bboxes=block_text_bboxes)
            photo_icons = _detect_icon_regions(_photo_work, text_bboxes=photo_text_bboxes, is_photo=True)
            present, missing, extra = _compare_icons(
                block_icons, photo_icons, block_pil.size, photo_pil.size,
                photo_path=_photo_work,
                is_override=(icon_regions_override is not None))
            # ⭐ 核心规则(2026-07-28 对齐用户)：文本差异化为重点，图标只看位置存在性。
            #   ┌──────────────────────────────────────────────────────────────┐
            #   │ 文本 = 四色框详细标示（红=缺 / 黄=多 / 绿=一致 / 灰=CAD跳过）│
            #   │ 图标(含二维码) = 不识别内容，只看当前位置有没有：            │
            #   │   · 块有+照片同位置有 → 都有，不标示                          │
            #   │   · 块有+照片无     → 缺图标(iconMissingRegions)，红框        │
            #   │   · 块无+照片有     → 多图标(iconExtraRegions)，红框          │
            #   │   · 都有或都没有     → 不标示                                  │
            #   │ 所有图纸统一适用。                                           │
            #   └──────────────────────────────────────────────────────────────┘
            result["iconRegions"] = [list(t) for t in block_icons]       # 块期望图标(参考)
            result["iconMissingRegions"] = [list(t) for t in missing]    # 缺图标 → 红框
            result["iconExtraRegions"] = [list(t) for t in extra]        # 多图标 → 红框
        except Exception:
            result["iconRegions"] = []
            result["iconMissingRegions"] = []
            result["iconExtraRegions"] = []
        
        # 画在照片上（用户视角：产品照片直接看出哪里有差异）
        # 同时也在图块上画（标准参考：图块标注的差异位置）
        draw = ImageDraw.Draw(photo_pil)
        draw_block = ImageDraw.Draw(block_pil)

        # 3. 对照片每一行：与图块逐行严格对比
        for pl in photo_lines:
            try:
                pt = pl["text"]
                bbox = _bbox_rect(pl["bbox"])
                # ⭐ v19.20 修正：序列号检查必须在所有灰化规则之前（最优先）！
                #    v19.17 已将 _is_serial_number 移到 _text_overlaps_icon 之前，
                #    但 _clean_qr_garbage / _is_qr_ocr_noise 仍在序列号之前执行，
                #    导致序列号文本被 QR 噪声规则误杀：
                #      - QHRLA(5字母) → 命中 _is_qr_ocr_noise 的 ^[A-Z0-9]{4,12}$ 规则
                #      - 200512(6位数字) → 被 _clean_qr_garbage 的 \d{6} 日期码规则剥离为空
                #      - 74U119I(7位字母数字) → 同样命中 _is_qr_ocr_noise
                #    序列号是明确的语义类别（产品追踪标识），必须在最前面被识别和分流，
                #    不应被任何基于"看起来像 QR 编码内容"的统计规则误杀。
                # ⭐ v19.21 双重序列号检测（raw + norm）：
                #    _is_serial_number_raw: 检查原始文本（防 _norm_text 尾部数字剥离破坏批次码如 BFF0001）
                #    _is_serial_number:     检查归一化文本（覆盖 OCR 混淆校正后的格式）
                if _is_serial_number_raw(pt) or _is_serial_number(_norm_text(pt)):
                    _ser_matched = False
                    for bl in block_lines:
                        if _is_qr_or_icon(bl["text"]):
                            continue
                        if _texts_match_strict(pt, bl["text"]):
                            _ser_matched = True
                            break
                    if _ser_matched:
                        result["greenRegions"].append(bbox)
                        result["matchedPhotoLines"].append(pt)
                        result["matchedBlockLines"].append(pt)
                    else:
                        # ⭐ v19.24 修正：按用户确认规则——序列号不同→完全不标(无框)。
                        #    仅相同部分标绿框即可；不同序列号每台唯一、比对无意义，不画任何框
                        #    (不是红/黄/灰，就是彻底不标)。记 unmarkedSerials 供调试但不渲染。
                        result.setdefault("unmarkedSerials", []).append(pt)
                    continue
                # ⭐ v19.16 剥离嵌入的二维码垃圾，避免拼合行(如'…NNFC')漏检导致假黄框
                pt_clean = _clean_qr_garbage(pt)
                if not pt_clean:
                    result["grayRegions"].append(bbox)
                    continue
                pt = pt_clean
                # 跳过 QR/图标（不作为文本差异）
                if _is_qr_or_icon(pt):
                    result["grayRegions"].append(bbox)
                    continue
                # OCR 噪声（含撇号/引号的伪影如 S'Oft）→ 灰化跳过
                if _is_ocr_noise(pt):
                    result["grayRegions"].append(bbox)
                    continue
                # ⭐ v19.14 QR 区域 OCR 噪声（QR 伪影词 / QR 解码内容码）→ 灰化跳过
                #    兜底：即使图标检测未捕获 QR 区域（pyzbar 缺失 + dense_rects 未命中），
                #    文本特征仍能识别 QR 噪声并跳过，防止黄框误报。
                if _is_qr_ocr_noise(pt):
                    result["grayRegions"].append(bbox)
                    continue
                # 文本 bbox 落在图标区域内（含扩展边距）→ 视为图标内噪声，跳过文本对比
                #    ⭐ v19.17/v19.20: 此检查在 _is_serial_number 之后，避免吞没序列号
                if _text_overlaps_icon(bbox, photo_icons_raw):
                    result["grayRegions"].append(bbox)
                    continue
                # ⭐ v19.19 修复：照片侧灰化规则收窄（仅 dimension + CJK garbage）
                #    旧代码用 _is_cad_annotation(pt) 一揽子灰化，但它内部调用 _is_annotation_note
                #    （命中 CAD_STOPWORDS"作业"→"A作业"被误灰）和 _is_lcd_display_text（命中"设定"→按钮标签被误灰）。
                #    这些规则是为 CAD 图纸文本设计的，套用到实物照片文字会误杀真实打标标识。
                #    照片侧仅保留两类明确非匹配文本的灰化：
                #      1) _is_dimension: 尺寸数字(80.5, 30±0.5 等)——永远不会是产品标识
                #      2) _is_cjk_garbage: 纯 OCR 噪声(3-6字无意义 CJK)——不是任何语言的真实词汇
                #    注：_is_serial_number 已在上面处理（silent/green），QR_noise 也已处理。
                if _is_dimension(pt):
                    result["grayRegions"].append(bbox)
                    continue
                if _is_cjk_garbage(_norm_text(pt)):
                    result["grayRegions"].append(bbox)
                    continue
                matched = False
                for bl in block_lines:
                    if _is_qr_or_icon(bl["text"]):
                        continue
                    if _texts_match_strict(pt, bl["text"]):
                        matched = True
                        break
                if matched:
                    result["greenRegions"].append(bbox)
                    result["matchedPhotoLines"].append(pt)
                else:
                    # ⭐ 跨块溢出过滤：照片标签条文本（型号/热线/NFC）因整行 bbox 过大，
                    #   常跨界覆盖尺寸/序列号块，导致这些块误报'照片有、块无'黄框（假多标）。
                    #   若照片文本含标签条特征，而当前块图 DB 文本完全不含任何标签条特征
                    #   （纯尺寸/序列号块），视为跨界噪声，跳过不报黄。
                    if _has_label_feature(pt) and not _block_has_label_feature(block_lines):
                        continue
                    result["yellowRegions"].append(bbox)
                    result["photoExclusive"].append(pt)
            except Exception as ex:
                # ⭐ v19.14 单行异常不崩整体：记录并跳过
                result["silentSkipped"].append(f"[STEP3_ERR:{type(ex).__name__}]")

        # 4. 对图块每一行：在照片中找匹配，CAD+QR 自动排除
        # ⭐ v7 优化：step 3 已匹配成功的文本直接标绿框（用块侧真实 bbox，避免重复匹配和 icon_overlap 误杀）
        matched_in_step3 = set(result.get("matchedBlockLines", []))
        for bl in block_lines:
            try:
                bt = bl["text"]
                bbox = _bbox_rect(bl["bbox"])
                # ⭐ v19.20 对称修复：step4 块图侧序列号也必须在最前面检查
                #    理由同 step3：_clean_qr_garbage / _is_qr_ocr_noise 会误杀序列号文本
                #    注意：块图侧序列号只需 silentSkipped（若照片侧有且匹配，step3 已标绿）
                # ⭐ v19.21 对称修复：step4 块图侧双重序列号检测（raw + norm）
                if _is_serial_number_raw(bt) or _is_serial_number(_norm_text(bt)):
                    # ⭐ v19.22 修正：块侧序列号也按规则执行匹配——与照片相同→绿框，
                    #    差异→灰框(预期公差，序列号每台不同)。旧逻辑无条件 silentSkipped(隐形)
                    #    导致块图序列号永远无框，违反"相同部分绿框"规则。
                    _ser_matched = False
                    for pl in photo_lines:
                        if _is_qr_or_icon(pl["text"]):
                            continue
                        if _texts_match_strict(bt, pl["text"]):
                            _ser_matched = True
                            break
                    if _ser_matched:
                        result["greenRegions"].append(bbox)
                    else:
                        # ⭐ v19.24 修正：按用户确认规则——序列号不同→完全不标(无框)。
                        result.setdefault("unmarkedSerials", []).append(bt)
                    continue
                # ⭐ v19.16 剥离嵌入的二维码垃圾，避免拼合行(如'…NNFC')漏检导致假红框
                bt_clean = _clean_qr_garbage(bt)
                if not bt_clean:
                    result["grayRegions"].append(bbox)
                    result["cadOnly"].append(bt)
                    continue
                bt = bt_clean
                # 已被 step 3 匹配 → 直接绿框（块侧 bbox 精确可见）
                if bt in matched_in_step3:
                    result["greenRegions"].append(bbox)
                    continue
                # QR/图标 → 灰色（不算差异）
                if _is_qr_or_icon(bt):
                    result["grayRegions"].append(bbox)
                    continue
                # OCR 噪声（含撇号/引号的伪影）→ 灰化跳过
                if _is_ocr_noise(bt):
                    result["grayRegions"].append(bbox)
                    result["cadOnly"].append(bt)
                    continue
                # ⭐ v19.14 QR 区域 OCR 噪声（块图 DB 中的 QR 伪影/解码内容）→ 灰化跳过
                if _is_qr_ocr_noise(bt):
                    result["grayRegions"].append(bbox)
                    result["cadOnly"].append(bt)
                    continue
                # 文本 bbox 落在图标区域内 → 仅对短文本（<4字符）灰化；长文本不拦截
                if _text_overlaps_icon(bbox, block_icons_raw) and len(bt.strip()) < 4:
                    result["grayRegions"].append(bbox)
                    continue
                # CAD 标注 → 灰色（不算差异）
                if _is_cad_annotation(bt):
                    result["grayRegions"].append(bbox)
                    result["cadOnly"].append(bt)
                    continue
                matched = False
                matched_photo_text = ""
                for pl in photo_lines:
                    if _is_qr_or_icon(pl["text"]):
                        continue
                    if _texts_match_strict(bt, pl["text"]):
                        matched = True
                        matched_photo_text = pl["text"]
                        break
                if matched:
                    result["greenRegions"].append(bbox)
                    result["matchedBlockLines"].append(bt)
                    # 字符级 diff：找出所有 photo line 在 block line 中的位置，未覆盖的字符为缺失
                    norm_bt = _norm_text(bt)
                    norm_pt = _norm_text(matched_photo_text) if matched_photo_text else ""
                    is_exact_substring = (norm_bt in norm_pt) or (norm_pt in norm_bt)
                    raw_sub = (bt in matched_photo_text) or (matched_photo_text in bt)
                    if matched_photo_text and matched_photo_text != bt and is_exact_substring and raw_sub:
                        matched_pt_texts = []
                        for pl2 in photo_lines:
                            if _is_qr_or_icon(pl2["text"]): continue
                            if _texts_match_strict(bt, pl2["text"]):
                                matched_pt_texts.append(pl2["text"])
                        if matched_pt_texts:
                            covered = set()
                            for pt in matched_pt_texts:
                                if bt and bt in pt:
                                    for ci in range(len(bt)):
                                        covered.add(ci)
                                    continue
                                start = 0
                                while True:
                                    pos = bt.find(pt, start)
                                    if pos < 0: break
                                    for ci in range(pos, pos + len(pt)):
                                        covered.add(ci)
                                    start = pos + 1
                            missing_positions = sorted(set(range(len(bt))) - covered)
                            meaningful_missing = [p for p in missing_positions if not str(bt[p]).isspace() and str(bt[p]) not in ',.;:，．、；']
                            if missing_positions and not meaningful_missing:
                                missing_positions = []
                            if missing_positions:
                                missing_chars = "".join(bt[i] for i in missing_positions)
                                result["missingChars"].append({
                                    "blockText": bt, "photoTexts": matched_pt_texts,
                                    "missing": missing_chars, "positions": missing_positions
                                })
                                char_w = bbox[2] / max(len(bt), 1)
                                groups = []
                                cur_group = [missing_positions[0]]
                                for pos in missing_positions[1:]:
                                    if pos == cur_group[-1] + 1:
                                        cur_group.append(pos)
                                    else:
                                        groups.append(cur_group)
                                        cur_group = [pos]
                                groups.append(cur_group)
                                for grp in groups:
                                    sp = grp[0]
                                    ep = grp[-1] + 1
                                    x = bbox[0] + int(char_w * sp)
                                    w = max(int(char_w * (ep - sp)), 4)
                                    result["redRegions"].append((x, bbox[1], w, bbox[3]))
                else:
                    result["redRegions"].append(bbox)
                    result["blockExclusive"].append(bt)
            except Exception as ex:
                result["silentSkipped"].append(f"[STEP4_ERR:{type(ex).__name__}]")

        # 5. 差异框只画在图块上（标准：不画在照片上——照片 bbox 不可靠，图块 bbox 精确）

        # 5.5 在图块上画框（标准参考）— 保留兼容
        # ⭐ v6 修复(2026-07-28)：灰框加深+过滤占位假坐标+iconExtra不画(坐标在照片空间)
        for r in result["redRegions"]:
            _draw_box(draw_block, r, (220, 38, 38), 3)
        for r in result["yellowRegions"]:
            _draw_box(draw_block, r, (217, 119, 6), 3)
        for r in result["greenRegions"]:
            _draw_box(draw_block, r, (34, 197, 94), 2)
        # 灰框：⭐ 2026-07-28 修正——半透明填充，不再全覆盖底层文字/图案。
        #   即便某区域应灰化跳过（CAD/图标），也须保留底层内容可见（用户明确要求）。
        #   做法：用 RGBA overlay 做 alpha 合成（alpha≈27%），占位假坐标(0,0,10,10)跳过。
        _GRAY_STROKE = (110, 110, 110)
        _GRAY_FILL_RGBA = (170, 170, 180, 70)  # 不透明度约 27%，底层清晰可见
        _gray_real = [r for r in result["grayRegions"]
                      if (r[0], r[1], r[2], r[3]) != (0, 0, 10, 10)]
        if _gray_real:
            _overlay = Image.new("RGBA", block_pil.size, (0, 0, 0, 0))
            _odraw = ImageDraw.Draw(_overlay)
            for r in _gray_real:
                x, y, w, h = r
                if w <= 0 or h <= 0:
                    continue
                _odraw.rectangle([x, y, x + w, y + h], fill=_GRAY_FILL_RGBA)
            block_pil = block_pil.convert("RGBA")
            block_pil = Image.alpha_composite(block_pil, _overlay).convert("RGB")
            draw_block = ImageDraw.Draw(block_pil)  # convert 后需重建 draw
        for r in _gray_real:
            _draw_box(draw_block, r, _GRAY_STROKE, 2)
        # ⭐ 图标缺失/多余标示（v18 改浅黄色，与文本红框区分）：
        #   图标（含二维码）不识别内容，只看当前位置有没有：
        #   - 块有照片无（缺图标）→ 浅黄框画在块图 DB 图标位置
        #   - 照片有块无（多图标）→ 浅黄框画在块图「照片图标归一化位置映射处」
        #   - 都有或都没有 → 不标示
        _ICON_BOX_COLOR = (240, 200, 40)   # 浅黄 — 与文本差异红框区分（v18）
        for r in result.get("iconMissingRegions", []) + result.get("iconExtraRegions", []):
            x, y, w, h = r
            if w <= 0 or h <= 0:
                continue
            _draw_box(draw_block, (x, y, w, h), _ICON_BOX_COLOR, 3)
        # 图块外边框
        W, H = block_pil.size
        draw_block.rectangle([0, 0, W-1, H-1], outline=(60, 60, 60), width=1)

        # 6. 输出块 OCR 原始 lines（供 C# 端下次缓存，避免重复 OCR 块图）
        try:
            if block_lines:
                result["blockLines"] = [{"text": bl["text"], "bbox": bl["bbox"], "conf": bl.get("conf", 1.0)} for bl in block_lines]
        except Exception:
            pass

        # 7. 输出差异标记图（画在图块上，照片不画框）
        # ⭐ 图片优化：JPEG quality=85 + 最大边 800px（体积缩小 60~80%，手机加载更快）
        block_out = output_path
        max_out = 800
        if max(W, H) > max_out:
            scale = max_out / max(W, H)
            nw, nh = int(W * scale), int(H * scale)
            block_pil = block_pil.resize((nw, nh), Image.LANCZOS)
        # 根据输出扩展名选择格式：.jpg/.jpeg → JPEG；其他 → PNG（向后兼容）
        ext = os.path.splitext(block_out)[1].lower()
        if ext in (".jpg", ".jpeg"):
            block_pil.save(block_out, "JPEG", quality=85)
        else:
            block_pil.save(block_out, "PNG", optimize=True)
        result["success"] = True
        # 清理扶正临时文件
        if _photo_work != photo_path:
            try: os.unlink(_photo_work)
            except Exception: pass
        return result

    except Exception as e:
        result["error"] = f"EXC {type(e).__name__}: {e}"
        traceback.print_exc(file=sys.stderr)
        # 异常时也清理临时文件
        if '_photo_work' in dir() and _photo_work != photo_path:
            try: os.unlink(_photo_work)
            except Exception: pass
        return result


def main():
    if len(sys.argv) < 4:
        print(json.dumps({"success": False, "error": "Usage: diff_visualizer.py <photo> <block> <output> [block_bbox_json] [block_text_override]"}))
        return 1
    photo_path = sys.argv[1]
    block_path = sys.argv[2]
    output_path = sys.argv[3]
    block_bbox_cache = None
    if len(sys.argv) >= 5 and sys.argv[4]:
        try:
            block_bbox_cache = json.loads(sys.argv[4])
        except (json.JSONDecodeError, TypeError):
            pass  # 无效的 JSON 则忽略，走 OCR
    # 5th 可选参数：块 RawText override（来自数据库，避免 OCR 与入库文本不一致）
    block_text_override = None
    if len(sys.argv) >= 6 and sys.argv[5]:
        block_text_override = sys.argv[5]
    # 6th 可选参数：照片 OCR 文本 override（来自 C#，避免 Python 端重复 OCR 照片）
    photo_text_override = None
    if len(sys.argv) >= 7 and sys.argv[6]:
        photo_text_override = sys.argv[6]
    # 7th 可选参数：保留占位（sys.argv[7]，与 C# 端 ComputeDiffFromPath 的 "" 对齐）
    # 8th 可选参数：图标区域 override（来自 DB 持久化，JSON list of [x,y,w,h]），避免重复图标检测
    # ⚠️ 注意：icons 在 argv[8]，不是 argv[7]（argv[7] 是保留占位，恒为 ""）。
    #    此前 off-by-one 误读 argv[7] → icons 永远为 None → 每次匹配都跑 _detect_icon_regions 加载 RapidOCR。
    icon_regions_override = None
    if len(sys.argv) >= 9 and sys.argv[8]:
        try:
            parsed = json.loads(sys.argv[8])
            if isinstance(parsed, list):
                icon_regions_override = parsed
        except (json.JSONDecodeError, TypeError):
            pass
    r = run(photo_path, block_path, output_path, block_bbox_cache, block_text_override, photo_text_override, icon_regions_override)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
