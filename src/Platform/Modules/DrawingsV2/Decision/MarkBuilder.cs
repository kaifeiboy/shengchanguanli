using System.Text;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// 应打标对象构造器 —— v2 决策层的核心。
///
/// 【设计原则：正向证据驱动】
/// 只有拿到**正向证据**的块才会成为 mark：条款声明、已知打标内容命中、图像对象。
/// 不做「圈一块区域再排除」的脆弱启发式 —— 那是旧方案 drawing_blocks 的老路。
///
/// 【三条证据链（RuleId）】
///   R1 条款声明：图纸自己在「技术要求」里写明要打什么（最高可信）
///   R2 已知内容：条款没写，但图上出现了已实测的打标内容词（兜底，置信度较低）
///   R3 图像对象：页面上的图像 → 二维码 / 图标（方案：只验存在性与位置，不解码）
///
/// 【可追溯】
/// 每条 mark 都带 MarkSource（RuleId + SpanIds/ImageIds + Evidence 文本），
/// 任何一条判定都能回溯到具体是哪一页的哪几个 span / image 支撑的。
/// </summary>
public static class MarkBuilder
{
    /// <summary>构造器输出：marks + 完整诊断信息。</summary>
    public sealed class Result
    {
        public string DrawingKey { get; set; } = "";
        public List<DrawingMark> Marks { get; } = new();
        public List<string> DeclaredItems { get; } = new();
        /// <summary>
        /// 适用范围：条款声明的**部位**（"上盖顶部"/"下盖接线"）。
        /// 这些不是打标内容项，而是指明"打在哪个部位"，其内容需视觉层或人工确认。
        /// </summary>
        public List<string> Scopes { get; } = new();
        public List<string> Warnings { get; } = new();
        public Dictionary<string, int> RuleHits { get; } = new();
        public bool VisionFallbackRequired { get; set; }
        public string? Source { get; set; }
    }

    /// <summary>内部文本块（由同一 block_index 的 span 归并而来）。</summary>
    private sealed class Block
    {
        public int Index;
        public string Text = "";
        public double X0, Y0, X1, Y1;      // 标准坐标系 pt
        public double[]? Norm;
        public List<string> SpanIds = new();
        public double Angle;
        public bool IsRevision;
        public bool IsDimension;
        public bool IsClause;
        public bool IsNote;
        public bool IsQrDecl;

        public double Cx => (X0 + X1) / 2;
        public double Cy => (Y0 + Y1) / 2;
        public double W => X1 - X0;
        public double H => Y1 - Y0;
    }

    private static void Hit(Result r, string rule)
    {
        r.RuleHits[rule] = r.RuleHits.TryGetValue(rule, out var n) ? n + 1 : 1;
    }

    /// <summary>从已解析的 vpdf 文档构造应打标对象清单。</summary>
    public static Result Build(VpdfDocument doc, string drawingKey)
    {
        var r = new Result { DrawingKey = drawingKey };
        var pages = doc.Pages ?? new List<VpdfPage>();
        if (pages.Count == 0)
        {
            r.Warnings.Add("文档无页面");
            return r;
        }

        int seq = 0;
        int NextId() => seq++;   // mark 自增编号；以委托传给 EmitGrouped（ref 不能进 lambda）
        foreach (var page in pages)
        {
            r.VisionFallbackRequired |= page.Diagnostics?.VisionFallbackRequired ?? false;
            r.Source = page.Diagnostics?.Source;

            var (W, H) = VectorPdfParser.PageSize(page);
            var blocks = GroupBlocks(page);
            Classify(blocks);

            // ---- 证据链 1：条款声明 ----
            var declared = ExtractDeclared(blocks, r);
            foreach (var d in declared)
                if (!r.DeclaredItems.Contains(d)) r.DeclaredItems.Add(d);

            var candidates = blocks
                .Where(b => !b.IsRevision && !b.IsClause && !b.IsDimension && b.Text.Length > 0)
                .ToList();

            // ---- R1：条款声明项在图上定位 ----
            // 同一文本块可能同时承载多个打标项（实测："地暖阀LOW VOLTAGE禁止强电AB" 同时含
            // 「地暖阀」与「禁止强电」），此时合并为 Group，避免产出多个坐标完全相同的 mark。
            var byBlock = new Dictionary<Block, List<(string Item, string Key, TextMatch M)>>();

            // "spanId|spanId" 作为块的稳定签名，用于 R1/R2 交叉去重
            static string Sig(IEnumerable<string> ids) => string.Join("|", ids);
            // 已被 R1 认领的 (块, 项)，R2 不再重复产出
            var covered = new HashSet<string>();

            foreach (var item in declared)
            {
                // 「二维码」由图像证据链处理，不按文字找
                if (MarkRules.ContainsQrWord(item)) continue;

                // 部位/工艺短语（"上盖顶部"/"下盖接线"/"按键丝印"）不是打标内容项：
                // 图纸声明的是「打在哪个部位」，图上不会印这串字，按内容项去定位必然失败。
                // 记为适用范围，并把内容确认交给视觉层/人工。
                if (MarkRules.IsLocationPhrase(item))
                {
                    if (!r.Scopes.Contains(item)) r.Scopes.Add(item);
                    r.VisionFallbackRequired = true;
                    r.Marks.Add(new DrawingMark
                    {
                        Id = $"{drawingKey}#{seq++:D3}",
                        Type = MarkType.Group,
                        Text = item,
                        Required = true,
                        View = MarkRules.ViewOf(MarkRules.FindViewWord(item)),
                        Confidence = 0.40,
                        Source = new MarkSource
                        {
                            Kind = "pdf_vector",
                            PageIndex = page.Index,
                            RuleId = "R1-scope",
                            Evidence = $"条款声明部位「{item}」为打标工艺，但部位本身无可定位的文本实例；" +
                                       $"该部位的打标内容需由视觉层或人工确认"
                        }
                    });
                    Hit(r, "R1-scope");
                    continue;
                }

                var located = false;
                foreach (var key in MarkRules.MatchKeysOf(item))
                {
                    if (key.Length < 2) continue;
                    var hit = candidates.FirstOrDefault(b => TextNormalizer.Contains(b.Text, key) is var m && m != TextMatch.None);
                    if (hit is null) continue;

                    var match = TextNormalizer.Contains(hit.Text, key);
                    if (!byBlock.TryGetValue(hit, out var list)) byBlock[hit] = list = new();
                    if (list.All(x => x.Item != item)) list.Add((item, key, match));
                    located = true;
                    break;   // 命中即止，避免同一项被多个别名重复建 mark
                }

                if (!located)
                {
                    r.Warnings.Add($"条款声明项「{item}」未在图上定位到实例");
                    r.Marks.Add(new DrawingMark
                    {
                        Id = $"{drawingKey}#{seq++:D3}",
                        Type = MarkType.Text,
                        Text = item,
                        Required = true,
                        Confidence = 0.30,
                        Source = new MarkSource
                        {
                            Kind = "pdf_vector",
                            PageIndex = page.Index,
                            RuleId = "R1-noloc",
                            Evidence = $"条款声明「{item}」，但图上未找到对应实例（多为部位/工艺描述，需人工复核）"
                        }
                    });
                    Hit(r, "R1-noloc");
                }
            }

            // R1 发射：同块多声明项合并为 Group
            foreach (var (blk, list) in byBlock)
            {
                foreach (var x in list) covered.Add($"{Sig(blk.SpanIds)}::{x.Item}");
                EmitGrouped(r, drawingKey, NextId, page, blocks, W, H, blk,
                    list.Select(x => (x.Item, x.Key, x.M)).ToList(), "R1",
                    (name, key, m) => $"条款声明「{name}」，图上按匹配键「{key}」命中（{m}）");
            }

            // ---- R2：已知打标内容兜底 ----
            // 与 R1 共用「同块合并」逻辑：实测同一块常同时含多个已知内容词
            //（"LOW VOLTAGE禁止强电地暖阀DC15/24V" 会同时命中 3 项），
            // 逐条建 mark 只会产出多个坐标完全相同的重复项。
            var byBlock2 = new Dictionary<Block, List<(string Item, string Key, TextMatch M)>>();
            foreach (var b in candidates)
            {
                foreach (var (name, forms) in MarkRules.KnownContent)
                {
                    if (name is "二维码") continue;
                    foreach (var form in forms)
                    {
                        var m = TextNormalizer.Contains(b.Text, form);
                        if (m is TextMatch.None) continue;
                        // R1 已认领过此项（或本块已收录）则跳过 —— Add 返回 false 即表示已存在
                        if (!covered.Add($"{Sig(b.SpanIds)}::{name}")) break;
                        if (!byBlock2.TryGetValue(b, out var list)) byBlock2[b] = list = new();
                        list.Add((name, form, m));
                        break;
                    }
                }
            }
            foreach (var (blk, list) in byBlock2)
            {
                EmitGrouped(r, drawingKey, NextId, page, blocks, W, H, blk, list, "R2",
                    (name, key, m) => $"图上命中已知打标内容词表「{name}」的写法「{key}」（{m}）");
            }

            // ---- R4：图面候选（无条款声明图纸的兜底） ----
            // 背景：实测语料里有一类图纸（10寸屏约克/日立/海信）条款中不写打标内容，
            // 但图面确实印着打标内容（"POWER LINE IS FORBIDDEN禁止接入强电"、端子标识 "RS485-1"/"H-LINK"、
            // 接口标识 "SD"/"RESET"/"USB"）。封闭词表必然漏检 —— 词表再全也追不上新图纸。
            // 因此设 R4：排除标题栏/尺寸/修订/条款/噪声后，剩下的文本块作为**候选**，
            // Required=false、置信度低，由人工复核页确认后转为正式项 —— 不猜"合格与否"，只提供候选。
            if (declared.Count == 0)
            {
                var byBlock4 = new Dictionary<Block, List<(string Item, string Key, TextMatch M)>>();
                foreach (var b in candidates)
                {
                    if (MarkRules.IsNoiseBlock(b.Text)) continue;
                    if (byBlock2.ContainsKey(b)) continue;   // 已被 R2 认领的块不再作为候选
                    if (b.IsQrDecl) continue;                // "…二维码格式：" 是 R3 的证据来源，不是打标内容
                    if (b.IsNote || MarkRules.IsConditionalNote(b.Text)) continue;   // 注释/条件说明
                    if (MarkRules.InTitleBand(b.Cx / W, b.Cy / H)) continue;         // 标题栏带
                    byBlock4[b] = new List<(string, string, TextMatch)>
                        { (b.Text, b.Text, TextMatch.Exact) };
                }
                foreach (var (blk, list) in byBlock4)
                {
                    EmitGrouped(r, drawingKey, NextId, page, blocks, W, H, blk, list, "R4",
                        (_, _, _) => "图面候选：条款未声明打标内容，此为排除标题栏/尺寸/修订后的图面文本，需人工确认");
                }
            }

            // ---- R3：图像对象 → 二维码 / 图标 ----
            foreach (var img in page.Images ?? new List<VpdfImage>())
            {
                var nb = img.NormBbox;
                if (nb is null || nb.Length < 4) continue;
                var b = img.Bbox;
                if (b is null || b.Length < 4) continue;

                var iw = b[2] - b[0];
                var ih = b[3] - b[1];
                var icx = (b[0] + b[2]) / 2;
                var icy = (b[1] + b[3]) / 2;

                // 关联证据：最近的、含「二维码」字样的文字块（距离以图像自身尺寸为单位，非页面常量）
                Block? near = null;
                var bestD = double.MaxValue;
                foreach (var tb in blocks)
                {
                    if (!MarkRules.ContainsQrWord(tb.Text)) continue;
                    var d = Math.Sqrt((tb.Cx - icx) * (tb.Cx - icx) + (tb.Cy - icy) * (tb.Cy - icy));
                    if (d < bestD) { bestD = d; near = tb; }
                }
                var scale = Math.Max(iw, ih);
                var hasQrEvidence = near is not null && scale > 0 && bestD <= scale * 2.5;

                var mark = new DrawingMark
                {
                    Id = $"{drawingKey}#{seq++:D3}",
                    Type = hasQrEvidence ? MarkType.Qr : MarkType.Icon,
                    Text = null,                      // 方案：二维码/图标只验存在性与位置，不记录解码文本
                    Required = true,
                    NormBbox = nb,
                    Bbox = b,
                    Direction = 0,
                    Confidence = hasQrEvidence ? 0.90 : 0.50,
                    Source = new MarkSource
                    {
                        Kind = "pdf_vector",
                        PageIndex = page.Index,
                        RuleId = hasQrEvidence ? "R3-qr" : "R3-icon",
                        ImageIds = new List<string> { img.Id ?? "" },
                        Evidence = hasQrEvidence
                            ? $"图像对象 {img.Id} 邻近含「二维码」的文字块「{Trunc(near!.Text, 24)}」（距离 {bestD:F1}pt ≤ 2.5×图像尺寸 {scale:F1}pt）"
                            : $"图像对象 {img.Id}（{img.Width}×{img.Height}px）无二维码文字证据，按图标候选处理"
                    }
                };
                var (r3View, r3ViewBbox) = InferView(blocks, icx, icy, W, H);
                mark.View = r3View;
                mark.ViewBbox = r3ViewBbox;
                ApplyCondition(blocks, mark);
                r.Marks.Add(mark);
                Hit(r, hasQrEvidence ? "R3-qr" : "R3-icon");
            }
        }

        Dedupe(r);

        // 「没有任何应打标对象」必须与「解析失败」区分开：
        // 实测 02-4K3GR 的技术要求里通篇没有打标声明（只有喷漆/公差/装配），
        // 此时 0 mark 是正确结论，但必须显式说明，否则会被误当成解析器故障。
        if (r.Marks.Count == 0)
        {
            r.Warnings.Add(r.VisionFallbackRequired
                ? "图纸无可用文本层，需走视觉层识别（vpdf 不产出 mark）"
                : "图纸未声明打标内容，且图面未发现打标候选（可能本图纸不适用于打标比对）");
        }

        return r;
    }

    // ---------------- 归并 / 分类 ----------------

    private static List<Block> GroupBlocks(VpdfPage page)
    {
        var groups = new Dictionary<int, List<VpdfSpan>>();
        foreach (var s in page.TextSpans ?? new List<VpdfSpan>())
        {
            if (!groups.TryGetValue(s.BlockIndex, out var l)) groups[s.BlockIndex] = l = new List<VpdfSpan>();
            l.Add(s);
        }

        var blocks = new List<Block>();
        foreach (var (bi, spans) in groups)
        {
            spans.Sort((a, b) =>
            {
                var ay = a.Bbox is { Length: >= 2 } ? a.Bbox[1] : 0;
                var by = b.Bbox is { Length: >= 2 } ? b.Bbox[1] : 0;
                var c = ay.CompareTo(by);
                if (c != 0) return c;
                var ax = a.Bbox is { Length: >= 1 } ? a.Bbox[0] : 0;
                var bx = b.Bbox is { Length: >= 1 } ? b.Bbox[0] : 0;
                return ax.CompareTo(bx);
            });

            var x0 = double.MaxValue; var y0 = double.MaxValue;
            var x1 = double.MinValue; var y1 = double.MinValue;
            var sb = new StringBuilder();
            var ids = new List<string>();
            var angles = new HashSet<double>();

            foreach (var s in spans)
            {
                if (s.Bbox is { Length: >= 4 })
                {
                    x0 = Math.Min(x0, s.Bbox[0]); y0 = Math.Min(y0, s.Bbox[1]);
                    x1 = Math.Max(x1, s.Bbox[2]); y1 = Math.Max(y1, s.Bbox[3]);
                }
                sb.Append(s.Text ?? "");
                if (s.Id is not null) ids.Add(s.Id);
                angles.Add(s.Angle);
            }

            if (x0 == double.MaxValue) { x0 = y0 = x1 = y1 = 0; }

            blocks.Add(new Block
            {
                Index = bi,
                Text = sb.ToString().Trim(),
                X0 = x0, Y0 = y0, X1 = x1, Y1 = y1,
                SpanIds = ids,
                Angle = angles.Count > 0 ? angles.First() : 0,
                Norm = spans.FirstOrDefault(s => s.NormBbox is { Length: >= 4 })?.NormBbox
            });
        }

        blocks.Sort((a, b) =>
        {
            var c = Math.Round(a.Y0).CompareTo(Math.Round(b.Y0));
            return c != 0 ? c : a.X0.CompareTo(b.X0);
        });
        return blocks;
    }

    private static void Classify(List<Block> blocks)
    {
        foreach (var b in blocks)
        {
            b.IsClause = MarkRules.ClauseMarkers.Any(m => b.Text.Contains(m, StringComparison.Ordinal));
            b.IsRevision = MarkRules.IsRevisionNote(b.Text);
            b.IsDimension = MarkRules.IsDimension(b.Text);
            b.IsNote = b.Text.Contains("注：", StringComparison.Ordinal)
                       || b.Text.Contains("注:", StringComparison.Ordinal)
                       || b.Text.Contains("说明", StringComparison.Ordinal);
            b.IsQrDecl = MarkRules.IsQrFormatDeclaration(b.Text);
        }
    }

    // ---------------- 证据链 1：条款声明解析 ----------------

    private static List<string> ExtractDeclared(List<Block> blocks, Result r)
    {
        var items = new List<string>();
        foreach (var b in blocks)
        {
            if (!b.IsClause) continue;

            // 先按编号切子项（"6、MAC地址、…处均使用激光打标，灰色效果。"），
            // 再在每个子项里找「工艺词 + 声明终结标记」。
            // 直接按 。 分句是错的：实测条款编号之间用的是 "." 或 "，"。
            foreach (var clauseItem in MarkRules.SplitClauseItems(b.Text))
            {
                if (!MarkRules.ContainsProcessWord(clauseItem)) continue;
                var end = MarkRules.FindDeclarationEnd(clauseItem);
                if (end <= 0) continue;

                var head = MarkRules.StripClauseNumber(clauseItem[..end]);
                if (head.Length == 0) continue;

                foreach (var it in MarkRules.SplitItems(head))
                    if (!items.Contains(it)) items.Add(it);

                Hit(r, "R1-decl");
            }
        }
        return items;
    }

    // ---------------- 组装 ----------------

    /// <summary>
    /// 把一个文本块上命中的若干打标项发射为 mark。
    ///
    /// 【为什么必须按块合并】
    /// 实测同一文本块常同时承载多个打标项：
    ///   R1 —— "地暖阀LOW VOLTAGE禁止强电AB" 同时满足「地暖阀」与「禁止强电」两项声明
    ///   R2 —— "LOW VOLTAGE禁止强电地暖阀DC15/24V" 同时命中 3 个已知内容词
    /// 逐项建 mark 会产出多个坐标完全相同的重复项，下游渲染与比对都会重复。
    /// 因此：单命中 → 直接建 mark；多命中 → 合并为一个 Group，子项放 Children 保留可追溯。
    /// </summary>
    private static void EmitGrouped(Result r, string drawingKey, Func<int> nextId, VpdfPage page,
        List<Block> blocks, double W, double H, Block blk,
        List<(string Item, string Key, TextMatch M)> list, string ruleBase,
        Func<string, string, TextMatch, string> evidenceOf)
    {
        if (list.Count == 0) return;

        var (conf, ambConf, required) = ruleBase switch
        {
            "R1" => (0.95, 0.55, true),    // 条款声明 —— 最高可信
            "R2" => (0.75, 0.50, true),    // 已知内容词表兜底
            _ => (0.35, 0.30, false)      // R4 图面候选 —— 仅候选，不参与强制比对
        };

        // 视图推断在 Select 外完成（InferView 现在返回 (view, viewBbox) 元组）
        var (emitView, emitViewBbox) = InferView(blocks, blk.Cx, blk.Cy, W, H);

        var kids = list.Select(x => new DrawingMark
        {
            Id = $"{drawingKey}#{nextId():D3}",
            Type = MarkType.Text,
            Text = x.Item,
            Required = required,
            NormBbox = NormOf(blk, W, H),
            Bbox = new[] { blk.X0, blk.Y0, blk.X1, blk.Y1 },
            Direction = blk.Angle,
            Confidence = x.M == TextMatch.Ambiguous ? ambConf : conf,
            View = emitView,
            ViewBbox = emitViewBbox,
            Source = new MarkSource
            {
                Kind = "pdf_vector",
                PageIndex = page.Index,
                RuleId = x.M == TextMatch.Ambiguous ? ruleBase + "-ambiguous" : ruleBase,
                SpanIds = new List<string>(blk.SpanIds),
                Evidence = evidenceOf(x.Item, x.Key, x.M) + "；块文本「" + Trunc(blk.Text, 40) + "」"
            }
        }).ToList();
        ApplyCondition(blocks, kids);

        if (kids.Count == 1)
        {
            r.Marks.Add(kids[0]);
        }
        else
        {
            r.Marks.Add(new DrawingMark
            {
                Id = $"{drawingKey}#{nextId():D3}",
                Type = MarkType.Group,
                Text = string.Join("+", kids.Select(k => k.Text)),
                Required = required,
                NormBbox = NormOf(blk, W, H),
                Bbox = new[] { blk.X0, blk.Y0, blk.X1, blk.Y1 },
                Direction = blk.Angle,
                Confidence = kids.Min(k => k.Confidence),
                View = kids[0].View,
                ViewBbox = kids[0].ViewBbox,
                Children = kids,
                Source = new MarkSource
                {
                    Kind = "pdf_vector",
                    PageIndex = page.Index,
                    RuleId = ruleBase + "-group",
                    SpanIds = new List<string>(blk.SpanIds),
                    Evidence = $"同一文本块承载 {kids.Count} 个打标项「{string.Join("+", kids.Select(k => k.Text))}」" +
                               $"（{ruleBase}）；块文本「{Trunc(blk.Text, 40)}」"
                }
            });
        }
        foreach (var k in kids) Hit(r, k.Source.RuleId ?? ruleBase);
    }

    /// <summary>归一化 [x, y, w, h] ∈ [0,1]（相对页面标准坐标系）。</summary>
    private static double[] NormOf(Block b, double W, double H)
    {
        if (W <= 0 || H <= 0) return new[] { 0.0, 0.0, 0.0, 0.0 };
        double Clamp(double v) => v < 0 ? 0 : (v > 1 ? 1 : v);
        return new[]
        {
            Math.Round(Clamp(b.X0 / W), 6),
            Math.Round(Clamp(b.Y0 / H), 6),
            Math.Round(Clamp((b.X1 - b.X0) / W), 6),
            Math.Round(Clamp((b.Y1 - b.Y0) / H), 6)
        };
    }

    /// <summary>
    /// 视图推断（最近邻）：在整页里找到**离目标点最近**的视图标签文字块。
    ///
    /// 【为什么不能用「阅读顺序第一个命中」】
    /// 首版实现取的是 blocks 里第一个含视图词的块，而 blocks 按阅读顺序排序，
    /// 于是整页所有 mark 都被打上同一个视图（实测：图上同时有上盖与下盖标注时全部归为「上盖」）。
    /// 正确做法是按空间距离取最近 —— 打标内容总是与它所标注的部位文字相邻。
    ///
    /// 【M6 修正·视图词范围】
    /// 原实现找的是「含视图词的任意文字块」，但视图词（如「上盖」）同时出现在：
    ///   a) 视图标签「上盖视图」（位于视图区域顶部，是真正的视图锚点）
    ///   b) 条款文本「上盖顶部和下盖接线为激光镭刻工艺」（位于技术要求区，远离视图区域）
    ///   c) 二维码格式声明「上盖固定板二维码格式：」（near the QR, IS a view anchor）
    /// 导致同 view 的 marks 被锚定到不同的视图词块，在页面上散乱分布。
    ///
    /// 修正：排除 clause（技术要求区，远离视图）和 revision（修订表），
    /// 但**保留 qrDecl**（「上盖二维码格式：」本身就是视图锚点，它标明了二维码在哪个面）
    /// 和 note/dimension（注释和尺寸通常在视图内部）。
    ///
    /// 同时返回视图标签块的 NormBbox（viewBbox），供视图级配准替代 marks UnionBbox 使用。
    /// 距离在**归一化坐标**下计算（除以页面宽高），使横/竖版图纸行为一致。
    /// </summary>
    private static (MarkView view, double[]? viewBbox) InferView(List<Block> blocks, double cx, double cy, double W, double H)
    {
        if (W <= 0 || H <= 0) return (MarkView.Unspecified, null);

        // 1) 自身文本带视图词 —— 最高优先（自身块就是视图标签）
        foreach (var b in blocks)
        {
            if (cx < b.X0 - 1 || cx > b.X1 + 1 || cy < b.Y0 - 1 || cy > b.Y1 + 1) continue;
            // 自身命中时，排除 clause/revision（技术要求区和修订表中的视图词不是视图锚点）
            if (b.IsClause || b.IsRevision) continue;
            var self = MarkRules.FindViewWord(b.Text);
            if (self is not null)
                return (MarkRules.ViewOf(self), b.Norm);
        }

        // 2) 最近邻视图标签块（仅排除 clause/revision）
        Block? best = null;
        var bestD = double.MaxValue;
        string? bestWord = null;
        foreach (var b in blocks)
        {
            // 【M6 修正】排除 clause/revision —— 技术要求区和修订表中的视图词远离实际视图区域。
            // 保留 qrDecl/note/dimension —— 它们通常在视图内部，是有效的视图锚点
            if (b.IsClause || b.IsRevision) continue;
            var w = MarkRules.FindViewWord(b.Text);
            if (w is null) continue;
            var dx = (b.Cx - cx) / W;
            var dy = (b.Cy - cy) / H;
            var d = dx * dx + dy * dy;
            if (d < bestD) { bestD = d; best = b; bestWord = w; }
        }
        return best is null ? (MarkView.Unspecified, null) : (MarkRules.ViewOf(bestWord), best.Norm);
    }

    /// <summary>把「以实际生产为准 / 按实际…打印」这类条件挂到 mark 上。</summary>
    private static void ApplyCondition(List<Block> blocks, DrawingMark mark)
    {
        var cond = FindCondition(blocks);
        if (cond is not null) mark.Condition = cond;
    }

    /// <summary>批量版本：同一组条件下发给多个 mark（R1 同块多声明项场景）。</summary>
    private static void ApplyCondition(List<Block> blocks, List<DrawingMark> marks)
    {
        if (marks.Count == 0) return;
        var cond = FindCondition(blocks);
        if (cond is null) return;
        foreach (var m in marks) m.Condition = cond;
    }

    private static string? FindCondition(List<Block> blocks)
    {
        foreach (var b in blocks)
        {
            if (!b.IsNote) continue;
            var t = b.Text;
            if (t.Contains("以实际", StringComparison.Ordinal)
                || t.Contains("按实际", StringComparison.Ordinal))
                return Trunc(t, 60);
        }
        return null;
    }

    /// <summary>
    /// 去重：同一组 span/id 且同名的 mark 只保留置信度最高的一条。
    /// 起因是 R1 与 R2 可能对同一处给出重复证据。
    /// </summary>
    private static void Dedupe(Result r)
    {
        var keep = new List<DrawingMark>();
        foreach (var m in r.Marks.OrderByDescending(x => x.Confidence))
        {
            var dup = keep.FirstOrDefault(k =>
                k.Type == m.Type
                && string.Equals(k.Text, m.Text, StringComparison.Ordinal)
                && k.Source.PageIndex == m.Source.PageIndex
                && (k.Source.SpanIds.Count > 0
                        ? k.Source.SpanIds.SequenceEqual(m.Source.SpanIds)
                        : k.Source.ImageIds.SequenceEqual(m.Source.ImageIds)));
            if (dup is null) keep.Add(m);
        }
        keep.Sort((a, b) => string.CompareOrdinal(a.Id, b.Id));
        r.Marks.Clear();
        r.Marks.AddRange(keep);
    }

    private static string Trunc(string s, int n)
        => string.IsNullOrEmpty(s) ? "" : (s.Length <= n ? s : s[..n] + "…");
}
