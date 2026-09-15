using System.Text;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// 文字安全归一化 —— 方案 §6「只做安全归一化，不改变语义」。
///
/// 【三条硬规则】
///  1. **不做模糊替换**：绝不能把 "0" 归一成 "O"、把 "1" 归一成 "I"。
///     图纸上 MAC / 编码里这些字符是有区别的，归一即等于伪造一致。
///  2. 仅在「字形/排版层面」归一：全角↔半角、空白、大小写 —— 这些不改变语义。
///  3. 若两个串**只有**在把易混字符折叠后才相等，则判定为 <see cref="TextMatch.Ambiguous"/>，
///     由上层转成 low_confidence（黄），**不能**当作 matched（绿）。
///
/// 【易混字符对的来源】打标内容常见字符集：0/O、1/I/L、8/B、5/S、2/Z、6/G。
/// </summary>
public static class TextNormalizer
{
    /// <summary>
    /// 安全归一化：Unicode NFKC（全角→半角等字形归一）→ 去除所有空白 → 转大写。
    /// 不改变任何字符的语义身份（0 仍是 0，O 仍是 O）。
    /// </summary>
    public static string Normalize(string? s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var t = s.Normalize(NormalizationForm.FormKC);
        var sb = new StringBuilder(t.Length);
        foreach (var ch in t)
        {
            if (char.IsWhiteSpace(ch)) continue;      // 中文排版常见 "技 术 要 求"
            if (ch == '\u3000') continue;             // 全角空格（NFKC 已处理，保险起见）
            sb.Append(char.ToUpperInvariant(ch));
        }
        return sb.ToString();
    }

    /// <summary>松散键：归一化后只保留字母、数字与 CJK 统一表意文字，去掉所有标点。</summary>
    public static string LooseKey(string? s)
    {
        var n = Normalize(s);
        var sb = new StringBuilder(n.Length);
        foreach (var ch in n)
        {
            if (char.IsAsciiLetterOrDigit(ch)) { sb.Append(ch); continue; }
            // CJK 统一表意文字 + 扩展 A
            var cp = ch;
            if (cp >= 0x4E00 && cp <= 0x9FFF) { sb.Append(ch); continue; }
            if (cp >= 0x3400 && cp <= 0x4DBF) { sb.Append(ch); continue; }
        }
        return sb.ToString();
    }

    /// <summary>
    /// 易混折叠键：在 LooseKey 基础上把易混字符折叠到同一代表字符。
    /// **仅用于判定「是否属于易混」，绝不用于判定「相等」**。
    /// </summary>
    public static string AmbiguousKey(string? s)
    {
        var k = LooseKey(s);
        var sb = new StringBuilder(k.Length);
        foreach (var ch in k)
        {
            sb.Append(ch switch
            {
                'O' => '0',                 // O -> 0
                'I' or 'L' => '1',          // I / L -> 1
                'B' => '8',
                'S' => '5',
                'Z' => '2',
                'G' => '6',
                _ => ch
            });
        }
        return sb.ToString();
    }

    /// <summary>文本相等判定，返回匹配置信等级。</summary>
    public static TextMatch Compare(string? a, string? b)
    {
        if (a is null || b is null) return TextMatch.None;
        var ra = a.Trim();
        var rb = b.Trim();
        if (ra.Length == 0 || rb.Length == 0) return TextMatch.None;
        if (string.Equals(ra, rb, StringComparison.Ordinal)) return TextMatch.Exact;

        var na = Normalize(a);
        var nb = Normalize(b);
        if (na.Length == 0 || nb.Length == 0) return TextMatch.None;
        if (string.Equals(na, nb, StringComparison.Ordinal)) return TextMatch.Normalized;

        var la = LooseKey(a);
        var lb = LooseKey(b);
        if (la.Length > 0 && string.Equals(la, lb, StringComparison.Ordinal)) return TextMatch.Normalized;

        // 只有在折叠易混字符后才相等 —— 低置信，不是相等
        var aa = AmbiguousKey(a);
        var ab = AmbiguousKey(b);
        if (aa.Length > 0 && string.Equals(aa, ab, StringComparison.Ordinal)) return TextMatch.Ambiguous;

        return TextMatch.None;
    }

    /// <summary>
    /// 判定 haystack 是否「包含」needle，返回匹配置信等级。
    /// 用于「条款声明的打标项」在「图上实例文本」中定位。
    /// </summary>
    public static TextMatch Contains(string? haystack, string? needle)
    {
        if (string.IsNullOrEmpty(haystack) || string.IsNullOrEmpty(needle)) return TextMatch.None;

        var rh = haystack.Trim();
        var rn = needle.Trim();
        if (rh.Length == 0 || rn.Length == 0) return TextMatch.None;

        if (rh.Contains(rn, StringComparison.Ordinal)) return TextMatch.Exact;

        var nh = Normalize(haystack);
        var nn = Normalize(needle);
        if (nn.Length > 0 && nh.Contains(nn, StringComparison.Ordinal)) return TextMatch.Normalized;

        var lh = LooseKey(haystack);
        var ln = LooseKey(needle);
        if (ln.Length > 0 && lh.Contains(ln, StringComparison.Ordinal)) return TextMatch.Normalized;

        var ah = AmbiguousKey(haystack);
        var an = AmbiguousKey(needle);
        if (an.Length > 0 && ah.Contains(an, StringComparison.Ordinal)) return TextMatch.Ambiguous;

        return TextMatch.None;
    }
}

/// <summary>
/// 文本匹配置信等级。
/// Exact / Normalized → 可判 matched；Ambiguous → 只能判 low_confidence。
/// </summary>
public enum TextMatch
{
    None,
    Exact,
    Normalized,
    Ambiguous
}
