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
///   2. 二维码：定点解码 + 盲检兜底，**只验存在性与位置**（V2Models 注释即规范），
///      不把解码内容当合格依据；
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

    /// <summary>图标存在性判定：裁剪区前景墨迹占比下限（语料白底图 ink_ratio 1.3%~2.9%）。</summary>
    public const double IconInkMin = 0.02;

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
    }

    public sealed class Result
    {
        public List<Verdict> Verdicts { get; set; } = new();
        public bool PhotoUsable { get; set; } = true;
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

        public double VerifyMs { get; set; }
        public Dictionary<string, int> Counts => Verdicts
            .GroupBy(v => v.State)
            .ToDictionary(g => g.Key, g => g.Count());
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
        return letters >= 2 && digits >= 2;
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
        JsonElement? observeDoc)
    {
        if (viewNormMap is null || viewNormMap.Count == 0) return viewNormMap;

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

    public static Result Verify(IReadOnlyList<DrawingMark> marks,
                                JsonElement verifyDoc,
                                JsonElement? observeDoc,
                                MarkView? selectedView = null,
                                Dictionary<string, double[]>? precomputedViewNormMap = null)
    {
        var result = new Result();

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
                    ObservedData = bc.Data,
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
        // Group：逐子项判定，组状态取子项聚合（不再把组合文本当文本匹配）
        if (m.Type == MarkType.Group)
        {
            var childStates = new List<string>();
            foreach (var kid in m.Children ?? new List<DrawingMark>())
            {
                VerifyMark(kid, regions, blindCodes, blindTexts, result, m.Id, null);
                var kv = result.Verdicts.LastOrDefault(v => v.MarkKey == kid.Id);
                if (kv is not null) childStates.Add(kv.State);
            }
            var worst = Aggregate(childStates);
            result.Verdicts.Add(new Verdict
            {
                MarkKey = m.Id,
                Type = m.Type.ToString(),
                View = m.View.ToString(),
                Text = m.Text,
                State = worst,
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
            result.Verdicts.Add(v);
            return;
        }

        // 上层（组/照片）已判不可比 → 传染
        if (inheritedState == nameof(MarkState.NotComparable))
        {
            v.State = inheritedState;
            v.Evidence = "继承不可比状态";
            result.Verdicts.Add(v);
            return;
        }

        // 照片质量门槛（一次判全局）
        if (!result.PhotoUsable)
        {
            v.State = nameof(MarkState.NotComparable);
            v.Evidence = "照片质量不可用：" + string.Join("；", result.QualityReasons);
            result.Verdicts.Add(v);
            return;
        }

        var regionId = RegionIdOf(m.Id, parentKey);
        regions.TryGetValue(regionId, out var obs);
        if (obs is null)
            // 定点区域缺失时按 markKey 原样再找一次（Python 端 id 透传）
            regions.TryGetValue(m.Id, out obs);

        if (obs is null || obs.Error is not null)
        {
            v.State = nameof(MarkState.NotComparable);
            v.Evidence = obs?.Error is not null ? $"定点区域异常：{obs.Error}" : "定点区域无观测结果";
            result.Verdicts.Add(v);
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

        switch (best)
        {
            case TextMatch.Exact or TextMatch.Normalized:
            {
                var expected = ExpectedBbox(m, result);
                v.Offset = Offset(expected, bestBox);
                var tol = PosTolOf(result);
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
            v.ObservedData = obs.QrData;
            v.Offset = Offset(ExpectedBbox(m, result), obs.QrNormBbox);
            if (v.Offset is null || v.Offset <= PosTolOf(result) * 2) // 码中心允许更大漂移（裁剪+透视）
            {
                v.State = nameof(MarkState.Matched);
                v.Evidence = $"定点解码成功「{obs.QrData}」（scale={obs.QrScale}）";
            }
            else
            {
                v.State = nameof(MarkState.LowConfidence);
                v.Evidence = $"定点解码「{obs.QrData}」但中心偏移 {v.Offset:F3}";
            }
            return;
        }

        // 2) 盲检兜底：全图扫到的码落在预期位置附近
        var c = Center(ExpectedBbox(m, result));
        if (c is not null)
        {
            BlindCode? near = null;
            double nearDist = double.MaxValue;
            foreach (var bc in blindCodes)
            {
                var cc = Center(bc.NormBbox);
                if (cc is null) continue;
                var d = Dist(c, cc);
                if (d <= PosTolOf(result) * 2 && d < nearDist) { near = bc; nearDist = d; }
            }
            if (near is not null)
            {
                v.ObservedData = near.Data;
                v.State = nameof(MarkState.Matched);
                v.PhotoBbox = near.NormBbox;   // 盲检码的实际位置（照片标注用）
                v.Evidence = $"盲检码「{near.Data}」落于预期位置（距离 {nearDist:F3}）";
                return;
            }
            // 几何未配准：预期位置不可信，但全图只要解出了码，至少证明「照片上有码」——
            // 不判绿（位置未核对）也不判红（可能就是该标的码），只判黄交人工复核。
            if (!result.GeometryTrusted && blindCodes.Count > 0)
            {
                var any = blindCodes[0];
                v.ObservedData = any.Data;
                v.State = nameof(MarkState.LowConfidence);
                v.PhotoBbox = any.NormBbox;
                v.Evidence = $"盲检解出「{any.Data ?? "(未解码)"}」，但几何未配准、位置未核对（需人工复核）";
                return;
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

    private static void VerifyIcon(DrawingMark m, RegionObs obs, Result result, Verdict v)
    {
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
    public static List<DrawingMark> FilterByView(IReadOnlyList<DrawingMark> marks, MarkView view)
    {
        var result = new List<DrawingMark>();
        foreach (var m in marks)
            if (m.View == view) result.Add(m);
        return result;
    }

    /// <summary>
    /// 计算视图级配准的 viewBbox（M6 修正·视图词范围）。
    /// <para>优先用视图标签文字的 NormBbox（marks 的 <see cref="DrawingMark.ViewBbox"/>）作为锚点，
    /// 只保留距视图标签 <paramref name="maxDist"/> 内的 marks 参与并集，
    /// 排除因 InferView 误判而散乱分布的 marks，使 viewBbox 更贴合照片实际产品面。</para>
    /// <para>无视图标签时回退到 marks 的 UnionBbox（向后兼容老档案 / 无 ViewBbox 的 marks）。</para>
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
