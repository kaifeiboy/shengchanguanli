using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// 【2026-09-27 定位更新 · 保留不删】新匹配范式（照片 OCR 文本 → 图块打标内容命中，见
/// docs/逻辑图块方案_2026-09-26.md §0.5）确立后，本类的**打分器内核正是新范式的现成雏形**：
/// 它做的本来就是「照片侧文本/型号码/QR 特征 ↔ 图侧内容签名」的加权命中，而非几何找图块。
/// ⏸ 待 P3 改造（不现在改）：把签名 key 由 <c>MarkView</c> 换成 <c>block_id</c>，
/// Texts 换成块内 participate=1 的打标内容，只检索 has_marking=1 的块，
/// 输出 view_hint + Top-N 候选块（命中判据见方案 §0.5.3.1）。改造前保持现有行为不变。
///
/// 特征级找图块（设计 §5 · Phase 2 / 偏差 B1+B3）。
///
/// <para>【替代对象】脆弱的 <see cref="MarkVerifier.InferViewFromBlind"/>——一个"文本巧合匹配器"：
/// 盲检 OCR 文本恰好唯一命中某 mark 的 Text 才推断视图；0 命中或多视图冲突 → 返回 null → 降级整页比对。
/// 这正是"产品照片常匹配不到真实部位"的根因。</para>
///
/// <para>【做法】对图纸各视图的 marks 聚合"特征签名"（固定文本集合 / 型号码 / QR 预期码 / QR 数 / 图标数 / 相对质心），
/// 与照片盲检特征（texts + codes）做多特征加权打分，取 Top-N。</para>
///
/// <para>【选择语义】只有型号、已录入的 QR 内容，或至少两条固定文字构成强证据时才允许自动选视图。
/// 单个 QR 的存在性或一条通用文字只用于候选排序，不再直接触发生产判定。</para>
///
/// <para>【事实依据】v2_drawing_views 仅页面级字段、无几何字段（B2 已证）→ 视图特征签名只能从 marks 聚合。
/// observe 输出含 source.width/height（归一化布局）、texts[]{text,bbox}、codes[]{data,bbox}，无 icons → 图标数特征暂不作照片侧输入。</para>
/// </summary>
public static class BlockMatcher
{
    // 权重（初版，待真实照片校准；run_baseline 做回归闸门）
    private const double WModelCode = 6.0;   // 型号码强身份锚（照片检出型号码 ∈ 视图型号码）
    private const double WQrDecode  = 5.0;   // QR 存在/解码命中视图 QR（身份锚）
    private const double WTextMatch = 2.0;   // 每个命中的"区别性"固定文本
    private const double WQrCount   = 1.5;   // 照片有码且视图有 QR mark
    private const double LeadRatio  = 1.6;   // Top1 须 ≥ LeadRatio × Top2 才自动选
    private const double MinAutoScore = 4.0; // 至少 2 条文字命中，或 1 个强身份锦点

    public sealed class ViewCandidate
    {
        public MarkView View;
        public double Score;
        public string Reason;
        public int StrongEvidence;
        public ViewCandidate(MarkView view, double score, string reason, int strongEvidence)
        {
            View = view; Score = score; Reason = reason; StrongEvidence = strongEvidence;
        }
    }

    public sealed class BlockMatchResult
    {
        public MarkView? Chosen;
        public List<ViewCandidate> Candidates = new();
    }

    public static BlockMatchResult InferView(IReadOnlyList<DrawingMark> marks, JsonElement? observeDoc)
    {
        var sig = BuildViewSignatures(marks);
        if (sig.Count == 0) return new BlockMatchResult();   // 无视图签名 → 无法推断

        var photo = ExtractPhotoFeatures(observeDoc);
        var scored = new List<ViewCandidate>();
        foreach (var kv in sig)
        {
            var (score, reasons, strongEvidence) = ScoreView(kv.Value, photo);
            if (score > 0)
                scored.Add(new ViewCandidate(kv.Key, Math.Round(score, 2), string.Join("; ", reasons), strongEvidence));
        }
        scored.Sort((a, b) => b.Score.CompareTo(a.Score));

        var result = new BlockMatchResult { Candidates = scored.Take(3).ToList() };
        var top = scored.FirstOrDefault();
        var hasStrongTop = top is not null && top.StrongEvidence > 0 && top.Score >= MinAutoScore;
        if (hasStrongTop && scored.Count == 1)
            result.Chosen = top!.View;
        else if (hasStrongTop && scored.Count >= 2 && top!.Score >= LeadRatio * scored[1].Score)
            result.Chosen = top.View;
        // 不足以证明部位时 Chosen=null，由 H5 让用户选择，不降级整页猜测。
        return result;
    }

    // ---------------- 视图特征签名（从 marks 聚合）----------------

    private sealed class ViewSig
    {
        public HashSet<string> Texts = new();        // 清洗后的固定文本（Text mark）
        public HashSet<string> ModelCodes = new();   // 型号码（Text mark 中 ModelCodeLike）
        public HashSet<string> QrExpected = new();   // QR mark 已录入的预期码（#18 expected-text）
        public int QrCount;
        public int IconCount;
        public double Cx, Cy;                         // marks 质心（norm 空间，累加）
        public int MarkN;
    }

    private static Dictionary<MarkView, ViewSig> BuildViewSignatures(IReadOnlyList<DrawingMark> marks)
    {
        var dict = new Dictionary<MarkView, ViewSig>();
        foreach (var m in Flatten(marks))
        {
            if (m.View == MarkView.Unspecified) continue;
            if (!dict.TryGetValue(m.View, out var s)) dict[m.View] = s = new ViewSig();
            s.MarkN++;
            if (m.NormBbox is { Length: >= 4 })
            {
                s.Cx += m.NormBbox[0] + m.NormBbox[2] / 2.0;
                s.Cy += m.NormBbox[1] + m.NormBbox[3] / 2.0;
            }
            if (m.Type == MarkType.Qr)
            {
                s.QrCount++;
                if (!string.IsNullOrEmpty(m.Text)) s.QrExpected.Add(Clean(m.Text));
            }
            else if (m.Type == MarkType.Icon)
                s.IconCount++;
            else if (!string.IsNullOrEmpty(m.Text))
            {
                var t = Clean(m.Text);
                if (t.Length >= 2) s.Texts.Add(t);
                if (ModelCodeLike(m.Text)) s.ModelCodes.Add(t);
            }
        }
        foreach (var s in dict.Values)
            if (s.MarkN > 0) { s.Cx /= s.MarkN; s.Cy /= s.MarkN; }
        return dict;
    }

    private static IEnumerable<DrawingMark> Flatten(IEnumerable<DrawingMark> marks)
    {
        foreach (var m in marks)
        {
            if (m.Type != MarkType.Group) yield return m;
            foreach (var child in Flatten(m.Children ?? new List<DrawingMark>()))
                yield return child;
        }
    }

    // ---------------- 照片侧特征（observeDoc）----------------

    private sealed class PhotoFeatures
    {
        public HashSet<string> Texts = new();
        public HashSet<string> ModelCodes = new();
        public HashSet<string> QrData = new();
        public int QrCount;
    }

    private static PhotoFeatures ExtractPhotoFeatures(JsonElement? observeDoc)
    {
        var pf = new PhotoFeatures();
        if (observeDoc is not { ValueKind: JsonValueKind.Object } obs) return pf;

        if (obs.TryGetProperty("texts", out var bts) && bts.ValueKind == JsonValueKind.Array)
        {
            foreach (var t in bts.EnumerateArray())
            {
                if (t.ValueKind != JsonValueKind.Object) continue;
                if (!t.TryGetProperty("text", out var tx) || tx.ValueKind != JsonValueKind.String) continue;
                var text = tx.GetString() ?? "";
                if (text.Length == 0) continue;
                var c = Clean(text);
                if (c.Length >= 2) pf.Texts.Add(c);
                if (ModelCodeLike(text)) pf.ModelCodes.Add(c);
            }
        }
        if (obs.TryGetProperty("codes", out var codes) && codes.ValueKind == JsonValueKind.Array)
        {
            foreach (var cc in codes.EnumerateArray())
            {
                if (cc.ValueKind != JsonValueKind.Object) continue;
                pf.QrCount++;
                if (cc.TryGetProperty("data", out var d) && d.ValueKind == JsonValueKind.String)
                {
                    var data = d.GetString() ?? "";
                    if (data.Length > 0) pf.QrData.Add(data);
                }
            }
        }
        return pf;
    }

    // ---------------- 打分 ----------------

    private static (double, List<string>, int) ScoreView(ViewSig s, PhotoFeatures pf)
    {
        double score = 0;
        var reasons = new List<string>();
        var strongEvidence = 0;

        // 1) 型号码强身份锚
        foreach (var mc in pf.ModelCodes)
            if (s.ModelCodes.Contains(mc)) { score += WModelCode; strongEvidence++; reasons.Add("型号码:" + mc); }

        // 2) QR 存在 / 解码命中（身份锚，对应旧 identityViews 任一 QR mark → 锚该视图）
        if (pf.QrCount > 0 && s.QrCount > 0) { score += WQrCount; reasons.Add("QR存在"); }
        // 【R5 回正·#35】QR 解码内容**不得**作为视图判定的强证据：
        // 设计 §7 明定「QR 不解码，只验存在+位置」；且图纸侧 25 条 Qr mark 的 text 实测全部为 NULL
        // （PDF 上是矢量示意图，根本没有可比对内容）。
        // 即便人工通过 SetMarkExpectedText 录入了预期内容，也**只作弱排序参考**：
        // 不加分、不计入 strongEvidence —— 否则会绕过「位置才是判据」的原则。
        foreach (var qd in pf.QrData)
            if (s.QrExpected.Contains(Clean(qd))) { reasons.Add("QR内容参考(设计§7：不计分)"); break; }

        // 3) 固定文本命中（清洗后子串语义，复刻旧 InferViewFromBlind 的双向包含匹配）
        var textHits = 0;
        foreach (var vt in s.Texts)
        {
            bool hit = false;
            foreach (var pt in pf.Texts)
            {
                if (pt == vt || vt.Contains(pt) || (vt.Length > 2 && pt.Contains(vt))) { hit = true; break; }
            }
            if (hit) { score += WTextMatch; textHits++; reasons.Add("文本:" + vt); }
        }
        if (textHits >= 2) strongEvidence++;

        // 图纸坐标是整页坐标，照片通常是局部特写，两者质心不在同一坐标系，不参与视图选择。
        return (score, reasons, strongEvidence);
    }

    // ---------------- 工具 ----------------

    /// <summary>清洗：转小写、仅留字母数字（去空格/标点/全角），用于稳健文本比对。</summary>
    private static string Clean(string s)
    {
        var sb = new System.Text.StringBuilder(s.Length);
        foreach (var ch in s.ToLowerInvariant())
            if (char.IsLetterOrDigit(ch)) sb.Append(ch);
        return sb.ToString();
    }

    /// <summary>是否像产品型号代码（字母+数字混合、长度≥6），用于强身份锚识别（同 MarkVerifier.ModelCodeLike）。</summary>
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
}
