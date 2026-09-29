using System.Text.RegularExpressions;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// 打标识别规则表 —— 全部条目来自 21 份真实图纸的语料实测，不是拍脑袋的阈值。
///
/// 【与旧方案的本质区别】
/// 旧方案靠「像素块 OCR + 相似度阈值」猜哪些是打标内容，语义丢失、阈值不可解释。
/// v2 靠**图纸自己声明**：技术要求条款里写着「MAC地址、服务热线、禁止强电、地暖阀处均使用激光打标」，
/// 打标清单由条款给出，不需要猜。
///
/// 【语料依据（2026-09-11 采集，见 _archived_debug/2026-09-11_m1_probes/_m2_clauses.txt）】
/// 条款声明句式实测样本：
///   "...6、MAC地址、服务热线、禁止强电、地暖阀处均使用激光打标，灰色效果。"
///   "...6、按键丝印和服务热线处均使用激光打标，灰色效果。"
///   "...6、二维码和服务热线处均使用激光打标，灰色效果。"
///   "...6、禁止强电、地暖阀处均使用激光打标，灰色效果。"
///   "3.上盖顶部及底部信息、后盖接线标识均为激光镭刻工艺，二维码能够清晰扫描识别..."
///   "3.上盖顶部和下盖接线为激光镭刻工艺，颜色参考标准样件。"
/// 图上打标实例实测样本：
///   "LOW VOLTAGE禁止强电地暖阀DC15/24V" / "地暖阀LOW VOLTAGE禁止强电AB"
///   "MAC:A42985377FA0" / "PC-P1HEQ2 服务热线：4008601111" / "C-XXXX-XXXX"
/// 二维码声明实测样本：
///   "上盖固定板二维码格式：" / "下盖二维码格式：" / "上盖底部二维码标签激光打标格式："
///   "注：二维码内容不包含程序版本号和校验和。"
/// 修订记录实测样本（必须排除，否则 "更改MAC地址打标" 会被误当成打标对象）：
///   "2胡水强24.03.18更改MAC地址打标A/22位置尺寸" / "更正上盖镭雕内容胡水强25.09.0513"
/// </summary>
public static class MarkRules
{
    // ---------------- 打标工艺词 ----------------
    /// <summary>条款里表示「这些内容是要打出来的」的工艺词。</summary>
    public static readonly string[] ProcessWords =
    {
        "激光打标", "激光镭刻", "激光雕刻", "镭刻", "镭雕", "镭射", "丝印", "移印", "喷码", "打标"
    };

    /// <summary>
    /// 条款声明句的「终结标记」—— 它之前的部分就是被声明的打标项枚举。
    /// 顺序重要：长标记优先匹配。
    /// </summary>
    public static readonly string[] DeclarationEndMarkers =
    {
        "处均使用", "处采用", "处使用", "处均", "均使用", "均为", "均采", "使用激光", "采用激光", "为激光"
    };

    /// <summary>条款分句符。</summary>
    public static readonly char[] SentenceSplitters = { '。', ';', '；' };

    /// <summary>打标项枚举分隔符。</summary>
    public static readonly char[] ItemSplitters = { '、', ',', '，' };

    /// <summary>枚举中的连接词（"A和B"、"A及B"）。</summary>
    public static readonly string[] ItemConjunctions = { "和", "及", "与" };

    /// <summary>条款编号前缀（"6、" / "3." / "1、")，需剥离。</summary>
    private static readonly Regex ClauseNumberPrefix = new(@"^\s*[0-9]+\s*[、.,，]", RegexOptions.Compiled);

    /// <summary>
    /// 条款内「编号子项」边界：分隔符 + 1~2 位编号 + 顿号/点。
    ///
    /// 【为什么必须按编号切，而不是按句号切】
    /// 实测条款中编号之间用的是 ASCII "." 或 "，" 而非 "。"：
    ///   "...具体对照样品.2、最终成品不可有变形...现象,3、未标注的公差按GB/T 1804-M.4、各部件...装配5、装配前吹掉...灰尘.6、MAC地址、服务热线、禁止强电、地暖阀处均使用激光打标，灰色效果。"
    /// 若只按 。 分句，整条条款会是一个句子，导致「表面颜色」「处理效果参照附表」
    /// 这类与打标无关的内容也被当成声明项（首版实现就犯了这个错，实测产出 55 条噪声）。
    ///
    /// 边界字符集包含 ':' / '：' 是因为条款常写作 "技术要求:1、…"。
    /// </summary>
    private static readonly Regex ClauseItemBoundary =
        new(@"(?:^|[：:。，,;；.])\s*([0-9]{1,2})\s*[、.．]", RegexOptions.Compiled);

    /// <summary>
    /// 把条款切成编号子项（"6、MAC地址、…处均使用激光打标，灰色效果。"）。
    /// 条款标题前缀（"技术要求:"）被丢弃 —— 它不是声明项。
    /// </summary>
    public static List<string> SplitClauseItems(string clause)
    {
        var idx = new List<int>();
        foreach (Match m in ClauseItemBoundary.Matches(clause)) idx.Add(m.Index);
        if (idx.Count == 0) return new List<string> { clause };

        var items = new List<string>();
        for (var i = 0; i < idx.Count; i++)
        {
            var start = idx[i];
            while (start < clause.Length && !char.IsAsciiDigit(clause[start])) start++;
            var end = i + 1 < idx.Count ? idx[i + 1] : clause.Length;
            if (end > start) items.Add(clause[start..end]);
        }
        return items;
    }

    // ---------------- 已知打标内容词表（条款未声明时的兜底） ----------------
    /// <summary>
    /// 已知打标内容词表。来源：21 份图纸图上实例文本实测汇总。
    /// 仅当条款未声明时启用（RuleId=R2），置信度低于条款声明（R1）。
    /// key=规范项名，value=图上可能的写法（含英文/缩写/带值形式）。
    /// </summary>
    public static readonly Dictionary<string, string[]> KnownContent = new()
    {
        ["服务热线"] = new[] { "服务热线", "热线", "HOTLINE" },
        ["MAC地址"] = new[] { "MAC", "MAC地址" },
        // 实测：约克/日立/海信 10 寸屏图纸上印的是「禁止接入强电」，比条款写法多了「接入」二字
        ["禁止强电"] = new[] { "禁止强电", "LOWVOLTAGE", "LOW VOLTAGE", "禁止接入强电", "POWERLINEISFORBIDDEN" },
        ["地暖阀"] = new[] { "地暖阀" },
        ["DC15/24V"] = new[] { "DC15/24V", "DC15", "DC24" },
        ["二维码"] = new[] { "二维码" }
    };

    /// <summary>声明项 → 图上实例的别名映射（条款说中文、图上印英文的情形）。实测存在。</summary>
    public static readonly Dictionary<string, string[]> Aliases = new()
    {
        // 条款写"禁止强电"，图上实际印 "LOW VOLTAGE禁止强电"
        ["禁止强电"] = new[] { "LOWVOLTAGE", "LOW VOLTAGE" },
        // 条款写"MAC地址"，图上实际印 "MAC:A42985377FA0"
        ["MAC地址"] = new[] { "MAC" },
        // 条款写"地暖阀"，图上实际印 "地暖阀"
        ["地暖阀"] = new[] { "地暖阀" },
        ["服务热线"] = new[] { "服务热线", "热线" },
        ["二维码"] = new[] { "二维码" }
    };

    /// <summary>声明项去掉这些后缀后仍视为同一项（"MAC地址" → "MAC"）。</summary>
    public static readonly string[] ItemSuffixes = { "地址", "号码", "编号", "代码", "标识", "内容", "信息", "处" };

    // ---------------- 二维码 ----------------
    /// <summary>二维码相关词：命中即认为该图像对象是二维码（方案：只验存在性与位置，不解码）。</summary>
    public static readonly string[] QrWords = { "二维码", "QR", "QRCODE" };

    /// <summary>二维码格式声明句式（"上盖固定板二维码格式："）。</summary>
    private static readonly Regex QrFormatDecl = new(@"二维码[^。；;]{0,12}(格式|打印格式|打标格式)\s*[:：]?", RegexOptions.Compiled);

    // ---------------- 视图/部位 ----------------
    /// <summary>视图词 → 枚举。用于给 mark 标注属于哪个部位。</summary>
    public static readonly (string Word, MarkView View)[] ViewWords =
    {
        ("上盖", MarkView.TopCover), ("面板", MarkView.TopCover), ("顶部", MarkView.TopCover),
        ("前壳", MarkView.TopCover), ("顶盖", MarkView.TopCover),
        ("下盖", MarkView.BottomCover), ("底壳", MarkView.BottomCover), ("底部", MarkView.BottomCover),
        ("后盖", MarkView.Side), ("后壳", MarkView.Side), ("侧面", MarkView.Side),
        ("接线", MarkView.Cable), ("线材", MarkView.Cable),
        ("铭牌", MarkView.Nameplate)
    };

    // ---------------- 排除：修订记录 ----------------
    /// <summary>修订记录动词。修订表里常出现打标字样，必须排除。</summary>
    public static readonly string[] RevisionVerbs = { "更改", "更正", "更新", "修订", "改为", "增加", "取消", "删除", "新增" };

    /// <summary>修订记录日期（24.03.18 / 25.09.05）。</summary>
    private static readonly Regex RevisionDate = new(@"[0-9]{2,4}[./-][0-9]{1,2}[./-][0-9]{1,2}", RegexOptions.Compiled);

    /// <summary>修订版本号（A/1、A/22）。</summary>
    private static readonly Regex RevisionVersion = new(@"A\s*/\s*[0-9]+", RegexOptions.Compiled);

    // ---------------- 排除：标题栏 ----------------
    /// <summary>
    /// 标题栏标签。这些块本身不是打标对象；
    /// 注意：v2 的 MarkBuilder 是「正向证据驱动」的（只按证据新增、不做脆弱的区域排除），
    /// 因此本表主要用于诊断与人工复核展示，不是主要过滤手段。
    /// </summary>
    public static readonly string[] TitleBlockLabels =
    {
        "文件编号", "物料编码", "版本", "设计", "校对", "审核", "工艺", "标准化", "批准",
        "比例", "单位", "材料", "日期", "签名", "更改单号", "处数", "标记",
        "图纸编号", "模具编号", "模具表", "视角", "第三角", "发放部门", "文控中心",
        "市场部", "采购部", "货仓部", "品质工程部", "电子部", "技术部", "生产部",
        "物控部", "其它", "共", "第"
    };

    /// <summary>尺寸标注允许出现的符号（数字/公差/比例/单位）。</summary>
    private const string DimensionSymbols = "±°.,:;：xX×*/\\-–()（）ΦφRr";

    /// <summary>条款起始标记。</summary>
    public static readonly string[] ClauseMarkers = { "技术要求", "技 术 要 求", "技术要求:" };

    // ---------------- 判定方法 ----------------

    public static bool ContainsProcessWord(string text)
        => ProcessWords.Any(w => text.Contains(w, StringComparison.OrdinalIgnoreCase));

    /// <summary>
    /// 是否尺寸标注（"12±0.5" / "3.5±0.5" / "1:1" / "88±0.5"），不是打标内容。
    ///
    /// 【首版实现的坑】首版用「只含数字/符号/字母」的字符类判定，而该类把 A-Za-z 全放行，
    /// 导致 "MAC:A42985377FA0" 被误判为尺寸标注而被排除，直接造成 MAC 地址定位失败。
    /// 修正：尺寸标注的本质是**以数字为主** —— 字母只允许单位含义且不超过 3 个，且不得含汉字。
    /// </summary>
    public static bool IsDimension(string text)
    {
        var t = text.Trim();
        if (t.Length == 0 || t.Length > 24) return false;
        if (!t.Any(char.IsAsciiDigit)) return false;

        var letters = 0;
        foreach (var ch in t)
        {
            if (char.IsAsciiDigit(ch)) continue;
            if (char.IsAsciiLetter(ch)) { letters++; continue; }
            if (DimensionSymbols.Contains(ch)) continue;
            return false;   // 含汉字或其它字符 —— 不是尺寸
        }
        return letters <= 3;
    }

    /// <summary>是否修订记录块（排除用）。</summary>
    public static bool IsRevisionNote(string text)
    {
        if (RevisionVerbs.Any(v => text.Contains(v, StringComparison.Ordinal)))
        {
            if (RevisionDate.IsMatch(text) || RevisionVersion.IsMatch(text)) return true;
            // "增加上盖镭雕" 这类无日期的短修订项：动词 + 极短文本
            if (text.Length <= 14) return true;
        }
        return false;
    }

    public static bool IsQrFormatDeclaration(string text) => QrFormatDecl.IsMatch(text);

    /// <summary>
    /// 是否「部位/工艺短语」而非打标内容项。
    ///
    /// 【实测问题】条款 "3.上盖顶部和下盖接线为激光镭刻工艺" 切出的「上盖顶部」「下盖接线」
    /// 描述的是**打标发生在哪个部位**，不是**打什么内容**。首版实现把它们当内容项去找文本实例，
    /// 必然定位不到（图上不会印「上盖顶部」四个字），产出 5 条 R1-noloc 噪声。
    /// 正确语义：这是适用范围（scope），内容需由视觉层/人工确认。
    ///
    /// 判定：含视图词，或以工艺词结尾（"按键丝印" = 按键部位 + 丝印工艺）。
    /// </summary>
    public static bool IsLocationPhrase(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return false;
        if (FindViewWord(text) is not null) return true;
        return ProcessWords.Any(w => text.EndsWith(w, StringComparison.Ordinal));
    }

    /// <summary>
    /// R4 图面候选的噪声排除表 —— 这些文本在图上出现但不是打标内容。
    /// 全部来自 21 份图纸实测：公司名、图号、图名、日期、人名、勾选符。
    /// </summary>
    public static readonly string[] NoiseWords =
    {
        "有限公司", "科技有限公司", "打标图纸", "效果图", "模具", "BOM",
        "第三角", "第一角", "视角", "未注", "公差", "GB/T", "QA标准",
        // 以下为实测新增：这些是**字段名标签**（二维码内容的组成部分名称），不是打标内容本身
        "制造编码", "供应商代码", "产品型号", "生产日期", "流水号", "厂商代码",
        "二维码格式", "二维码内容", "字体", "放大图", "贴纸", "此处",
        // 工艺材料/工艺方式词（文档 §1：技术要求类默认不进打标对象）
        // 这些是图纸描述"怎么印"的加工说明，不会成为产品表面的打标内容。
        // 实测：p54「-/+标识丝印冷灰9C油墨」曾被误当打标对象；
        // 注意不能加「丝印」—— p62 合法对象组名「按键丝印」会被误删。
        "油墨", "喷码", "移印", "烫金", "丝网印", "冷灰", "热灰"
    };

    /// <summary>说明性/批注句式词 —— 这些是图纸对打标内容的描述，不是打标内容本身。</summary>
    public static readonly string[] ExplanatoryWords =
    {
        "示例", "虚线框不打印", "虚线框", "生产规则", "一机一地址", "设备ID", "为二维码",
        "说明", "注：", "注:", "此处", "扫码", "扫描", "查看", "请", "电话", "网址",
        "www.", "http", "格式", "内容", "贴纸", "放大图", "字体", "标签"
    };

    private static readonly Regex NoiseCode = new(@"^[A-Z]{1,3}-?[0-9]{2,}", RegexOptions.Compiled);     // JT-010-F-9
    private static readonly Regex NoiseDate = new(@"[0-9]{2}[./-][0-9]{1,2}[./-][0-9]{1,2}", RegexOptions.Compiled);
    private static readonly Regex NoiseTicks = new(@"^[√✓■□●○·]+$", RegexOptions.Compiled);

    /// <summary>
    /// 去掉所有空白字符。
    ///
    /// 【实测坑】标题栏为了对齐会插入空格："市 场 部"、"材  料"、"设  计"、"技 术 部"、"其    它"。
    /// 首版 IsNoiseBlock 直接拿原文与词表 Contains 比对，全部失配，
    /// 导致 02-4K3GR 一份图纸就产出 13 条标题栏噪声候选（占其 R4 产量的 87%）。
    /// 比对前必须先归一化空白。
    /// </summary>
    private static string NoSpace(string s)
    {
        var sb = new System.Text.StringBuilder(s.Length);
        foreach (var ch in s)
            if (!char.IsWhiteSpace(ch)) sb.Append(ch);
        return sb.ToString();
    }

    /// <summary>
    /// R4 候选噪声判定。注意：本方法是**排除**手段，只用于 R4 这条低置信兜底链；
    /// R1/R2/R3 是正向证据驱动，不受影响。
    /// </summary>
    public static bool IsNoiseBlock(string text)
    {
        var raw = (text ?? "").Trim();
        if (raw.Length == 0) return true;
        var t = NoSpace(raw);
        if (t.Length < 3) return true;                       // 'AB' / '1' / '√'
        if (NoiseTicks.IsMatch(t)) return true;
        if (NoiseDate.IsMatch(t)) return true;               // 纯日期，或「人名+日期」这类签名块
        if (NoiseCode.IsMatch(t)) return true;               // JT-010-F-9
        if (NoiseWords.Any(w => t.Contains(NoSpace(w), StringComparison.OrdinalIgnoreCase))) return true;
        if (TitleBlockLabels.Any(w => t.Contains(NoSpace(w), StringComparison.Ordinal))) return true;
        // 工艺词本身（"激光打标"）是图纸类别标签，不是打标内容；
        // 工艺词开头的（"激光打标（字体黑体）"）是工艺说明，同样不是内容
        if (ProcessWords.Any(w =>
            {
                var p = NoSpace(w);
                return t.Equals(p, StringComparison.OrdinalIgnoreCase) || t.StartsWith(p, StringComparison.Ordinal);
            })) return true;
        return false;
    }

    /// <summary>
    /// R4 的标题栏带过滤阈值。
    ///
    /// 【实测依据】21 份图纸中，标题栏/发放部门栏恒落在页面底部 y&gt;0.90 或最右侧 x&gt;0.88 的带状区；
    /// 真实打标内容全部落在图面中部（P1HEQ2 最低的内容块 y=0.74，10寸屏约克 y≤0.62）。
    /// 本阈值**只用于 R4 这条低置信候选链**，R1/R2/R3 是正向证据驱动，不受位置影响。
    /// 已知局限：若将来出现打标内容本身落在底部带的图纸，R4 会漏 —— 但那时条款声明（R1）会先命中。
    /// </summary>
    public const double TitleBandBottom = 0.90;
    public const double TitleBandRight = 0.88;

    /// <summary>
    /// 是否说明性长句/批注（不是产品表面打标内容）。
    /// 设计 §1：标题栏/技术要求/尺寸/批注默认不进比对；只有关键词/引线附近内容才候选。
    /// 说明性文字（如「为二维码，生产规则为设备ID，一机一地址，示例：…，虚线框不打印。」）
    /// 虽邻近 QR 图像，但是对 QR 的描述，不应成为比对对象。
    /// </summary>
    public static bool IsExplanatoryProse(string text)
    {
        var t = NoSpace(text ?? "").Trim();
        if (t.Length == 0) return true;
        if (ExplanatoryWords.Any(w => t.Contains(NoSpace(w), StringComparison.OrdinalIgnoreCase))) return true;
        if (t.Length > 8 && t.Any(ch => "。，；、".Contains(ch))) return true;
        return false;
    }

    // ---------------- A（#37）：图纸侧清单净化 ----------------
    /// <summary>屏显 / 数码管 UI 专用词（多字，低风险误杀真实打标内容）。来源：p62 实测污染
    /// （8888 / 点检模式冲突 / 试运行热启动 / 开送小时后 / 地址系统 / 定时 等屏显文案）。</summary>
    public static readonly string[] DisplayUiWords =
    {
        "点检", "试运行", "热启动", "风量", "风向", "模式冲突", "开送", "地址系统",
        "定时", "显示屏", "数码管", "面板显示", "运行模式", "待机", "故障码", "参数设置", "屏显"
    };

    /// <summary>短屏显词：单/双字，配合「整条极短」门限，避免误杀真实短打标内容。</summary>
    public static readonly string[] DisplayUiShort =
    {
        "模式", "定时", "开", "关", "屏", "显", "运行", "暂停", "锁定", "菜单", "设定"
    };

    /// <summary>
    /// A（#37）：判断一条图纸侧文本是否疑似「非打标内容」污染，返回排除原因；否则 null。
    /// 仅应用于 R4/R5 低置信兜底链（MarkBuilder 内已限定），R1/R2/R3 正向证据不受影响。
    /// 三类污染：①屏显/数码管 UI 文案 ②页脚/署名碎片 ③二维码格式说明文字。
    /// </summary>
    public static string? PollutionReason(string text)
    {
        var t = NoSpace((text ?? "").Trim());
        if (t.Length == 0) return null;
        // ③ 二维码格式说明 / 字段名标签（与 R4 噪声表一致）
        if (NoiseWords.Any(w => t.Contains(NoSpace(w), StringComparison.OrdinalIgnoreCase))) return "二维码格式说明/字段标签";
        if (IsQrFormatDeclaration(t)) return "二维码格式声明";
        if (IsExplanatoryProse(t)) return "说明性长句/批注";
        // ① 屏显 UI 专用词（多字，低风险）
        if (DisplayUiWords.Any(w => t.Contains(w, StringComparison.OrdinalIgnoreCase))) return "屏显UI文案";
        // ① 短屏显词：仅当整条极短（≤4 字）且无已知内容词命中，避免误杀真实短内容
        if (t.Length <= 4)
        {
            if (DisplayUiShort.Any(w => t.Equals(w, StringComparison.OrdinalIgnoreCase) || t.Contains(w)))
                return "屏显UI短词";
            // 纯数字屏显值（如 8888）：排除已知内容词（热线/编码）后的纯数字串
            if (t.All(char.IsAsciiDigit) && t.Length >= 3
                && !KnownContent.Values.Any(vals => vals.Any(v => t.Contains(v, StringComparison.OrdinalIgnoreCase))))
                return "屏显数字值";
        }
        return null;
    }

    

    /// <summary>
    /// R4 候选正向判定：只把"像打标内容"的文本块作为图面候选，过滤批注/说明长句。
    /// 与 IsNoiseBlock（排除）互补：IsNoiseBlock 排除标题栏/尺寸/修订等已知噪声，
    /// 本方法进一步要求剩余文本"像是实际打标内容"（已知内容词 / 代码式 / 短标签）。
    /// </summary>
    public static bool IsMarkingLike(string text)
    {
        var t = (text ?? "").Trim();
        if (t.Length == 0) return false;
        if (IsExplanatoryProse(t)) return false;
        foreach (var forms in KnownContent.Values)
            foreach (var f in forms)
                if (t.Contains(f, StringComparison.OrdinalIgnoreCase)) return true;
        int L = 0, D = 0;
        foreach (var c in t)
        {
            if (char.IsLetter(c)) L++;
            else if (char.IsDigit(c)) D++;
        }
        if (L >= 2 && D >= 2 && t.Length >= 4) return true;
        if (t.Length <= 12 && !t.Any(ch => "。，；、".Contains(ch))) return true;
        return false;
    }

    /// <summary>R4 专用：是否落在标题栏带内。</summary>
    public static bool InTitleBand(double nx, double ny)
        => ny > TitleBandBottom || nx > TitleBandRight;

    /// <summary>R4 专用：是否注释/条件说明块（"…内容以实际生产为准"）。</summary>
    public static bool IsConditionalNote(string text)
        => (text ?? "").Contains("以实际", StringComparison.Ordinal)
           || (text ?? "").Contains("按实际", StringComparison.Ordinal);

    public static bool ContainsQrWord(string text)
        => QrWords.Any(w => text.Contains(w, StringComparison.OrdinalIgnoreCase));

    /// <summary>剥离条款编号前缀（"6、" / "3."）。</summary>
    public static string StripClauseNumber(string s) => ClauseNumberPrefix.Replace(s, "").Trim();

    /// <summary>找出声明句的终结标记位置，未命中返回 -1。</summary>
    public static int FindDeclarationEnd(string sentence)
    {
        var best = -1;
        foreach (var m in DeclarationEndMarkers)
        {
            var i = sentence.IndexOf(m, StringComparison.Ordinal);
            if (i >= 0 && (best < 0 || i < best)) best = i;
        }
        return best;
    }

    /// <summary>把枚举串切成打标项（先按标点，再按连接词）。</summary>
    public static List<string> SplitItems(string s)
    {
        var items = new List<string>();
        foreach (var part in s.Split(ItemSplitters, StringSplitOptions.RemoveEmptyEntries))
        {
            var p = part.Trim();
            if (p.Length == 0) continue;
            // "按键丝印和服务热线" → 再按连接词切
            var cut = -1;
            foreach (var c in ItemConjunctions)
            {
                var i = p.IndexOf(c, StringComparison.Ordinal);
                if (i > 0 && i < p.Length - 1 && (cut < 0 || i < cut)) cut = i;
            }
            if (cut > 0)
            {
                var a = p[..cut].Trim();
                var b = p[(cut + 1)..].Trim();
                if (a.Length > 0) items.Add(a);
                if (b.Length > 0) items.Add(b);
            }
            else
            {
                items.Add(p);
            }
        }
        return items.Where(i => i.Length >= 2).Distinct().ToList();
    }

    /// <summary>生成某声明项在图上可能的匹配键（本体 + 去后缀 + 别名）。</summary>
    public static List<string> MatchKeysOf(string item)
    {
        var keys = new List<string> { item };
        foreach (var suf in ItemSuffixes)
        {
            if (item.Length > suf.Length && item.EndsWith(suf, StringComparison.Ordinal))
                keys.Add(item[..^suf.Length]);
        }
        if (Aliases.TryGetValue(item, out var al)) keys.AddRange(al);
        if (KnownContent.TryGetValue(item, out var kc)) keys.AddRange(kc);
        return keys.Where(k => k.Length >= 2).Distinct().ToList();
    }

    /// <summary>从文本中推断视图/部位词（取最先命中的）。</summary>
    public static string? FindViewWord(string text)
    {
        foreach (var (w, _) in ViewWords)
            if (text.Contains(w, StringComparison.Ordinal)) return w;
        return null;
    }

    public static MarkView ViewOf(string? word)
    {
        if (word is null) return MarkView.Unspecified;
        foreach (var (w, v) in ViewWords)
            if (string.Equals(w, word, StringComparison.Ordinal)) return v;
        return MarkView.Unspecified;
    }
}
