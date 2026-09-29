using System.Text.Json;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// 匹配判定层：图纸侧「应打标对象」 ↔ 照片侧「定点观测」的位置与存在性比对。
///
/// <para>【判定输入】observe-verify/1（定点区域观测）+ observe/1（全图盲观测，用于
///    二维码兜底匹配与 Extra 检出、质量门槛）。</para>
///
/// <para>【判定原则 —— 对应「不猜测合格与否」的用户约束】
/// 只做三类客观比较，不做任何语义猜测：
///   1. 文本：TextNormalizer 三级匹配（Exact / Normalized / Ambiguous），
///      Ambiguous 只能判 LowConfidence，绝不当 Matched（防 0/O、1/I 伪造一致）；
///   2. 二维码：定点解码 + 盲检兜底，**只验存在性与位置**（V2Models 注释即规范）；
///      解码内容按设计 §7 **不参与通过与否**，仅作提示性人工标注（P4 回正）；
///   3. 图标：只验存在性（前景墨迹占比）与位置。
/// 感知降级（code_degraded / 质量不可用 / 区域无坐标）→ NotComparable，绝不谎报 Missing。</para>
///
/// <para>【八态 → MarkState】Matched / Missing / Wrong / Extra / LowConfidence /
/// NotApplicable / NotComparable / ProcessingError（枚举定义见 V2Models.cs）。</para>
///
/// <para>【视图级配准（方案 B，M6 阶段）】真实单面产品照片不满足「照片矫正后≈图纸页面布局」
/// 的空间前提（geometry=no-quad）。Verify 入口接收 <c>selectedView</c> 参数：
/// 用户在 H5 选定拍摄的产品面后，照片即视图本身，C# 把 mark 的页面级 NormBbox 用
/// 视图并集 bbox 重算为视图级 NormBbox（<see cref="RebaseToView"/>），强制
/// <see cref="Result.GeometryTrusted"/> = true 跳过 A 方案兜底分支，位置容差改用
/// <see cref="ViewPosTol"/>（0.10）。Python verify 端无需改动 —— 它本就把传入的
/// normBbox 当图像相对坐标处理。selectedView 为 null/Unspecified 时与今天逐字相同。</para>
/// </summary>
public static class MarkVerifier
{
    /// <summary>位置容差（归一化坐标，原图尺寸比例）。手持照片 + 矫正误差的宽松经验值。</summary>
    public const double PosTol = 0.06;

    /// <summary>
    /// 视图级配准时的位置容差（方案 B）。
    /// 单面特写照片手持倾斜 + viewBbox 估计误差累积，比页面级放宽；
    /// 仅在 <see cref="Result.ViewNormMap"/> 非空（用户选定了拍摄视图）时生效。
    /// </summary>
    public const double ViewPosTol = 0.10;

    /// <summary>P0：锚点配准后的位置容差上限（防止不确定度叠加后位置判据失去意义）。</summary>
    public const double MaxPosTol = 0.35;

    /// <summary>
    /// R4（#35）QR 配对的唯一性判据：最近距离 / 次近距离 的上限。
    /// ≤ 该值（最近比次近近 40% 以上）才判「明确 = Matched」；两候选距离接近则判歧义 → 黄 + 人工按编号指认。
    /// **不得因召回压力下调**（与「不得用召回换时延」同原则）。
    /// </summary>
    public const double QrUniquenessRatio = 0.60;

    /// <summary>P0：不确定度随「离锚点质心距离」的增长系数（每 1.0 归一化距离增加的容差）。
    /// 单锚点平移只在锚点附近可信，越远越不可信 —— 防止自证循环把远处项误判为位置核对通过。</summary>
    public const double AnchorDistK = 0.25;

    /// <summary>图标存在性判定：裁剪区前景墨迹占比下限（语料白底图 ink_ratio 1.3%~2.9%）。</summary>
    public const double IconInkMin = 0.02;

    /// <summary>
    /// 视图标签框的最小可信面积（页面归一化面积）。
    ///
    /// CAD 图纸里“上盖二维码格式：”等说明文字也会被 InferView 当作视图锚点，
    /// 但它们通常只是页面上的一小行字。若直接把该文字框当成 viewBbox，
    /// RebaseToView 会把整张产品面的 mark 压缩到一个极小区域，现场照片裁剪随即偏离。
    /// 面积低于此阈值时，ViewBboxOf 回退到同视图实际 mark 的并集；阈值只针对锚点框，
    /// 不会过滤或改变 mark 本身。
    /// </summary>
    public const double MinViewLabelArea = 0.0025;

    // ---------------- 结果模型 ----------------

    public sealed class Verdict
    {
        public string MarkKey { get; set; } = "";
        public string Type { get; set; } = "";
        public string View { get; set; } = "";
        public string? Text { get; set; }
        public string State { get; set; } = "";
        /// <summary>观测中心与预期中心的归一化偏移（对角线比例），未观测到为 null。</summary>
        public double? Offset { get; set; }
        /// <summary>观测到的二维码内容（仅 Qr 且解出时）。</summary>
        public string? ObservedData { get; set; }
        public string Evidence { get; set; } = "";
        /// <summary>检测结果在照片上的归一化坐标 [x,y,w,h]（Stage 5 标示：照片标注框）。
        /// 只填**实际检出**位置；未检出时为 null（此时由 <see cref="ExpectedBbox"/> 表达"应在此处"）。</summary>
        public double[]? PhotoBbox { get; set; }
        /// <summary>mark 在图纸页面上的归一化坐标 [x,y,w,h]（Stage 5 标示：图纸回显框）。
        /// 【坐标系硬约定】恒为 mark 的原始页面坐标，绝不参与 RebaseToView / 仿射配准 ——
        /// 那两者会把坐标变换到照片空间，画到图纸截图上必然错位。</summary>
        public double[]? DrawingBbox { get; set; }
        /// <summary>照片上的**预期（定点）位置** [x,y,w,h]：即下发 Python 裁剪所用的框。
        /// 页面级配准时 == 图纸页面坐标；视图级配准时 == RebaseToView + 仿射配准后的照片坐标。
        /// 用于照片画布上画虚线框，告诉复核人"系统在这里找过"。</summary>
        public double[]? ExpectedBbox { get; set; }
        /// <summary>
        /// 内部标记：定点区为空、由全图盲检**精确**命中兜底（Ambiguous 不算）。
        /// 供坐标对齐（EstimateConsistentOffset）估计整体偏移用，不对外序列化。
        /// </summary>
        [System.Text.Json.Serialization.JsonIgnore]
        public bool BlindFallbackHit { get; set; }

        /// <summary>
        /// M3（#36）判定置信度 0~1 —— **不参与任何判定分支**。
        /// ⚠️ 2026-09-27：原本消费它的「严格 Gate 自动放行」已删除（真值集作废、无标定依据），
        /// 本字段现仅作诊断输出随 verdict 返回，H5 未消费。
        /// （Verify* 里没有任何一处读它，因此新增它不会改变任何既有 state）。
        /// 由 <see cref="AddVerdict"/> 在 verdict 入列前统一计算，保证所有早退分支都有值。
        /// <para>⚠️ 数值口径：基础分按内容级匹配等级（Exact/Normalized/Ambiguous/Qr/QrBlind/Icon）
        /// 给定，再按状态、位置偏移、几何可信度、感知降级逐级折减。
        /// 这些系数是**工程经验值，未经真实数据标定** —— Gate 默认关闭，
        /// 启用前必须用真实确认数据校准（见 docs 执行说明）。</para>
        /// </summary>
        public double Confidence { get; set; }

        /// <summary>内容级匹配等级（Exact/Normalized/Ambiguous/Qr/QrBlind/Icon），仅供 Confidence 计算，不序列化。</summary>
        [System.Text.Json.Serialization.JsonIgnore]
        public string? MatchLevel { get; set; }
    }

    public sealed class Result
    {
        public List<Verdict> Verdicts { get; set; } = new();
        public bool PhotoUsable { get; set; } = true;

        /// <summary>
        /// 照片有效部位区域（产品主体）归一化 [x, y, w, h]，来自 observe 的 subject。
        /// null = 未启用主体约束（文档 §5 产品轮廓 / §8 灰色「未拍到该部位」）。
        /// </summary>
        public double[]? Subject { get; set; }

        /// <summary>B（#37）：配准是否可信（affine/anchor 生效或指定视图）。仅此时部位覆盖判据才启用。</summary>
        public bool RegistrationTrusted { get; set; }

        /// <summary>B（#37）：部位覆盖判据总开关（来自 DrawingsV2:PartCoverageGate）。</summary>
        public bool PartCoverageGate { get; set; } = true;
        /// <summary>S1（2026-09-14）：照片整体是否可读（盲检至少检出一个文本或码）。
        /// 整体可读但单项未 OCR 到时判 NotDetected（黄）而非 Missing（红），避免批量假红警。</summary>
        public bool PhotoReadable { get; set; }
        public List<string> QualityReasons { get; set; } = new();
        public string CodeDetector { get; set; } = "";
        public bool CodeDegraded { get; set; }
        /// <summary>
        /// 几何可信度：照片与图纸页面之间是否存在坐标对齐关系。
        /// applied=true（已透视矫正）或 flat-quad-skip（四角可信、页面≈全图）→ 可信；
        /// no-quad（背景杂乱检不出文档四边形）→ 不可信，定点框位置不可信。
        /// 【真实照片实测（2026-09-12，P1HVQ）】不可信时仍按图纸页面坐标定点，
        /// 会把「框罩错了地方」当成缺标误报（服务热线/QR 判缺、Icon 罩住背景墨迹假绿）。
        /// </summary>
        public bool GeometryTrusted { get; set; } = true;

        /// <summary>
        /// 视图级配准（方案 B）按 mark.Id 索引的视图级 NormBbox。
        /// null 表示未启用视图级配准（向后兼容，走页面级坐标与 <see cref="PosTol"/>）；
        /// 非空表示用户已选定拍摄视图，照片即视图，<see cref="GeometryTrusted"/> 已被强制为 true，
        /// 位置比较使用 <see cref="ViewPosTol"/>。
        /// </summary>
        public Dictionary<string, double[]>? ViewNormMap { get; set; }

        /// <summary>P0（2026-09-15）：文本锚点配准变换（null = 未校正）。
        /// 用于位置容差的不确定度衰减与结果追溯。</summary>
        public AnchorTransform? Anchor { get; set; }

        /// <summary>
        /// 【P4 多 QR 消歧】已被某个 Qr mark 认领的盲检码索引（相对 Verify 内部 blindCodes 列表）。
        /// 一个盲检码只能服务一个 Qr mark：多个 QR mark 各自指向不同实体码，
        /// 避免「同一个码被多个 mark 重复引用」造成多 QR 场景判定混淆。
        /// </summary>
        public HashSet<int> ClaimedBlindCodeIdx { get; } = new();

        public double VerifyMs { get; set; }
        public Dictionary<string, int> Counts => Verdicts
            .GroupBy(v => v.State)
            .ToDictionary(g => g.Key, g => g.Count());
    }

    /// <summary>
    /// 文本锚点配准变换（P0，2026-09-15）。
    ///
    /// <para>【实证根因】平面图纸照片 geometry=no-quad → 仿射配准生产 0/60 生效 →
    /// 图纸坐标直映照片 → 取景差异原样变成错位（实测「服务热线」图纸 y=0 vs 照片 y=0.548，
    /// 偏差超半张图）。定点框因此大面积落空，判定只能靠全图盲检兜底。</para>
    ///
    /// <para>【为什么不用轮廓/孔位基准点】最初设计 Stage2 的「轮廓→边框/孔位/面板结构」经实测
    /// 不可行：图纸侧 v2_drawing_views 无几何字段；照片侧 3 张真实照片合格基准点候选 0/3
    /// （手持特写、部件被手指遮挡、背景杂乱）。改用已有的 OCR 文本作锚点载体。</para>
    ///
    /// <para>【保守性】0 锚点 → 不校正（行为与校正前逐字相同）；1 锚点 → 仅平移（不用尺寸比，
    /// mark 框与 OCR 框语义不同，尺寸比不可靠）；≥2 锚点 → 平移 + 均匀尺度（两两距离比
    /// 中位数抗离群，限幅 0.25~4.0）。</para>
    /// </summary>
    public sealed class AnchorTransform
    {
        /// <summary>【P3】锚点残差 RMS 门限（归一化页面比例）：≥3 锚点且超过 → 抑制变换并标需人工复核。</summary>
        public const double ResidualGate = 0.10;

        public double Dx { get; init; }
        public double Dy { get; init; }
        public double Scale { get; init; } = 1.0;
        public int AnchorCount { get; init; }
        /// <summary>锚点残差 RMS（归一化）：变换后预测位置与实际检出的偏差。</summary>
        public double Residual { get; init; }
        /// <summary>【P3】残差超门限被抑制（Applied=false，坐标未变换）：需人工复核坐标，不硬给错误映射。</summary>
        public bool Suppressed { get; init; }
        /// <summary>锚点质心（图纸坐标系），供不确定度随距离衰减使用。</summary>
        public double[] Centroid { get; init; } = { 0.5, 0.5 };
        public bool Applied => AnchorCount > 0 && !Suppressed;

        /// <summary>把图纸坐标系下的 [x,y,w,h] 变换到照片坐标系。</summary>
        public double[] Apply(double[] nb)
        {
            var w = Math.Clamp(nb[2] * Scale, 0.001, 1.0);
            var h = Math.Clamp(nb[3] * Scale, 0.001, 1.0);
            var cx = (nb[0] + nb[2] / 2.0) * Scale + Dx;
            var cy = (nb[1] + nb[3] / 2.0) * Scale + Dy;
            return new[]
            {
                Math.Clamp(cx - w / 2.0, 0.0, Math.Max(0.0, 1.0 - w)),
                Math.Clamp(cy - h / 2.0, 0.0, Math.Max(0.0, 1.0 - h)),
                w, h
            };
        }
    }

    // ---------------- 入口 ----------------

    /// <summary>
    /// 从盲检结果自动推断照片拍的是哪个视图（Stage 3 自动找图块）。
    /// <para>用盲检 OCR 文本 + QR 解码反向匹配图纸 marks 的 View 字段：
    /// 盲检到「服务热线」→ 找图纸 marks 里 text 含「服务热线」的 → 取其 View → 推断 view=Side。
    /// 唯一视图 → 返回该 MarkView；0 个或多冲突 → 返回 null（降级页面级）。</para>
    /// </summary>
    public static MarkView? InferViewFromBlind(IReadOnlyList<DrawingMark> marks, JsonElement? observeDoc)
    {
        if (observeDoc is not { ValueKind: JsonValueKind.Object } obs) return null;

        // 身份视图（型号码 / QR）：最高优先级锚点。实物型号代码（如 YCWA15NCWQ）几乎只印在
        // 铭牌等固定面，且其文本与图纸 marks 的 Text 精确对应 → 可可靠锚定视图，不受多视图
        // 文本冲突影响。R1（2026-09-14）：优先于普通文本匹配，消除「多视图冲突→返回 null」失效。
        var identityViews = new HashSet<MarkView>();
        // 普通文本视图：盲检文本恰好命中某视图 mark 的 Text（弱信号，可能跨多视图冲突）
        var genericViews = new HashSet<MarkView>();

        void Consider(DrawingMark m)
        {
            if (m.View == MarkView.Unspecified) return;
            if (string.IsNullOrEmpty(m.Text)) return;
            (m.Type == MarkType.Qr || ModelCodeLike(m.Text) ? identityViews : genericViews).Add(m.View);
        }

        // 1. 盲检 OCR 文本反向匹配 marks 的 Text 字段
        if (obs.TryGetProperty("texts", out var bts) && bts.ValueKind == JsonValueKind.Array)
        {
            foreach (var t in bts.EnumerateArray())
            {
                if (t.ValueKind != JsonValueKind.Object) continue;
                if (!t.TryGetProperty("text", out var tx) || tx.ValueKind != JsonValueKind.String) continue;
                var text = tx.GetString() ?? "";
                if (text.Length == 0) continue;
                foreach (var m in marks)
                {
                    if (m.View == MarkView.Unspecified) continue;
                    if (string.IsNullOrEmpty(m.Text)) continue;
                    // 模糊匹配：盲检文本包含 mark 的 Text，或 mark 的 Text 包含盲检文本
                    if (text.Contains(m.Text, StringComparison.OrdinalIgnoreCase) ||
                        (m.Text.Length > 2 && m.Text.Contains(text, StringComparison.OrdinalIgnoreCase)))
                    {
                        Consider(m);
                    }
                }
            }
        }

        // 2. 盲检 QR 解码反向匹配 marks 的 Qr 类型 → 身份锚点
        if (obs.TryGetProperty("codes", out var codes) && codes.ValueKind == JsonValueKind.Array)
        {
            foreach (var c in codes.EnumerateArray())
            {
                if (c.ValueKind != JsonValueKind.Object) continue;
                if (!c.TryGetProperty("data", out var d) || d.ValueKind != JsonValueKind.String) continue;
                var data = d.GetString() ?? "";
                if (data.Length == 0) continue;
                foreach (var m in marks)
                {
                    if (m.Type != MarkType.Qr) continue;
                    if (m.View == MarkView.Unspecified) continue;
                    // QR 命中：照片上有码 + 图纸声明了 QR mark → 取该 mark 的 View（身份锚点）
                    identityViews.Add(m.View);
                }
            }
        }

        // 3. 优先级解析：身份锚点唯一 → 直接返回（即使与普通文本冲突）；
        //    否则普通文本唯一 → 返回；否则 0 个或多冲突 → null（降级页面级）
        if (identityViews.Count == 1) return identityViews.First();
        if (genericViews.Count == 1) return genericViews.First();
        return null;
    }

    /// <summary>是否像产品型号代码：字母+数字混合、长度≥6（如 YCWA15NCWQ）。用于 R1 身份锚点识别。</summary>
    private static bool ModelCodeLike(string? s)
    {
        if (string.IsNullOrWhiteSpace(s) || s.Length < 6) return false;
        int letters = 0, digits = 0;
        foreach (var ch in s)
        {
            if (char.IsLetter(ch)) letters++;
            else if (char.IsDigit(ch)) digits++;
        }
        return letters >= 2 && digits >= 1 && (digits >= 2 || s.Contains('-'));
    }

    // ---------------- 仿射配准（Stage 4 真正几何配准） ----------------

    /// <summary>
    /// 基于对象的仿射配准：用盲检到的文本/QR 位置与图纸 marks 位置建立点对，
    /// 计算仿射变换矩阵（图纸视图级坐标 → 照片坐标），补偿旋转与缩放。
    /// <para>至少 3 个点对 → 求解；不足 → 返回 null（不做配准，保持原视图级坐标）。</para>
    /// </summary>
    private static double[]? ComputeAffine(IReadOnlyList<DrawingMark> filteredMarks,
                                           Dictionary<string, double[]>? viewNormMap,
                                           List<TextObs> blindTexts,
                                           List<BlindCode> blindCodes)
    {
        if (viewNormMap is null || viewNormMap.Count == 0) return null;

        // 建立点对：(图纸视图级中心, 照片中心)
        // 用 EnumerateNonGroup 展开 Group 子项（Group 本身无独立坐标，子项才有）
        var nonGroupMarks = EnumerateNonGroup(filteredMarks).ToList();
        var pairs = new List<(double x1, double y1, double x2, double y2)>();

        // 1. 文本点对：盲检文本 ↔ marks.Text
        foreach (var bt in blindTexts)
        {
            if (bt.Text.Length == 0 || bt.NormBbox is not { Length: >= 4 }) continue;
            foreach (var m in nonGroupMarks)
            {
                if (string.IsNullOrEmpty(m.Text) || m.Type == MarkType.Qr) continue;
                if (!viewNormMap.TryGetValue(m.Id, out var nb)) continue;
                // 模糊匹配
                if (!bt.Text.Contains(m.Text, StringComparison.OrdinalIgnoreCase) &&
                    !(m.Text.Length > 2 && m.Text.Contains(bt.Text, StringComparison.OrdinalIgnoreCase)))
                    continue;
                var dc = Center(nb);
                var pc = Center(bt.NormBbox);
                if (dc is null || pc is null) continue;
                pairs.Add((dc[0], dc[1], pc[0], pc[1]));
            }
        }

        // 2. QR 点对：盲检 QR ↔ marks.Type==Qr
        if (blindCodes.Count > 0)
        {
            var qrMarks = nonGroupMarks.Where(m => m.Type == MarkType.Qr).ToList();
            foreach (var bc in blindCodes)
            {
                if (bc.NormBbox is not { Length: >= 4 }) continue;
                foreach (var m in qrMarks)
                {
                    if (!viewNormMap.TryGetValue(m.Id, out var nb)) continue;
                    var dc = Center(nb);
                    var pc = Center(bc.NormBbox);
                    if (dc is null || pc is null) continue;
                    pairs.Add((dc[0], dc[1], pc[0], pc[1]));
                }
            }
        }

        if (pairs.Count < 3) return null;

        // 最小二乘求解仿射变换：x' = a*x + b*y + tx, y' = c*x + d*y + ty
        double a11 = 0, a12 = 0, a13 = 0, a22 = 0, a23 = 0, a33 = pairs.Count;
        double bx1 = 0, bx2 = 0, bx3 = 0, by1 = 0, by2 = 0, by3 = 0;
        foreach (var p in pairs)
        {
            a11 += p.x1 * p.x1;
            a12 += p.x1 * p.y1;
            a13 += p.x1;
            a22 += p.y1 * p.y1;
            a23 += p.y1;
            bx1 += p.x1 * p.x2;
            bx2 += p.y1 * p.x2;
            bx3 += p.x2;
            by1 += p.x1 * p.y2;
            by2 += p.y1 * p.y2;
            by3 += p.y2;
        }
        var AtA = new double[,] { { a11, a12, a13 }, { a12, a22, a23 }, { a13, a23, a33 } };
        var x = Solve3x3(AtA, new[] { bx1, bx2, bx3 });  // [a, b, tx]
        var y = Solve3x3(AtA, new[] { by1, by2, by3 });  // [c, d, ty]
        if (x is null || y is null) return null;

        return new[] { x[0], x[1], x[2], y[0], y[1], y[2] };  // [a, b, tx, c, d, ty]
    }

    /// <summary>求解 3x3 线性方程组 Ax=b（克莱姆法则）。</summary>
    private static double[]? Solve3x3(double[,] A, double[] b)
    {
        double det = A[0, 0] * (A[1, 1] * A[2, 2] - A[1, 2] * A[2, 1])
                   - A[0, 1] * (A[1, 0] * A[2, 2] - A[1, 2] * A[2, 0])
                   + A[0, 2] * (A[1, 0] * A[2, 1] - A[1, 1] * A[2, 0]);
        if (Math.Abs(det) < 1e-12) return null;
        double dx = b[0] * (A[1, 1] * A[2, 2] - A[1, 2] * A[2, 1])
                 - A[0, 1] * (b[1] * A[2, 2] - A[1, 2] * b[2])
                 + A[0, 2] * (b[1] * A[2, 1] - A[1, 1] * b[2]);
        double dy = A[0, 0] * (b[1] * A[2, 2] - A[1, 2] * b[2])
                 - b[0] * (A[1, 0] * A[2, 2] - A[1, 2] * A[2, 0])
                 + A[0, 2] * (A[1, 0] * b[2] - b[1] * A[2, 0]);
        double dz = A[0, 0] * (A[1, 1] * b[2] - b[1] * A[2, 1])
                 - A[0, 1] * (A[1, 0] * b[2] - b[1] * A[2, 0])
                 + b[0] * (A[1, 0] * A[2, 1] - A[1, 1] * A[2, 0]);
        return new[] { dx / det, dy / det, dz / det };
    }

    /// <summary>应用仿射变换到一个点：x' = a*x + b*y + tx, y' = c*x + d*y + ty。</summary>
    private static (double x, double y) ApplyAffine(double[] affine, double x, double y) =>
        (affine[0] * x + affine[1] * y + affine[2], affine[3] * x + affine[4] * y + affine[5]);

    /// <summary>
    /// 端到端仿射配准：从 observeDoc 提取盲检文本/QR，与 marks 建立点对，
    /// 计算仿射变换并应用到 viewNormMap，返回配准后的 ViewNormMap。
    /// <para>供 V2Service 在构造 regions 之前调用，保证 regions 裁剪位置与 MarkVerifier 判定位置一致。
    /// 至少 3 个点对才生效；不足或失败 → 返回原始 viewNormMap（不做配准）。</para>
    /// </summary>
    public static Dictionary<string, double[]> ApplyAffineRegistration(
        IReadOnlyList<DrawingMark> filteredMarks,
        Dictionary<string, double[]> viewNormMap,
        JsonElement? observeDoc,
        out bool applied)
    {
        applied = false;
        if (viewNormMap.Count == 0) return viewNormMap;

        // 从 observeDoc 提取盲检文本和 QR
        var blindTexts = new List<TextObs>();
        var blindCodes = new List<BlindCode>();
        if (observeDoc is { ValueKind: JsonValueKind.Object } ob)
        {
            if (ob.TryGetProperty("codes", out var codes) && codes.ValueKind == JsonValueKind.Array)
                foreach (var c in codes.EnumerateArray())
                {
                    var bc = BlindCode.Parse(c);
                    if (bc is not null) blindCodes.Add(bc);
                }
            if (ob.TryGetProperty("texts", out var bts) && bts.ValueKind == JsonValueKind.Array)
                foreach (var t in bts.EnumerateArray())
                {
                    if (t.ValueKind != JsonValueKind.Object) continue;
                    var to = new TextObs();
                    if (t.TryGetProperty("text", out var tx) && tx.ValueKind == JsonValueKind.String)
                        to.Text = tx.GetString() ?? "";
                    if (t.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array)
                        to.NormBbox = RegionObs.Doubles(nb);
                    if (to.Text.Length > 0) blindTexts.Add(to);
                }
        }

        var affine = ComputeAffine(filteredMarks, viewNormMap, blindTexts, blindCodes);
        if (affine is null) return viewNormMap;
        applied = true;

        var remapped = new Dictionary<string, double[]>();
        foreach (var kv in viewNormMap)
        {
            var nb = kv.Value;
            if (nb is not { Length: >= 4 }) continue;
            var (x0, y0) = ApplyAffine(affine, nb[0], nb[1]);
            var (x1, y1) = ApplyAffine(affine, nb[0] + nb[2], nb[1]);
            var (x2, y2) = ApplyAffine(affine, nb[0], nb[1] + nb[3]);
            var (x3, y3) = ApplyAffine(affine, nb[0] + nb[2], nb[1] + nb[3]);
            var nx = Math.Min(Math.Min(x0, x1), Math.Min(x2, x3));
            var ny = Math.Min(Math.Min(y0, y1), Math.Min(y2, y3));
            var nx1 = Math.Max(Math.Max(x0, x1), Math.Max(x2, x3));
            var ny1 = Math.Max(Math.Max(y0, y1), Math.Max(y2, y3));
            remapped[kv.Key] = new[] { nx, ny, nx1 - nx, ny1 - ny };
        }
        return remapped;
    }

    /// <summary>
    /// 【P0 文本锚点配准】用盲检 OCR 命中的 mark 作锚点，估计「图纸坐标系 → 照片坐标系」的
    /// 平移/尺度，供定点框搬正使用。详见 <see cref="AnchorTransform"/> 的实证说明。
    ///
    /// <para>锚点匹配复用判定层的同一套语义（<see cref="MarkRules.MatchKeysOf"/> +
    /// <see cref="TextNormalizer.Contains"/>），保证「锚点认得出的文本」与「判定认得出的文本」一致。</para>
    ///
    /// <para>返回 null 表示无法配准（无盲检文本或 0 命中），调用方应保持原坐标不动。</para>
    /// </summary>
    public static AnchorTransform? EstimateAnchorTransform(
        IEnumerable<DrawingMark> marks,
        Dictionary<string, double[]>? viewNormMap,
        JsonElement? observeDoc)
    {
        var blind = new List<TextObs>();
        if (observeDoc is { ValueKind: JsonValueKind.Object } ob &&
            ob.TryGetProperty("texts", out var bts) && bts.ValueKind == JsonValueKind.Array)
        {
            foreach (var t in bts.EnumerateArray())
            {
                if (t.ValueKind != JsonValueKind.Object) continue;
                var to = new TextObs();
                if (t.TryGetProperty("text", out var tx) && tx.ValueKind == JsonValueKind.String)
                    to.Text = tx.GetString() ?? "";
                if (t.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array)
                    to.NormBbox = RegionObs.Doubles(nb);
                if (to.Text.Length > 0) blind.Add(to);
            }
        }
        // 【P3 锚点拓宽】照片侧 QR 解码结果（codes[].norm_bbox）也可作锚点：QR 框语义一致
        // （图纸应打标区域 ↔ 实物同款框），比 OCR 文本框更稳；texts 与 codes 都为空才放弃。
        var blindQrs = new List<BlindCode>();
        if (observeDoc is { ValueKind: JsonValueKind.Object } ob2 &&
            ob2.TryGetProperty("codes", out var cds) && cds.ValueKind == JsonValueKind.Array)
        {
            foreach (var c in cds.EnumerateArray())
            {
                var bc = BlindCode.Parse(c);
                if (bc is not null) blindQrs.Add(bc);
            }
        }
        if (blind.Count == 0 && blindQrs.Count == 0) return null;

        var pairs = new List<(double[] draw, double[] photo)>();
        foreach (var m in marks)
        {
            if (string.IsNullOrWhiteSpace(m.Text)) continue;
            var draw = viewNormMap is not null && viewNormMap.TryGetValue(m.Id, out var vb)
                       ? vb : m.NormBbox;
            if (draw is not { Length: >= 4 }) continue;
            var dc = Center(draw);
            if (dc is null) continue;

            var keys = MarkRules.MatchKeysOf(m.Text!).ToList();
            if (keys.Count == 0) keys.Add(m.Text!);

            foreach (var t in blind)
            {
                if (t.NormBbox is not { Length: >= 4 }) continue;
                if (!keys.Any(k => TextNormalizer.Contains(t.Text, k) != TextMatch.None)) continue;
                var pc = Center(t.NormBbox);
                if (pc is null) continue;
                pairs.Add((dc, pc));
                break;   // 一个 mark 只取一个最靠前的命中，避免同文本重复加权
            }
        }

        // 【P3 QR 锚点配对】图纸侧 Qr mark 中心 ↔ 照片侧 codes 中心：贪心最近配对
        // （全部距离对升序依次取用未配过的，防多对一），与文本锚点合并进 pairs 统一估参。
        var qrDraws = new List<double[]>();
        foreach (var m in marks)
        {
            if (m.Type != MarkType.Qr) continue;
            var qb = viewNormMap is not null && viewNormMap.TryGetValue(m.Id, out var qv)
                     ? qv : m.NormBbox;
            if (qb is not { Length: >= 4 }) continue;
            var qc = Center(qb);
            if (qc is not null) qrDraws.Add(qc);
        }
        var qrPhotos = new List<double[]>();
        foreach (var bc2 in blindQrs)
        {
            if (bc2.NormBbox is not { Length: >= 4 }) continue;
            var pc2 = Center(bc2.NormBbox);
            if (pc2 is not null) qrPhotos.Add(pc2);
        }
        if (qrDraws.Count > 0 && qrPhotos.Count > 0)
        {
            // R3（#35）：全局最优一对一 —— 取代原「距离升序贪心」。
            // 贪心在多码场景会产出非最优、甚至错误的配对。规模保护：任一侧超过 8 个时退回贪心
            // （当前业务实测单份图纸最多 2 个 Qr mark，该分支不会触发）。
            var bestPairs = (qrDraws.Count <= 8 && qrPhotos.Count <= 8)
                ? BestOneToOne(qrDraws, qrPhotos)
                : GreedyPairs(qrDraws, qrPhotos);
            foreach (var (di, pi) in bestPairs)
                pairs.Add((qrDraws[di], qrPhotos[pi]));
        }
        if (pairs.Count == 0) return null;

        double dx, dy, scale;
        if (pairs.Count == 1)
        {
            // 单锚点：只做平移。尺寸比不可用 —— mark 框是「应打标区域」，
            // OCR 框是「实际检出的整行文字」，二者语义不同（实测宽度比可达 3 倍）。
            scale = 1.0;
            dx = pairs[0].photo[0] - pairs[0].draw[0];
            dy = pairs[0].photo[1] - pairs[0].draw[1];
        }
        else
        {
            // ≥2 锚点：两两距离比的中位数估均匀尺度（抗离群），再做质心对齐
            var ratios = new List<double>();
            for (int i = 0; i < pairs.Count; i++)
                for (int j = i + 1; j < pairs.Count; j++)
                {
                    var dd = Dist(pairs[i].draw, pairs[j].draw);
                    var dp = Dist(pairs[i].photo, pairs[j].photo);
                    if (dd > 1e-4 && dp > 1e-4) ratios.Add(dp / dd);
                }
            ratios.Sort();
            scale = ratios.Count > 0 ? ratios[ratios.Count / 2] : 1.0;
            if (scale < 0.25) scale = 0.25;
            if (scale > 4.0) scale = 4.0;

            double sx = 0, sy = 0, px = 0, py = 0;
            foreach (var (d, p) in pairs) { sx += d[0]; sy += d[1]; px += p[0]; py += p[1]; }
            var n = pairs.Count;
            dx = px / n - (sx / n) * scale;
            dy = py / n - (sy / n) * scale;
        }

        double rss = 0, cx = 0, cy = 0;
        foreach (var (d, p) in pairs)
        {
            var ex = d[0] * scale + dx;
            var ey = d[1] * scale + dy;
            rss += (ex - p[0]) * (ex - p[0]) + (ey - p[1]) * (ey - p[1]);
            cx += d[0]; cy += d[1];
        }
        var residual = Math.Sqrt(rss / pairs.Count);

        // 【P3 残差 gating】≥3 锚点时残差才有诊断意义（1~2 点总能拟合）。
        // 残差超门限 → 配准明显不可信（锚点错配/极端取景），抑制变换并标需人工复核，
        // 不硬给错误映射。门限为归一化页面 10%（AnchorTransform.ResidualGate），可依基线调优。
        var suppressed = pairs.Count >= 3 && residual > AnchorTransform.ResidualGate;

        return new AnchorTransform
        {
            Dx = dx, Dy = dy, Scale = scale,
            AnchorCount = pairs.Count,
            Residual = residual,
            Suppressed = suppressed,
            Centroid = new[] { cx / pairs.Count, cy / pairs.Count }
        };
    }

    public static Result Verify(IReadOnlyList<DrawingMark> marks,
                                JsonElement verifyDoc,
                                JsonElement? observeDoc,
                                MarkView? selectedView = null,
                                Dictionary<string, double[]>? precomputedViewNormMap = null,
                                AnchorTransform? precomputedAnchor = null,
                                double[]? subject = null,
                                bool? registrationTrusted = null,
                                bool partCoverageGate = true)
    {
        var result = new Result
        {
            Anchor = precomputedAnchor,
            Subject = subject,
            RegistrationTrusted = registrationTrusted == true,
            PartCoverageGate = partCoverageGate
        };

        // 质量门槛：照片不可用 → 全部 NotComparable（这是 not_comparable 存在的意义）
        if (observeDoc is { ValueKind: JsonValueKind.Object } obs)
        {
            if (obs.TryGetProperty("quality", out var q))
            {
                if (q.TryGetProperty("usable", out var u))
                    result.PhotoUsable = u.ValueKind == JsonValueKind.True;
                if (q.TryGetProperty("reasons", out var rs) && rs.ValueKind == JsonValueKind.Array)
                    foreach (var r in rs.EnumerateArray())
                        if (r.ValueKind == JsonValueKind.String) result.QualityReasons.Add(r.GetString()!);
            }
            if (obs.TryGetProperty("diagnostics", out var dg))
            {
                if (dg.TryGetProperty("code_detector", out var cd) && cd.ValueKind == JsonValueKind.String)
                    result.CodeDetector = cd.GetString() ?? "";
                if (dg.TryGetProperty("code_degraded", out var cg))
                    result.CodeDegraded = cg.ValueKind == JsonValueKind.True;
            }
            // 几何可信度：applied（已矫正）或 corners 非空（flat-quad-skip，页面≈全图）→ 可信；
            // no-quad → corners 为 null → 不可信（见 GeometryTrusted 注释）
            if (obs.TryGetProperty("geometry", out var gm))
            {
                bool applied = gm.TryGetProperty("applied", out var ap) && ap.ValueKind == JsonValueKind.True;
                bool hasCorners = gm.TryGetProperty("corners", out var cr)
                                  && cr.ValueKind == JsonValueKind.Array && cr.GetArrayLength() > 0;
                result.GeometryTrusted = applied || hasCorners;
            }
        }

        // 视图级配准（方案 B）：用户选定拍摄的产品面后，照片即视图本身，
        // mark 的页面级 NormBbox 重算为视图级 NormBbox，照片坐标系即视图坐标系。
        // selectedView 为 null 或 Unspecified → 行为与今天逐字相同（A 方案兜底继续生效）。
        var filteredMarks = marks;
        if (selectedView is { } sv && sv != MarkView.Unspecified)
        {
            filteredMarks = FilterByView(marks, sv);
            // 【Stage 4 真正几何配准】如果 V2Service 已计算并配准好 ViewNormMap，直接复用
            // （regions 构造也用同一份，保证裁剪位置与判定位置一致）。
            if (precomputedViewNormMap is not null)
            {
                result.ViewNormMap = precomputedViewNormMap;
            }
            else
            {
                // M6 修正·视图词范围：用 ViewBboxOf 替代 marks 的 UnionBbox，
                // 以视图标签文字为锚点排除散乱 marks，使 viewBbox 更贴合照片实际产品面
                var viewBbox = ViewBboxOf(filteredMarks);
                if (viewBbox is null || viewBbox[2] <= 0 || viewBbox[3] <= 0)
                {
                    // 该图纸无选定视图声明 → 不产生任何 mark verdict（不误报缺标），仅给一条视图级提示
                    result.Verdicts.Add(new Verdict
                    {
                        MarkKey = "_view",
                        Type = "View",
                        View = sv.ToString(),
                        State = nameof(MarkState.NotApplicable),
                        Confidence = 1.0,   // M3：非判定项，不参与 Gate
                        Evidence = $"该图纸无 {sv} 视图声明，跳过该视图下所有 mark（不判缺标）"
                    });
                    return result;
                }
                var map = new Dictionary<string, double[]>();
                foreach (var m in EnumerateNonGroup(filteredMarks))
                    if (m.NormBbox is { Length: >= 4 })
                        map[m.Id] = RebaseToView(m.NormBbox, viewBbox);
                result.ViewNormMap = map;
            }
            // 关键：照片即视图，no-quad 不再代表位置不可信 —— 强制可信，跳过 A 方案兜底分支
            result.GeometryTrusted = true;
        }
        if (verifyDoc.TryGetProperty("diagnostics", out var vd) &&
            vd.TryGetProperty("verify_ms", out var vm) && vm.TryGetDouble(out var ms))
            result.VerifyMs = ms;

        // 定点区域索引：mark.Id → 观测
        var regions = new Dictionary<string, RegionObs>();
        if (verifyDoc.TryGetProperty("regions", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var r in arr.EnumerateArray())
            {
                var o = RegionObs.Parse(r);
                if (o is not null) regions[o.Id] = o;
            }
        }

        // 盲检码列表（Qr 兜底匹配 + Extra 检出）+ 盲检文本（几何未配准时的内容级兜底证据）
        var blindCodes = new List<BlindCode>();
        var blindTexts = new List<TextObs>();
        if (observeDoc is { ValueKind: JsonValueKind.Object } ob)
        {
            if (ob.TryGetProperty("codes", out var codes) && codes.ValueKind == JsonValueKind.Array)
            {
                foreach (var c in codes.EnumerateArray())
                {
                    var bc = BlindCode.Parse(c);
                    if (bc is not null) blindCodes.Add(bc);
                }
            }
            if (ob.TryGetProperty("texts", out var bts) && bts.ValueKind == JsonValueKind.Array)
            {
                foreach (var t in bts.EnumerateArray())
                {
                    if (t.ValueKind != JsonValueKind.Object) continue;
                    var to = new TextObs();
                    if (t.TryGetProperty("text", out var tx) && tx.ValueKind == JsonValueKind.String)
                        to.Text = tx.GetString() ?? "";
                    if (t.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array)
                        to.NormBbox = RegionObs.Doubles(nb);
                    if (to.Text.Length > 0) blindTexts.Add(to);
                }
            }
        }

        // S1：照片整体可读性 = 盲检至少检出一个文本或码（与判定顺序无关，避免按 marks 顺序误判）
        result.PhotoReadable = blindTexts.Count > 0 || blindCodes.Count > 0;

        // 【Stage 4 真正几何配准】仿射配准在 V2Service 构造 regions 之前完成，
        // result.ViewNormMap 已是配准后的照片坐标（regions 与判定位置一致）。

        foreach (var m in filteredMarks)
            VerifyMark(m, regions, blindCodes, blindTexts, result, parentKey: null, inheritedState: null);

        // Extra 检出：盲检码不落在任何 Qr 预期位置附近 → 照片上有、图纸没声明（黄）。
        // 几何未配准时「预期位置」本身不可信，位置判据失效 → 跳过 Extra，避免误报。
        if (result.PhotoUsable && !result.CodeDegraded && result.GeometryTrusted)
        {
            var qrCenters = new List<double[]>();
            CollectQrCenters(filteredMarks, qrCenters, result);
            foreach (var bc in blindCodes)
            {
                var cc = Center(bc.NormBbox);
                if (cc is null) continue;
                var claimed = qrCenters.Any(qc => Dist(qc, cc) <= PosTolOf(result) * 2);
                if (claimed) continue;
                result.Verdicts.Add(new Verdict
                {
                    MarkKey = $"extra#{result.Verdicts.Count:X4}",
                    Type = "Qr",
                    State = nameof(MarkState.Extra),
                    Confidence = 0.40,   // M3：图纸未声明，恒需人工定性，不给高置信
                    ObservedData = bc.Data,
                    // R1（#34）：补盲检检出坐标 —— 此前 Extra 恒无 photoBbox，
                    // 导致照片上多出的真实二维码「无法编号、无法标示定位」（实测 34/34 为 null）。
                    PhotoBbox = bc.NormBbox,
                    Evidence = $"照片在图纸未声明位置扫到二维码「{bc.Data ?? "(未解码)"}」"
                });
            }
        }

        return result;
    }

    // ---------------- 单 mark 判定 ----------------

    private static void VerifyMark(DrawingMark m,
                                   Dictionary<string, RegionObs> regions,
                                   List<BlindCode> blindCodes,
                                   List<TextObs> blindTexts,
                                   Result result, string? parentKey, string? inheritedState)
    {
        // A（#37）：被排除的疑似非打标内容（屏显文案/页脚碎片/二维码格式说明）→ 不适用（灰），不参与比对。
        if (m.Excluded)
        {
            result.Verdicts.Add(new Verdict
            {
                MarkKey = m.Id,
                Type = m.Type.ToString(),
                View = m.View.ToString(),
                Text = m.Text,
                State = nameof(MarkState.NotApplicable),
                Confidence = 1.0,
                DrawingBbox = m.NormBbox,
                ExpectedBbox = ExpectedBbox(m, result),
                Evidence = "已排除（疑似非打标内容：" + (m.ExcludeReason ?? "污染") + "），不参与比对"
            });
            return;
        }

        // Group：逐子项判定，组状态取子项聚合（不再把组合文本当文本匹配）
        if (m.Type == MarkType.Group)
        {
            var childStates = new List<string>();
            var childConfs = new List<double>();
            foreach (var kid in m.Children ?? new List<DrawingMark>())
            {
                VerifyMark(kid, regions, blindCodes, blindTexts, result, m.Id, null);
                var kv = result.Verdicts.LastOrDefault(v => v.MarkKey == kid.Id);
                if (kv is not null) { childStates.Add(kv.State); childConfs.Add(kv.Confidence); }
            }
            var worst = Aggregate(childStates);
            result.Verdicts.Add(new Verdict
            {
                MarkKey = m.Id,
                Type = m.Type.ToString(),
                View = m.View.ToString(),
                Text = m.Text,
                State = worst,
                // M3：组合项置信度取子项最差者 —— 组的结论强度不可能高于最弱子项。
                // 空组（无子项）时 Aggregate 返回 NotApplicable：那是「没有可判定内容」，
                // 不是「不确定」，置信度按非判定项给 1.0（与 NotApplicable 分支一致）。
                Confidence = childConfs.Count > 0 ? childConfs.Min() : 1.0,
                Evidence = $"组合项聚合（{string.Join(" / ", childStates)}）"
            });
            return;
        }

        var v = new Verdict
        {
            MarkKey = m.Id,
            Type = m.Type.ToString(),
            View = m.View.ToString(),
            Text = m.Text,
            // 【坐标系】图纸回显恒用 mark 的原始页面坐标（不参与视图重算 / 仿射配准）；
            // 照片预期位置用 ExpectedBbox（视图级配准时为配准后的照片坐标）。
            // 二者在创建时就填好，保证下面所有早退分支也带坐标，画布不缺项。
            DrawingBbox = m.NormBbox,
            ExpectedBbox = ExpectedBbox(m, result)
        };

        // 非必标 → 不适用
        if (!m.Required)
        {
            v.State = nameof(MarkState.NotApplicable);
            v.Evidence = "标记为非必标";
            AddVerdict(result, v, m);
            return;
        }

        // B（#37）：部位覆盖判据已后置到匹配之后（见下方 _applyPartCoverage 段），
        // 仅把「未观测到的缺标项」翻为不适用（灰），绝不翻已命中项（避免假阴性）。

        // 上层（组/照片）已判不可比 → 传染
        if (inheritedState == nameof(MarkState.NotComparable))
        {
            v.State = inheritedState;
            v.Evidence = "继承不可比状态";
            AddVerdict(result, v, m);
            return;
        }

        // 照片质量门槛（一次判全局）
        if (!result.PhotoUsable)
        {
            v.State = nameof(MarkState.NotComparable);
            v.Evidence = "照片质量不可用：" + string.Join("；", result.QualityReasons);
            AddVerdict(result, v, m);
            return;
        }

        // 【文档 §5】observe 已提取照片有效部位区域（subject），随记录留痕（result.Subject）。
        // 【实测回退 2026-09-17】原设计「定点区域与 subject 不相交 → 未拍到该部位（灰）」
        // 在 42 例基线上产生 50 处负向（含 Matched → NotComparable）：viewNormMap 是
        // 「图纸视图 → 照片」的直接映射，与照片实际内容分布（受取景/比例/遮挡影响）
        // 并不同源，二者求交不可靠。故 subject 暂不参与判定，仅作观测数据；
        // 启用前需先解决坐标对齐（例如按配准残差把 mark 位置投到照片内容坐标系）。
        // 【#37 B 项】在「配准可信（affine/anchor 生效 或 指定视图）」前提下 subject 重新参与判定，
        // 但仅翻「未观测到的缺标项」为不适用，不翻已命中项，故不重现 2026-09-17 的假阴性。

        var regionId = RegionIdOf(m.Id, parentKey);
        regions.TryGetValue(regionId, out var obs);
        if (obs is null)
            // 定点区域缺失时按 markKey 原样再找一次（Python 端 id 透传）
            regions.TryGetValue(m.Id, out obs);

        if (obs is null || obs.Error is not null)
        {
            v.State = nameof(MarkState.NotComparable);
            v.Evidence = obs?.Error is not null ? $"定点区域异常：{obs.Error}" : "定点区域无观测结果";
            AddVerdict(result, v, m);
            return;
        }

        switch (m.Type)
        {
            case MarkType.Text:
                VerifyText(m, obs, blindTexts, result, v);
                break;
            case MarkType.Qr:
                VerifyQr(m, obs, blindCodes, result, v);
                break;
            case MarkType.Icon:
                VerifyIcon(m, obs, result, v);
                break;
            default:
                v.State = nameof(MarkState.ProcessingError);
                v.Evidence = $"未知 mark 类型 {m.Type}";
                break;
        }

        // Stage 5 标示：照片侧只认**实际检出**位置。
        // ⚠️ 旧实现在 Icon 分支把 m.NormBbox（图纸页面坐标）当照片框回落 —— 照片画布上会画错位置。
        // 图标/二维码只在定点区里测存在性，其"检出位置"就是那个定点区本身（ExpectedBbox）。
        // ⚠️ 只在判定函数**没有**填过时才兜底：VerifyText 的盲检命中 / VerifyQr 的盲检码
        // 会先填入全图实际检出位置，此处若无条件覆盖会把它们清空（实测 2026-09-14）。
        if (v.PhotoBbox is null)
        {
            v.PhotoBbox = m.Type switch
            {
                MarkType.Qr => obs.QrNormBbox,
                _ => obs.Texts.FirstOrDefault(t => t.NormBbox is { Length: >= 4 })?.NormBbox
                     ?? (obs.InkRatio is not null ? v.ExpectedBbox : null)
            };
        }

        // B（#37）：部位覆盖判据（后置，避免假阴性）。
        // 仅当配准可信 且 该 mark 未被观测到（将判 Missing/NotDetected）时，
        // 若其（图纸映射的）期望位置在照片主体范围之外 → 该部位未拍到 → 不适用（灰）而非缺标（红）。
        // 已观测到的 mark（Matched/LowConfidence/Extra）视为照片拍到了该部位，绝不翻灰。
        if (result.PartCoverageGate && result.RegistrationTrusted
            && (v.State == nameof(MarkState.Missing) || v.State == nameof(MarkState.NotDetected))
            && result.Subject is { Length: >= 4 } sub
            && result.ViewNormMap.TryGetValue(m.Id, out var covB) && covB is { Length: >= 4 })
        {
            var cx = covB[0] + covB[2] / 2;
            var cy = covB[1] + covB[3] / 2;
            const double M = 0.06;
            bool inside = cx >= sub[0] - M && cx <= sub[0] + sub[2] + M
                       && cy >= sub[1] - M && cy <= sub[1] + sub[3] + M;
            if (!inside)
            {
                v.State = nameof(MarkState.NotApplicable);
                v.Confidence = 1.0;
                v.Evidence = "部位覆盖判据：mark 期望位置在照片主体范围之外（照片未拍到该部位），原「"
                             + v.State + "」改为不适用";
            }
        }

        AddVerdict(result, v, m);
    }

    // ---------------- M3（#36）置信度 ----------------

    /// <summary>内容级匹配等级 → 置信度基础分（工程经验值，未经真实数据标定）。</summary>
    private static double BaseConfOf(string? level) => level switch
    {
        "Exact"      => 0.95,   // 逐字命中
        "Normalized" => 0.85,   // 归一化后命中（空格/全半角/大小写差异）
        "Ambiguous"  => 0.45,   // 易混折叠后命中（0/O、1/I 类）—— 本就不判绿
        "Qr"         => 0.90,   // 定点区解出码 + 位置核过
        "QrBlind"    => 0.60,   // 盲检兜底命中，位置证据弱于定点
        "Icon"       => 0.70,   // 只验墨迹存在性，无内容证据
        _            => 0.60    // 无内容级证据（存在性/缺失类判定）
    };

    /// <summary>
    /// verdict 统一入列口：先算 <see cref="Verdict.Confidence"/> 再加入结果集。
    /// 所有早退分支都走这里，保证「没有任何 verdict 会漏掉置信度」。
    /// </summary>
    private static void AddVerdict(Result result, Verdict v, DrawingMark m)
    {
        double c = BaseConfOf(v.MatchLevel);

        // ① 状态天花板：无论证据多强，状态本身决定了结论可信上限
        c = v.State switch
        {
            nameof(MarkState.Matched)        => c,
            nameof(MarkState.LowConfidence)  => Math.Min(c, 0.55),
            nameof(MarkState.Extra)          => Math.Min(c, 0.40),
            nameof(MarkState.NotDetected)    => Math.Min(c, 0.30),
            nameof(MarkState.Missing)        => Math.Min(c, 0.70),  // 缺标结论同样依赖位置可信度
            nameof(MarkState.NotComparable)  => Math.Min(c, 0.10),
            nameof(MarkState.NotApplicable)  => 1.0,                // 非判定项
            _                                => 0.0                 // Wrong / ProcessingError
        };

        // ② 位置因子：偏移越接近容差上限越不可信（下限 0.5，避免把位置判据放大成主导项）
        if (v.Offset is { } off && v.State is nameof(MarkState.Matched) or nameof(MarkState.LowConfidence))
        {
            var tol = PosTolOf(result);
            var f = tol > 1e-9 ? 1.0 - Math.Min(1.0, off / (2.0 * tol)) : 0.5;
            c *= Math.Clamp(f, 0.5, 1.0);
        }

        // ③ 感知/几何降级折减
        if (!result.GeometryTrusted) c *= 0.70;                                  // 定点框位置本身不可信
        if (result.CodeDegraded && v.Type == nameof(MarkType.Qr)) c *= 0.85;     // 码检测器降级
        if (v.BlindFallbackHit) c *= 0.80;                                       // 靠全图兜底命中的

        // ④ 图纸侧对象自身的剖析置信度（视觉兜底来源的 mark 更低）
        if (m.Confidence > 0) c *= 0.85 + 0.15 * Math.Clamp(m.Confidence, 0.0, 1.0);

        v.Confidence = Math.Round(Math.Clamp(c, 0.0, 1.0), 4);
        result.Verdicts.Add(v);
    }

    private static void VerifyText(DrawingMark m, RegionObs obs, List<TextObs> blindTexts,
                                   Result result, Verdict v)
    {
        // 匹配键：声明项本体 + 去后缀 + 别名（M2 已按 21 份语料实测的规则表）
        var keys = MarkRules.MatchKeysOf(m.Text ?? "").ToList();
        if (keys.Count == 0 && !string.IsNullOrWhiteSpace(m.Text)) keys.Add(m.Text!);

        if (obs.Texts.Count == 0)
        {
            // 几何未配准（no-quad）：定点框罩在哪里不可信 —— 用全图盲检 OCR 做内容级兜底。
            // 【真实照片实测（2026-09-12，P1HVQ）】「服务热线」定点框罩住空白，但全图
            // OCR conf 0.91 看得见 → 判缺标是误报。内容命中只判黄（位置未核对），绝不判绿。
            if (!result.GeometryTrusted)
            {
                var fb = BlindContentMatch(blindTexts, keys);
                if (fb is not null)
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.BlindFallbackHit = fb.Value.Match != TextMatch.Ambiguous;
                    // 照片标注：定点区空但全图命中时，把框画在盲检实际检出的位置
                    // （否则 H5 照片画布一片空白，复核人无从下手）
                    v.PhotoBbox = fb.Value.Bbox;
                    v.Evidence = fb.Value.Match == TextMatch.Ambiguous
                        ? $"全图仅易混命中「{fb.Value.Text}」（几何未配准，位置未核对；不作绿，需人工复核）"
                        : $"定点区域为空，但全图命中「{fb.Value.Text}」（{fb.Value.Match}；几何未配准，位置未核对，需人工复核）";
                }
                else
                {
                    v.State = result.PhotoReadable ? nameof(MarkState.NotDetected) : nameof(MarkState.Missing);
                    v.Evidence = result.PhotoReadable
                        ? "定点区域未 OCR 到任何文本，全图亦未发现声明内容（照片整体可读，该项未检出，非确认缺标，需人工复核）"
                        : "定点区域未 OCR 到任何文本，全图亦未发现声明内容（照片整体无可读内容）";
                }
            }
            else
            {
                // 混合兜底（M6 实测）：视图级配准时 viewBbox 位置映射可能偏差导致定点落空，
                // 但全图 OCR 仍可能命中声明内容。回退盲检：命中只判黄（位置未核对），不判绿也不判红。
                var fb = BlindContentMatch(blindTexts, keys);
                if (fb is not null)
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.BlindFallbackHit = fb.Value.Match != TextMatch.Ambiguous;
                    v.PhotoBbox = fb.Value.Bbox;   // 同上：黄框画在全图命中处
                    v.Evidence = fb.Value.Match == TextMatch.Ambiguous
                        ? $"定点区域为空，全图仅易混命中「{fb.Value.Text}」（视图级配准位置可能有偏差，需人工复核）"
                        : $"定点区域为空，但全图命中「{fb.Value.Text}」（{fb.Value.Match}；视图级配准位置可能有偏差，需人工复核）";
                }
                else
                {
                    v.State = result.PhotoReadable ? nameof(MarkState.NotDetected) : nameof(MarkState.Missing);
                    v.Evidence = result.PhotoReadable
                        ? "定点区域未 OCR 到任何文本，全图亦未发现声明内容（照片整体可读，该项未检出，非确认缺标，需人工复核）"
                        : "定点区域未 OCR 到任何文本，全图亦未发现声明内容（照片整体无可读内容）";
                }
            }
            return;
        }

        TextMatch best = TextMatch.None;
        string? bestText = null;
        double[]? bestBox = null;
        foreach (var t in obs.Texts)
        {
            foreach (var k in keys)
            {
                var tm = TextNormalizer.Contains(t.Text, k);
                if (best == TextMatch.None || Rank(tm) > Rank(best))
                {
                    best = tm; bestText = t.Text; bestBox = t.NormBbox;
                }
                if (best == TextMatch.Exact) break;
            }
            if (best == TextMatch.Exact) break;
        }

        // M3：记录内容级匹配等级，供 Confidence 计算（不参与判定）
        v.MatchLevel = best.ToString();

        switch (best)
        {
            case TextMatch.Exact or TextMatch.Normalized:
            {
                var expected = ExpectedBbox(m, result);
                v.Offset = Offset(expected, bestBox);
                // P0：锚点配准后按「离锚点距离」放宽容差（远处不确定度更高），上限 MaxPosTol
                var tol = PosTolForMark(expected, result);
                if (PositionOk(expected, bestBox, v.Offset, tol))
                {
                    v.State = nameof(MarkState.Matched);
                    v.Evidence = $"文本命中「{bestText}」（{best}）";
                }
                else
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.Evidence = $"文本命中「{bestText}」但位置偏移 {v.Offset:F3} > {tol:F2}（黄，需人工复核）";
                }
                break;
            }
            case TextMatch.Ambiguous:
                v.State = nameof(MarkState.LowConfidence);
                v.Evidence = $"仅易混折叠后命中「{bestText}」（0/O、1/I 类），不作绿";
                break;
            default:
                // 【M5 实测（合成手机照）】真实成像下 OCR 会单字误识：
                //   「MAC:A42985377FA0」→「NAC:…」、「地暖阀」→「地暖烟」。
                // 直接判 Missing（红）等于**误报缺标**，比判黄严重得多。
                // 因此严格匹配失败后，先查「疑似误识」：只判黄，绝不判绿。
                var near = NearestKey(obs, keys);
                if (near is not null)
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.PhotoBbox = near.Value.Bbox;   // 疑似误识也标出实际文本位置，便于人工一眼看出差哪个字
                    v.Evidence = $"疑似 OCR 误识：区域文本含「{near.Value.Text}」，"
                                 + $"与声明「{near.Value.Key}」仅差 {near.Value.Dist} 字（不判一致，需人工复核）";
                }
                else if ((near = NearestKeyOf(blindTexts, keys)) is not null)
                {
                    // 混合兜底（M6）：视图级配准时定点区域可能因位置映射偏差罩到错误内容，
                    // 全图疑似误识仍可作黄级证据。几何已配准也回退盲检。
                    v.State = nameof(MarkState.LowConfidence);
                    v.PhotoBbox = near.Value.Bbox;
                    v.Evidence = result.GeometryTrusted
                        ? $"全图疑似 OCR 误识：含「{near.Value.Text}」，与声明「{near.Value.Key}」"
                          + $"仅差 {near.Value.Dist} 字（视图级配准位置可能有偏差，需人工复核）"
                        : $"全图疑似 OCR 误识：含「{near.Value.Text}」，与声明「{near.Value.Key}」"
                          + $"仅差 {near.Value.Dist} 字（几何未配准，位置未核对，需人工复核）";
                }
                else
                {
                    v.State = nameof(MarkState.Missing);
                    v.Evidence = result.GeometryTrusted
                        ? $"定点区域文本 {ObsJoin(obs)} 与声明「{m.Text}」无匹配"
                        : $"定点区域文本 {ObsJoin(obs)} 与声明「{m.Text}」无匹配，全图亦未发现（几何未配准）";
                }
                break;
        }
    }

    /// <summary>
    /// 在区域文本里找「与匹配键只差一两个字符」的候选 —— 用于识别 OCR 单字误识。
    /// <para>只返回**疑似**结果：上层必须判 LowConfidence（黄），不能判 Matched（绿）——
    /// 把 NAC 当成 MAC 与把 0 当成 O 是同一类伪造一致。</para>
    /// <para>容差随键长放宽（2~6 字允许差 1，更长允许差 2），
    /// 依据是「OCR 对短串的误识通常只有 1 个字符」这一通用现象，不针对任何具体图纸。</para>
    /// </summary>
    private static (string Text, string Key, int Dist, double[]? Bbox)? NearestKey(RegionObs obs, List<string> keys)
        => NearestKeyOf(obs.Texts, keys);

    private static (string Text, string Key, int Dist, double[]? Bbox)? NearestKeyOf(List<TextObs> texts, List<string> keys)
    {
        (string, string, int, double[]?)? best = null;
        foreach (var t in texts)
        {
            var hay = TextNormalizer.LooseKey(t.Text);
            if (hay.Length == 0) continue;

            foreach (var k in keys)
            {
                var needle = TextNormalizer.LooseKey(k);
                if (needle.Length < 2) continue;

                var maxDist = needle.Length <= 6 ? 1 : 2;
                var d = BestSubstringDistance(hay, needle, maxDist);
                if (d < 0) continue;                    // 超容差
                if (best is null || d < best.Value.Item3) best = (t.Text, k, d, t.NormBbox);
            }
        }
        return best;
    }

    /// <summary>
    /// 几何未配准时的全图内容兜底：在盲检（全图）文本里找匹配键。
    /// <para>只作 LowConfidence 证据 —— 没有配准就没有位置核对，绝不判绿。</para>
    /// </summary>
    private static (string Text, TextMatch Match, double[]? Bbox)? BlindContentMatch(List<TextObs> texts, List<string> keys)
    {
        (string, TextMatch, double[]?)? best = null;
        foreach (var t in texts)
        {
            foreach (var k in keys)
            {
                var tm = TextNormalizer.Contains(t.Text, k);
                if (best is null || Rank(tm) > Rank(best.Value.Item2))
                    best = (t.Text, tm, t.NormBbox);
                if (best.Value.Item2 == TextMatch.Exact) return best;
            }
        }
        return best is not null && best.Value.Item2 != TextMatch.None ? best : null;
    }

    /// <summary>haystack 中与 needle 最接近的等长（±1）子串的编辑距离；超容差返回 -1。</summary>
    private static int BestSubstringDistance(string hay, string needle, int maxDist)
    {
        int best = int.MaxValue;
        for (var len = Math.Max(1, needle.Length - 1); len <= needle.Length + 1 && len <= hay.Length; len++)
        {
            for (var i = 0; i + len <= hay.Length; i++)
            {
                var d = Levenshtein(hay.Substring(i, len), needle);
                if (d < best) best = d;
            }
        }
        return best <= maxDist ? best : -1;
    }

    private static int Levenshtein(string a, string b)
    {
        if (a.Length == 0) return b.Length;
        if (b.Length == 0) return a.Length;
        var prev = new int[b.Length + 1];
        var cur = new int[b.Length + 1];
        for (var j = 0; j <= b.Length; j++) prev[j] = j;
        for (var i = 1; i <= a.Length; i++)
        {
            cur[0] = i;
            for (var j = 1; j <= b.Length; j++)
            {
                var cost = a[i - 1] == b[j - 1] ? 0 : 1;
                cur[j] = Math.Min(Math.Min(cur[j - 1] + 1, prev[j] + 1), prev[j - 1] + cost);
            }
            (prev, cur) = (cur, prev);
        }
        return prev[b.Length];
    }

    private static void VerifyQr(DrawingMark m, RegionObs obs, List<BlindCode> blindCodes,
                                 Result result, Verdict v)
    {
        // 1) 定点解码
        if (obs.QrFound)
        {
            v.MatchLevel = "Qr";   // M3：定点区解出码 = 最强的内容级证据
            v.ObservedData = obs.QrData;
            v.Offset = Offset(ExpectedBbox(m, result), obs.QrNormBbox);
            var decoded = (obs.QrData ?? "").Trim();
            var expected = (m.Text ?? "").Trim();
            var posOk = v.Offset is null || v.Offset <= PosTolOf(result) * 2; // 码中心允许更大漂移（裁剪+透视）
            // 【P4 回正·设计 §7】只验存在性/外接矩形/中心位置：解码内容不参与结论，
            // 内容差异仅作提示性人工标注（ObservedData + Evidence），不再改判 LowConfidence。
            var contentHint = QrContentHint(decoded, expected);
            if (posOk)
            {
                v.State = nameof(MarkState.Matched);
                v.Evidence = $"定点解码成功「{decoded}」（scale={obs.QrScale}）{contentHint}";
            }
            else
            {
                v.State = nameof(MarkState.LowConfidence);
                v.Evidence = $"定点解码「{decoded}」但中心偏移 {v.Offset:F3}（位置存疑，需人工复核）{contentHint}";
            }
            return;
        }

        // 2) 盲检兜底：全图扫到的码落在预期位置附近
        // 【P4 多 QR 消歧】一个盲检码只能被一个 Qr mark 认领（按距预期位置最近优先）：
        // 多个 QR mark 各自指向不同实体码，避免同一码被重复引用造成判定混淆。
        var c = Center(ExpectedBbox(m, result));
        if (c is not null)
        {
            // R4（#35）唯一性判据：先在容差内收集全部候选（按几何竞争关系），
            // 取最近的「未被认领」者配对；若还存在距离接近的次近候选
            // （r = d1/d2 > QrUniquenessRatio），说明「指哪一个」并不明确 → 判黄交人工按编号指认。
            // 与「宁黄勿红」同源：宁可多问一次，不可把歧义当明确。
            var candQr = new List<(double D, int Idx)>();
            for (int i = 0; i < blindCodes.Count; i++)
            {
                var cc = Center(blindCodes[i].NormBbox);
                if (cc is null) continue;
                var d = Dist(c, cc);
                if (d <= PosTolOf(result) * 2) candQr.Add((d, i));
            }
            candQr.Sort((x, y) => x.D.CompareTo(y.D));

            int nearIdx = -1;
            double nearDist = 0;
            foreach (var (d, i) in candQr)
            {
                if (result.ClaimedBlindCodeIdx.Contains(i)) continue;   // 已被其它 Qr mark 认领
                nearIdx = i; nearDist = d; break;
            }
            double? secondDist = null;
            foreach (var (d, i) in candQr)
            {
                if (i == nearIdx) continue;
                if (result.ClaimedBlindCodeIdx.Contains(i)) continue;   // 已认领者不构成可选歧义
                secondDist = d; break;
            }

            if (nearIdx >= 0)
            {
                v.MatchLevel = "QrBlind";   // M3：盲检兜底命中，弱于定点解码
                var near = blindCodes[nearIdx];
                result.ClaimedBlindCodeIdx.Add(nearIdx);
                v.ObservedData = near.Data;
                var decoded = (near.Data ?? "").Trim();
                var expected = (m.Text ?? "").Trim();
                v.PhotoBbox = near.NormBbox;   // 盲检码的实际位置（照片标注用）
                // 【P4 回正·设计 §7】只验位置；内容差异仅作人工标注，不改判
                var contentHint = QrContentHint(decoded, expected);

                // r = 最近/次近：无次近候选（唯一）或明显更近 → 明确；否则歧义
                double ratio = secondDist is { } sd && sd > 1e-9 ? nearDist / sd : 0.0;
                if (secondDist is null || ratio <= QrUniquenessRatio)
                {
                    v.State = nameof(MarkState.Matched);
                    v.Evidence = $"盲检码「{decoded}」落于预期位置（距离 {nearDist:F3}）{contentHint}";
                }
                else
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.Evidence = $"盲检码「{decoded}」距预期 {nearDist:F3}，但存在距离接近的候选"
                               + $"（次近 {secondDist!.Value:F3}，比 {ratio:F2} > {QrUniquenessRatio}）"
                               + $"→ 需按编号人工指认{contentHint}";
                }
                return;
            }
            // 几何未配准：预期位置不可信，但全图只要解出了码，至少证明「照片上有码」——
            // 不判绿（位置未核对）也不判红（可能就是该标的码），只判黄交人工复核。
            // 【P4】取**未被认领**的第一个码，多个 QR mark 各自引用不同码。
            if (!result.GeometryTrusted)
            {
                for (int i = 0; i < blindCodes.Count; i++)
                {
                    if (result.ClaimedBlindCodeIdx.Contains(i)) continue;
                    var any = blindCodes[i];
                    result.ClaimedBlindCodeIdx.Add(i);
                    v.ObservedData = any.Data;
                    v.State = nameof(MarkState.LowConfidence);
                    v.PhotoBbox = any.NormBbox;
                    v.Evidence = $"盲检解出「{any.Data ?? "(未解码)"}」，但几何未配准、位置未核对（需人工复核）";
                    return;
                }
            }
        }

        // 3) 都没有 → 看感知层是否降级；降级必须 NotComparable，不谎报 Missing
        if (result.CodeDegraded)
        {
            v.State = nameof(MarkState.NotComparable);
            v.Evidence = $"码检测降级（detector={result.CodeDetector}），无法证实存在或缺失";
            return;
        }

        // 几何未配准时「定点框罩住的位置」不可信：没解出码≠码不存在
        // 【真实照片实测（2026-09-12，P1HVQ）】码清晰可见但 cv2-qr 全图+分块均未解出 →
        // 判红是误报。试点期宁黄勿红：转黄交人工复核。
        // 【M6 混合兜底】视图级配准（GeometryTrusted=true）同理：cv2-qr 检测器本身可能漏检，
        // viewBbox 位置映射偏差也可能导致定点框罩偏。宁黄勿红，统一转黄交人工复核。
        // 【M6 ink_ratio 辅助】定点+盲检都失败时，用墨迹占比辅助判断：
        // QR 码的黑白模块各占约一半，ink_ratio 通常在 0.3~0.6 范围。
        // 落在此范围 →「定点区域墨迹占比 X% 符合二维码特征，可能码存在但检测器受限」
        // 不落此范围 → 补充「墨迹占比 X% 不符合二维码特征，可能定点框罩偏或码缺失」
        v.State = nameof(MarkState.LowConfidence);
        var hint = obs.Texts.Count > 0 ? $"；区域文本 {ObsJoin(obs)}（供人工复核）" : "";
        var inkHint = "";
        if (obs.InkRatio is { } ir)
        {
            inkHint = ir is >= 0.25 and <= 0.65
                ? $"；定点区域墨迹占比 {ir:F3} 符合二维码特征（0.25~0.65），码可能存在但检测器受限"
                : $"；定点区域墨迹占比 {ir:F3} 不符合二维码特征（期望 0.25~0.65），可能定点框罩偏或码缺失";
        }
        v.Evidence = result.GeometryTrusted
            ? $"定点多尺度解码（{string.Join("/", obs.QrScalesTried)}）与盲检均未发现二维码{hint}{inkHint}"
              + "（视图级配准下检测可能受限，需人工复核）"
            : $"定点多尺度解码（{string.Join("/", obs.QrScalesTried)}）与盲检均未发现二维码{hint}{inkHint}"
              + "（几何未配准，定点框位置不可信，需人工复核）";
    }

    /// <summary>
    /// 【P4 设计 §7】二维码内容提示文案：录入了预期码时给出「一致 / 不一致」提示，
    /// 但**只作人工标注，绝不改变判定状态**——内容不参与通过与否（见类头判定原则 2）。
    /// </summary>
    private static string QrContentHint(string decoded, string expected)
    {
        if (expected.Length == 0) return "";
        if (decoded.Length == 0)
            return $"（已录入预期码「{expected}」，本次未解码出内容；按设计 §7 不影响结论）";
        return string.Equals(decoded, expected, StringComparison.OrdinalIgnoreCase)
            ? $"（内容与录入预期「{expected}」一致，仅供参考）"
            : $"（内容「{decoded}」与录入预期「{expected}」不一致；按设计 §7 仅作人工标注，不影响结论）";
    }

    private static void VerifyIcon(DrawingMark m, RegionObs obs, Result result, Verdict v)
    {
        v.MatchLevel = "Icon";   // M3：图标只验存在性，证据强度低于文本精确匹配

        // 几何未配准：定点框罩住的「墨迹」可能是背景/手/阴影
        // 【真实照片实测（2026-09-12，P1HVQ）】框落进背景 ink_ratio=0.669 → 假「一致」（漏报）。
        // 存在性判据依赖位置可信，位置不可信时只判黄，绝不判绿。
        if (!result.GeometryTrusted)
        {
            v.State = nameof(MarkState.LowConfidence);
            v.Evidence = (obs.InkRatio ?? 0) >= IconInkMin
                ? $"预期区域存在墨迹（ink_ratio={obs.InkRatio:F3}），但几何未配准、位置未核对（可能是背景，需人工复核）"
                : $"预期区域未见明显墨迹（ink_ratio={obs.InkRatio:F3}），但几何未配准、定点框位置不可信（需人工复核）";
            return;
        }

        // 图标只验存在性与位置（V2Models 注释即规范）
        if ((obs.InkRatio ?? 0) >= IconInkMin)
        {
            v.State = nameof(MarkState.Matched);
            v.Evidence = $"预期位置存在内容（ink_ratio={obs.InkRatio:F3} ≥ {IconInkMin}，仅存在性比对）";
        }
        else
        {
            v.State = nameof(MarkState.Missing);
            v.Evidence = $"预期位置空白（ink_ratio={obs.InkRatio:F3} < {IconInkMin}）";
        }
    }

    // ---------------- 工具 ----------------

    private static string RegionIdOf(string markId, string? parentKey)
    {
        // Python 端收到的 id 是构建 regions 时传入的原样字符串（mark.Id 全名）
        return markId;
    }

    private static void CollectQrCenters(IEnumerable<DrawingMark> marks, List<double[]> acc, Result? result = null)
    {
        foreach (var m in marks)
        {
            if (m.Type == MarkType.Qr && Center(ExpectedBbox(m, result)) is { } c) acc.Add(c);
            CollectQrCenters(m.Children ?? new List<DrawingMark>(), acc, result);
        }
    }

    // ---------------- 视图级配准（方案 B）辅助 ----------------

    /// <summary>
    /// 当前应使用的位置容差：启用视图级配准（<see cref="Result.ViewNormMap"/> 非空）时
    /// 用 <see cref="ViewPosTol"/>（0.10，单面照片手持倾斜 + viewBbox 估计误差累积）；
    /// 否则用 <see cref="PosTol"/>（0.06，向后兼容）。
    /// </summary>
    private static double PosTolOf(Result? result)
        => result?.ViewNormMap is not null ? ViewPosTol : PosTol;

    /// <summary>
    /// P0：单个 mark 的位置容差 = 基础容差 + 锚点残差 + 离锚点质心距离 × <see cref="AnchorDistK"/>。
    /// 未启用锚点配准时退化为 <see cref="PosTolOf"/>（向后兼容，逐字不变）。
    /// </summary>
    private static double PosTolForMark(double[]? expected, Result? result)
    {
        var tol = PosTolOf(result);
        if (result?.Anchor is { Applied: true } a && expected is { Length: >= 4 })
        {
            var c = Center(expected);
            if (c is not null)
                tol += a.Residual + AnchorDistK * Dist(c, a.Centroid);
        }
        return Math.Min(tol, MaxPosTol);
    }

    // ---------------- 坐标对齐（2026-09-17 #27）----------------
    // 【实测根因】真实照片（p63 系列）上，图纸→照片的定点框存在**系统性整体平移**：
    // 同一视图内多个互相独立的 mark，其「全图实际检出位置 − 图纸映射位置」的偏移高度一致
    // （实测 7 例 dy 极差仅 0.005~0.014，dx 亦逐字相近），偏移量约 (-0.6, +0.6) ——
    // 这不是随机误差而是整体错位，定点框因此罩到空白，只能靠全图兜底判黄。
    // 处理：用内容已确认（Exact 命中）的兜底项估计一致偏移，把定点框搬正后重裁一次；
    // 重裁命中才升级为绿，未命中一律保持原黄 —— 只可能变好，不会制造假绿。

    /// <summary>一致偏移估计结果（null = 不足以判定为整体平移）。</summary>
    public sealed class OffsetAlignment
    {
        public double Dx;
        public double Dy;
        public List<string> MarkIds = new();
        public int Support;
    }

    /// <summary>偏移一致性容差：到中位偏移的距离超过此值视为离群，不参与估计也不重裁。</summary>
    public const double OffsetConsistencyTol = 0.25;

    /// <summary>启用坐标对齐所需的最少一致项数（≥2 才能证明是整体平移而非单点巧合）。</summary>
    public const int MinAlignmentSupport = 2;

    /// <summary>
    /// 从「定点区为空但全图精确命中」的项估计一致的照片偏移。
    /// 取各独立 mark 偏移向量的中位数，保留与中位数一致（≤容差）的项；
    /// 一致项 <see cref="MinAlignmentSupport"/> 个时返回 null（不做任何校正）。
    /// </summary>
    public static OffsetAlignment? EstimateConsistentOffset(Result result)
    {
        var vecs = new List<(string id, double dx, double dy)>();
        foreach (var v in result.Verdicts)
        {
            if (!v.BlindFallbackHit || string.IsNullOrEmpty(v.MarkKey)) continue;
            var p = Center(v.PhotoBbox);
            var e = Center(v.ExpectedBbox);
            if (p is null || e is null) continue;
            vecs.Add((v.MarkKey, p[0] - e[0], p[1] - e[1]));
        }
        if (vecs.Count < MinAlignmentSupport) return null;

        var mdx = Median(vecs.Select(x => x.dx).ToList());
        var mdy = Median(vecs.Select(x => x.dy).ToList());
        var ids = new List<string>();
        foreach (var x in vecs)
        {
            var ax = x.dx - mdx;
            var ay = x.dy - mdy;
            if (Math.Sqrt(ax * ax + ay * ay) <= OffsetConsistencyTol) ids.Add(x.id);
        }
        if (ids.Count < MinAlignmentSupport) return null;
        return new OffsetAlignment { Dx = mdx, Dy = mdy, MarkIds = ids, Support = ids.Count };
    }

    private static double Median(List<double> xs)
    {
        if (xs.Count == 0) return 0.0;
        var s = xs.OrderBy(x => x).ToList();
        int n = s.Count;
        return n % 2 == 1 ? s[n / 2] : (s[n / 2 - 1] + s[n / 2]) / 2.0;
    }

    /// <summary>
    /// 把「按一致偏移搬正后重裁」的观测应用到判定：仅在重裁命中时升级为 Matched，
    /// 未命中或异常一律保持原判定 —— 绝不降级、绝不制造假绿。
    /// 返回升级的项数。
    /// </summary>
    public static int ApplyRealignedRegions(IReadOnlyList<DrawingMark> marks, Result result,
                                            JsonElement verifyDoc2, JsonElement? observeDoc,
                                            OffsetAlignment al)
    {
        var regions2 = new Dictionary<string, RegionObs>();
        if (verifyDoc2.TryGetProperty("regions", out var arr) && arr.ValueKind == JsonValueKind.Array)
            foreach (var r in arr.EnumerateArray())
            {
                var o = RegionObs.Parse(r);
                if (o is not null) regions2[o.Id] = o;
            }
        if (regions2.Count == 0) return 0;

        var blindCodes = new List<BlindCode>();
        var blindTexts = new List<TextObs>();
        ParseBlind(observeDoc, blindCodes, blindTexts);

        // 判定位置必须与重裁位置一致 → 同步平移 viewNormMap
        if (result.ViewNormMap is { } vm)
        {
            var shifted = new Dictionary<string, double[]>();
            foreach (var kv in vm)
            {
                var b = kv.Value;
                shifted[kv.Key] = b is { Length: >= 4 }
                    ? new[] { b[0] + al.Dx, b[1] + al.Dy, b[2], b[3] }
                    : b;
            }
            result.ViewNormMap = shifted;
        }

        int upgraded = 0;
        foreach (var id in al.MarkIds)
        {
            var m = FindMarkById(marks, id);
            if (m is null) continue;
            if (!regions2.TryGetValue(id, out var obs2) || obs2.Error is not null) continue;
            var old = result.Verdicts.FirstOrDefault(v => v.MarkKey == id);
            if (old is null) continue;

            var nv = new Verdict
            {
                MarkKey = m.Id,
                Type = m.Type.ToString(),
                View = m.View.ToString(),
                Text = m.Text,
                DrawingBbox = m.NormBbox,
                ExpectedBbox = ExpectedBbox(m, result)
            };
            switch (m.Type)
            {
                case MarkType.Text: VerifyText(m, obs2, blindTexts, result, nv); break;
                case MarkType.Qr: VerifyQr(m, obs2, blindCodes, result, nv); break;
                case MarkType.Icon: VerifyIcon(m, obs2, result, nv); break;
                default: continue;
            }
            if (nv.PhotoBbox is null)
                nv.PhotoBbox = obs2.Texts.FirstOrDefault(t => t.NormBbox is { Length: >= 4 })?.NormBbox;

            if (nv.State != nameof(MarkState.Matched)) continue;   // 未命中 → 保持原判定

            old.State = nv.State;
            old.Evidence = $"【坐标对齐·偏移校正 d=({al.Dx:F3},{al.Dy:F3})，{al.Support} 项一致】" + nv.Evidence;
            old.PhotoBbox = nv.PhotoBbox ?? old.PhotoBbox;
            old.ExpectedBbox = nv.ExpectedBbox ?? old.ExpectedBbox;
            old.Offset = nv.Offset;
            old.ObservedData = nv.ObservedData ?? old.ObservedData;
            upgraded++;
        }

        if (upgraded > 0) ReaggregateGroups(marks, result);
        return upgraded;
    }

    /// <summary>子项升级后重算 Group 的聚合状态，避免组仍停留在旧结论。</summary>
    private static void ReaggregateGroups(IReadOnlyList<DrawingMark> marks, Result result)
    {
        foreach (var m in marks)
        {
            if (m.Type != MarkType.Group) continue;
            var states = new List<string>();
            foreach (var kid in m.Children ?? new List<DrawingMark>())
            {
                var kv = result.Verdicts.FirstOrDefault(v => v.MarkKey == kid.Id);
                if (kv is not null) states.Add(kv.State);
            }
            if (states.Count == 0) continue;
            var gv = result.Verdicts.FirstOrDefault(v => v.MarkKey == m.Id);
            if (gv is null) continue;
            gv.State = Aggregate(states);
            gv.Evidence = $"组合项聚合（{string.Join(" / ", states)}）";
        }
    }

    private static DrawingMark? FindMarkById(IReadOnlyList<DrawingMark> marks, string id)
    {
        foreach (var m in marks)
        {
            if (m.Id == id) return m;
            if (m.Children is { Count: > 0 } kids)
            {
                var r = FindMarkById(kids, id);
                if (r is not null) return r;
            }
        }
        return null;
    }

    /// <summary>从 observe 文档解析盲检码/文本（供重判使用）。</summary>
    private static void ParseBlind(JsonElement? observeDoc, List<BlindCode> blindCodes,
                                   List<TextObs> blindTexts)
    {
        if (observeDoc is not { ValueKind: JsonValueKind.Object } ob) return;
        if (ob.TryGetProperty("codes", out var codes) && codes.ValueKind == JsonValueKind.Array)
            foreach (var c in codes.EnumerateArray())
            {
                var bc = BlindCode.Parse(c);
                if (bc is not null) blindCodes.Add(bc);
            }
        if (ob.TryGetProperty("texts", out var bts) && bts.ValueKind == JsonValueKind.Array)
            foreach (var t in bts.EnumerateArray())
            {
                if (t.ValueKind != JsonValueKind.Object) continue;
                var to = new TextObs();
                if (t.TryGetProperty("text", out var tx) && tx.ValueKind == JsonValueKind.String)
                    to.Text = tx.GetString() ?? "";
                if (t.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array)
                    to.NormBbox = RegionObs.Doubles(nb);
                if (to.Text.Length > 0) blindTexts.Add(to);
            }
    }

    /// <summary>
    /// 取 mark 的预期 NormBbox：启用视图级配准时返回视图级坐标（按 mark.Id 查 ViewNormMap），
    /// 否则返回 mark 原始的页面级 NormBbox（向后兼容）。
    /// </summary>
    private static double[]? ExpectedBbox(DrawingMark m, Result? result)
        => result?.ViewNormMap is { } map && map.TryGetValue(m.Id, out var nb) ? nb : m.NormBbox;

    /// <summary>
    /// 把页面级 NormBbox 重算为视图级 NormBbox。
    /// <para>视图级坐标 = 视图坐标系下的归一化坐标：把 viewBbox 视为 [0,1]×[0,1] 的视图空间，
    /// mark 在该空间内的相对位置即 (page - viewOrigin) / viewSize。</para>
    /// <para>单 mark 独占视图时退化为 [0,0,1,1] —— 位置比较失去判据，但内容比较仍生效
    /// （OK，单 mark 视图本就只能做内容命中）。</para>
    /// </summary>
    public static double[] RebaseToView(double[] pageNorm, double[] viewBbox)
    {
        var w = viewBbox[2] > 0 ? viewBbox[2] : 1.0;
        var h = viewBbox[3] > 0 ? viewBbox[3] : 1.0;
        return new[]
        {
            (pageNorm[0] - viewBbox[0]) / w,
            (pageNorm[1] - viewBbox[1]) / h,
            pageNorm[2] / w,
            pageNorm[3] / h
        };
    }

    /// <summary>
    /// 求一组 NormBbox 的并集（外接矩形）。空集或全无效返回 null。
    /// </summary>
    public static double[]? UnionBbox(IEnumerable<double[]?> bboxes)
    {
        double? minX = null, minY = null, maxX = null, maxY = null;
        foreach (var nb in bboxes)
        {
            if (nb is not { Length: >= 4 }) continue;
            var x1 = nb[0];
            var y1 = nb[1];
            var x2 = nb[0] + nb[2];
            var y2 = nb[1] + nb[3];
            if (minX is null || x1 < minX) minX = x1;
            if (minY is null || y1 < minY) minY = y1;
            if (maxX is null || x2 > maxX) maxX = x2;
            if (maxY is null || y2 > maxY) maxY = y2;
        }
        if (minX is null || minY is null || maxX is null || maxY is null) return null;
        return new[] { minX.Value, minY.Value, maxX.Value - minX.Value, maxY.Value - minY.Value };
    }

    /// <summary>
    /// 按视图过滤 marks：只保留 <see cref="DrawingMark.View"/> == <paramref name="view"/> 的项。
    /// <para>Group 的 View 匹配 → 整组（含所有子项）参与判定（不再过滤子项，符合
    /// 「父 Group 视图代表子项视图」的语义）；Group 的 View 不匹配 → 整组跳过。</para>
    /// <para>被过滤的 mark 不产生 verdict（不判 Missing），避免单面照片上误报缺标。</para>
    /// </summary>
    /// <summary>两个归一化框 [x, y, w, h] 是否相交（有效部位区域判定用）。</summary>
    private static bool BoxesIntersect(double[] a, double[] b)
    {
        double ax1 = a[0], ay1 = a[1], ax2 = a[0] + a[2], ay2 = a[1] + a[3];
        double bx1 = b[0], by1 = b[1], bx2 = b[0] + b[2], by2 = b[1] + b[3];
        return ax1 < bx2 && bx1 < ax2 && ay1 < by2 && by1 < ay2;
    }

    public static List<DrawingMark> FilterByView(IReadOnlyList<DrawingMark> marks, MarkView view)
    {
        var result = new List<DrawingMark>();
        foreach (var m in marks)
            if (m.View == view) result.Add(m);
        return result;
    }

    /// <summary>
    /// 计算视图级配准的 viewBbox。
    /// <para>优先使用视图标签文字框约束同视图 marks，并最终回退到 marks 并集。</para>
    /// </summary>
    /// <param name="filteredMarks">已按视图过滤的 marks（FilterByView 的输出）</param>
    /// <param name="maxDist">marks 与视图标签中心的最大归一化距离（默认 1.0），
    /// 超出此距离的 marks 视为散乱项，排除出 viewBbox 计算。
    /// M6 实测：0.35 过严，将「服务热线」(d=0.53) 等 TopCover 合法 marks 误排，
    /// 导致 viewBbox 收窄、照片裁剪后服务热线越界→Missing。放宽至 1.0 保留全部同视图 marks。</param>
    public static double[]? ViewBboxOf(IReadOnlyList<DrawingMark> filteredMarks, double maxDist = 1.0)
    {
        var nonGroup = EnumerateNonGroup(filteredMarks).ToList();

        // 视图标签的 NormBbox（同 view 的 marks 应共享同一视图标签锚点）
        var labelBbox = nonGroup
            .Where(m => m.ViewBbox is { Length: >= 4 })
            .Select(m => m.ViewBbox)
            .FirstOrDefault();

        // 视图词有时来自二维码格式说明/注释，框本身只有一行小字。
        // 这种框不是产品视图范围：若拿它做 RebaseToView，所有 mark 会被压缩到
        // 说明文字附近，导致照片裁剪和定点检测整体错位。小标签直接忽略，
        // 回退到同视图实际 mark 的并集（没有 mark 时再返回 null）。
        if (labelBbox is { Length: >= 4 } && labelBbox[2] > 0 && labelBbox[3] > 0
            && labelBbox[2] * labelBbox[3] < MinViewLabelArea)
            labelBbox = null;

        // 无视图标签 → 回退到 marks 的 UnionBbox（老档案 / ViewBbox 全 null）
        if (labelBbox is null || labelBbox.Length < 4)
            return UnionBbox(nonGroup.Select(m => m.NormBbox));

        // 视图标签中心
        var lcx = labelBbox[0] + labelBbox[2] / 2;
        var lcy = labelBbox[1] + labelBbox[3] / 2;

        // 只保留距视图标签 maxDist 内的 marks（排除散乱 marks）
        var bboxes = new List<double[]> { labelBbox };
        foreach (var m in nonGroup)
        {
            if (m.NormBbox is not { Length: >= 4 }) continue;
            var mcx = m.NormBbox[0] + m.NormBbox[2] / 2;
            var mcy = m.NormBbox[1] + m.NormBbox[3] / 2;
            var d = Math.Sqrt((mcx - lcx) * (mcx - lcx) + (mcy - lcy) * (mcy - lcy));
            if (d <= maxDist)
                bboxes.Add(m.NormBbox);
        }

        return UnionBbox(bboxes);
    }

    /// <summary>
    /// 递归遍历 marks 中所有非 Group 类型的 mark（含 Group 的子项），用于视图级 NormBbox 计算。
    /// </summary>
    public static IEnumerable<DrawingMark> EnumerateNonGroup(IEnumerable<DrawingMark> marks)
    {
        foreach (var m in marks)
        {
            if (m.Type != MarkType.Group) yield return m;
            foreach (var kid in EnumerateNonGroup(m.Children ?? new List<DrawingMark>()))
                yield return kid;
        }
    }

    private static int Rank(TextMatch t) => t switch
    {
        TextMatch.Exact => 3,
        TextMatch.Normalized => 2,
        TextMatch.Ambiguous => 1,
        _ => 0
    };

    private static string Aggregate(List<string> states)
    {
        // 空组合 = 部位容器组（M2 实测：新约克等图纸的 Group 无子项，只是部位声明），
        // 不是处理错误 —— 如实标灰
        if (states.Count == 0) return nameof(MarkState.NotApplicable);
        if (states.All(s => s == nameof(MarkState.Matched))) return nameof(MarkState.Matched);
        if (states.Contains(nameof(MarkState.NotComparable))) return nameof(MarkState.NotComparable);
        if (states.Contains(nameof(MarkState.Missing))) return nameof(MarkState.Missing);
        if (states.Contains(nameof(MarkState.ProcessingError))) return nameof(MarkState.ProcessingError);
        // S1：未检出（黄）归入需复核，不升级为缺标（红）
        if (states.Contains(nameof(MarkState.NotDetected))) return nameof(MarkState.NotDetected);
        return nameof(MarkState.LowConfidence);
    }

    private static double[]? Center(double[]? nb)
        => nb is { Length: >= 4 } ? new[] { nb[0] + nb[2] / 2, nb[1] + nb[3] / 2 } : null;

    /// <summary>
    /// 位置是否可接受：中心距离 ≤ <paramref name="posTol"/>，**或** 观测框与预期框的交叠占预期框 ≥ 50%。
    /// <para>【实测教训（M4 端点验证）】verify 端的行内合并会把同一行多个打标内容
    /// （如「MAC:A42985377FA0　PC-P1HEQ2 服务热线：4008601111」）并成一个长条框，
    /// 其中心必然偏离行内任一单项的预期框中心 —— 单看中心距离会把明明压在预期框上
    /// 的内容误判成黄。交叠比判据对长条观测框稳健。</para>
    /// <para>【视图级配准（方案 B）】调用方传 <see cref="ViewPosTol"/>（当 ViewNormMap 非空）或
    /// <see cref="PosTol"/>（向后兼容）。两种容差的取舍见 <see cref="PosTolOf"/></para>
    /// </summary>
    private static bool PositionOk(double[]? expected, double[]? observed, double? centerOffset, double posTol)
    {
        if (centerOffset is not null && centerOffset <= posTol) return true;
        if (expected is not { Length: >= 4 } || observed is not { Length: >= 4 }) return centerOffset is null;
        double ix = Math.Min(expected[0] + expected[2], observed[0] + observed[2])
                    - Math.Max(expected[0], observed[0]);
        double iy = Math.Min(expected[1] + expected[3], observed[1] + observed[3])
                    - Math.Max(expected[1], observed[1]);
        if (ix <= 0 || iy <= 0) return false;
        var ea = expected[2] * expected[3];
        return ea > 0 && ix * iy / ea >= 0.5;
    }

    private static double? Offset(double[]? expected, double[]? observed)
    {
        var ce = Center(expected);
        var co = Center(observed);
        if (ce is null || co is null) return null;
        return Math.Round(Dist(ce, co), 4);
    }

    private static double Dist(double[] a, double[] b)
        => Math.Sqrt((a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]));

    private static string ObsJoin(RegionObs obs)
        => string.Join(" | ", obs.Texts.Take(5).Select(t => $"「{t.Text}」"));

    // ---------------- 观测模型 ----------------

    private sealed class RegionObs
    {
        public string Id = "";
        public bool QrFound;
        public string? QrData;
        public string? QrScale;
        public double[]? QrNormBbox;
        public List<string> QrScalesTried = new();
        public List<TextObs> Texts = new();
        public double? InkRatio;
        public string? Error;

        public static RegionObs? Parse(JsonElement e)
        {
            if (e.ValueKind != JsonValueKind.Object) return null;
            var o = new RegionObs();
            if (e.TryGetProperty("id", out var id) && id.ValueKind == JsonValueKind.String)
                o.Id = id.GetString() ?? "";
            if (e.TryGetProperty("error", out var er) && er.ValueKind == JsonValueKind.String)
                o.Error = er.GetString();

            if (e.TryGetProperty("qr", out var qr) && qr.ValueKind == JsonValueKind.Object)
            {
                if (qr.TryGetProperty("found", out var f))
                    o.QrFound = f.ValueKind == JsonValueKind.True;
                if (qr.TryGetProperty("data", out var d) && d.ValueKind == JsonValueKind.String)
                    o.QrData = d.GetString();
                if (qr.TryGetProperty("scale", out var sc) && sc.ValueKind == JsonValueKind.Number)
                    o.QrScale = sc.GetRawText();
                if (qr.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array)
                    o.QrNormBbox = Doubles(nb);
                if (qr.TryGetProperty("scales_tried", out var st) && st.ValueKind == JsonValueKind.Array)
                    foreach (var s in st.EnumerateArray())
                        o.QrScalesTried.Add(s.GetRawText());
                // M6 ink_ratio 辅助：QR 类型的 region 只有 entry["qr"] 没有 entry["presence"]，
                // 需从 qr.ink_ratio 读取（verify.py 的 _verify_qr 已返回此字段）
                if (qr.TryGetProperty("ink_ratio", out var qir) && qir.ValueKind == JsonValueKind.Number)
                    o.InkRatio = qir.GetDouble();
            }
            if (e.TryGetProperty("texts", out var ts) && ts.ValueKind == JsonValueKind.Array)
            {
                foreach (var t in ts.EnumerateArray())
                {
                    if (t.ValueKind != JsonValueKind.Object) continue;
                    var to = new TextObs();
                    if (t.TryGetProperty("text", out var tx) && tx.ValueKind == JsonValueKind.String)
                        to.Text = tx.GetString() ?? "";
                    if (t.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array)
                        to.NormBbox = Doubles(nb);
                    o.Texts.Add(to);
                }
            }
            if (e.TryGetProperty("presence", out var pr) && pr.ValueKind == JsonValueKind.Object &&
                pr.TryGetProperty("ink_ratio", out var ir) && ir.ValueKind == JsonValueKind.Number)
                o.InkRatio = ir.GetDouble();
            return o.Id.Length > 0 ? o : null;
        }

        internal static double[]? Doubles(JsonElement a)
        {
            var list = new List<double>();
            foreach (var v in a.EnumerateArray())
                if (v.ValueKind == JsonValueKind.Number) list.Add(v.GetDouble());
            return list.Count >= 4 ? list.ToArray() : list.Count > 0 ? list.ToArray() : null;
        }
    }

    private sealed class TextObs
    {
        public string Text = "";
        public double[]? NormBbox;
    }

    /// <summary>
    /// R3（#35）全局最优一对一配对：**先最大化配对数，再最小化总距离**。
    /// 原贪心只保证「配对数最大」，总距离可能非最优；本实现在同等配对数下取总距离最小的解。
    /// n、m ≤ 6（实测最多 2 个 Qr mark），枚举可行；规模变大时应换匈牙利算法。
    /// </summary>
    private static List<(int I, int J)> BestOneToOne(IReadOnlyList<double[]> a, IReadOnlyList<double[]> b)
    {
        var best = new List<(int, int)>();
        double bestCost = double.MaxValue;
        if (a.Count == 0 || b.Count == 0) return best;

        var usedB = new bool[b.Count];
        var cur = new List<(int, int)>();

        void Dfs(int i, double cost)
        {
            if (i == a.Count)
            {
                if (cur.Count > best.Count || (cur.Count == best.Count && cost < bestCost))
                {
                    best = new List<(int, int)>(cur);
                    bestCost = cost;
                }
                return;
            }
            // 剪枝 1：剩余元素全用上也追不上当前最好配对数
            if (cur.Count + (a.Count - i) < best.Count) return;
            // 剪枝 2：只有在「配对数追平」时才用 cost 剪枝。
            // ⚠️ 不能无条件写 `if (cost >= bestCost) return;`：首次到达「空匹配」时 cost=0 会把
            // bestCost 压成 0，其后所有非空配对（cost>0）都会被误剪，最终恒返回空配对
            // —— 该缺陷在 300 组随机数据上 300/300 全错（已用暴力全枚举交叉验证发现并修正）。
            if (cur.Count + (a.Count - i) == best.Count && cost >= bestCost) return;
            Dfs(i + 1, cost);                                   // 不选 a[i]
            for (int j = 0; j < b.Count; j++)
            {
                if (usedB[j]) continue;
                usedB[j] = true;
                cur.Add((i, j));
                Dfs(i + 1, cost + Dist(a[i], b[j]));
                cur.RemoveAt(cur.Count - 1);
                usedB[j] = false;
            }
        }
        Dfs(0, 0.0);
        return best;
    }

    /// <summary>原贪心配对（距离升序依次取用未配过的），仅作为超大规模时的兜底。</summary>
    private static List<(int I, int J)> GreedyPairs(IReadOnlyList<double[]> a, IReadOnlyList<double[]> b)
    {
        var cand = new List<(double Dist, int I, int J)>();
        for (int i = 0; i < a.Count; i++)
            for (int j = 0; j < b.Count; j++)
                cand.Add((Dist(a[i], b[j]), i, j));
        cand.Sort((x, y) => x.Dist.CompareTo(y.Dist));
        var usedD = new HashSet<int>();
        var usedB = new HashSet<int>();
        var outp = new List<(int, int)>();
        foreach (var (_, i, j) in cand)
        {
            if (usedD.Contains(i) || usedB.Contains(j)) continue;
            usedD.Add(i); usedB.Add(j);
            outp.Add((i, j));
        }
        return outp;
    }

    private sealed class BlindCode
    {
        public string? Data;
        public double[]? NormBbox;

        public static BlindCode? Parse(JsonElement e)
        {
            if (e.ValueKind != JsonValueKind.Object) return null;
            var o = new BlindCode();
            if (e.TryGetProperty("data", out var d) && d.ValueKind == JsonValueKind.String)
                o.Data = d.GetString();
            if (e.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array)
            {
                var list = new List<double>();
                foreach (var v in nb.EnumerateArray())
                    if (v.ValueKind == JsonValueKind.Number) list.Add(v.GetDouble());
                if (list.Count >= 4) o.NormBbox = list.ToArray();
            }
            return o;
        }
    }
}
