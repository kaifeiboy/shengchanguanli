using System.IO;
using System.Text.RegularExpressions;

namespace Platform.Modules.Drawings;

/// <summary>
/// 型号 / 部位 解析工具。
/// 关键经验：源图纸「文件名自带型号」，所以建库以文件名提取最稳；
/// OCR 仅作为补充 + 用于识别上传照片里的型号与部位。
/// 部位匹配：图纸按关键词切分为多个「部位片段」，照片 OCR 后与片段比对。
/// 中文 OCR 常有单字误识（如 盖→盎），故关键词命中采用「编辑距离≤1」模糊匹配。
/// </summary>
public static class ModelParser
{
    /// <summary>部位关键词 -> 规范部位名。顺序即优先级。</summary>
    private static readonly (string[] Keys, string Name)[] PartRules =
    {
        (new[]{"顶部","顶端","上表面"},            "顶部"),
        (new[]{"底部","底端","下表面"},            "底部"),
        (new[]{"背面"},                          "背面"),
        (new[]{"正面"},                          "正面"),
        (new[]{"左侧","左边"},                  "左侧"),
        (new[]{"右侧","右边"},                  "右侧"),
        (new[]{"侧面"},                        "侧面"),
        (new[]{"上盖","上蓋"},                  "上盖"),
        (new[]{"下盖","下蓋"},                  "下盖"),
        (new[]{"上端"},                        "上端"),
        (new[]{"下端"},                        "下端"),
    };

    /// <summary>去除所有空白（OCR 常把中文拆成单字带空格），便于关键词/片段匹配。</summary>
    public static string Normalize(string s)
        => s is null ? "" : Regex.Replace(s, @"\s+", "");

    /// <summary>
    /// 模糊命中：文本是否包含关键词，或存在「首字相同、且整词编辑距离≤1」的近似片段
    /// （容忍中文 OCR 的单字误识，如 盖→盎）。
    /// </summary>
    public static bool ContainsFuzzy(string text, string keyword)
    {
        if (string.IsNullOrEmpty(text) || string.IsNullOrEmpty(keyword)) return false;
        if (text.Contains(keyword)) return true;
        if (keyword.Length < 2) return false;

        int i = text.IndexOf(keyword[0]);
        while (i >= 0)
        {
            int len = Math.Min(keyword.Length, text.Length - i);
            if (len == keyword.Length && Levenshtein(text.Substring(i, len), keyword) <= 1)
                return true;
            i = text.IndexOf(keyword[0], i + 1);
        }
        return false;
    }

    /// <summary>返回关键词在文本中首次模糊命中的位置，未命中返回 -1。</summary>
    private static int IndexOfFuzzy(string text, string keyword)
    {
        if (string.IsNullOrEmpty(text) || string.IsNullOrEmpty(keyword)) return -1;
        if (keyword.Length < 2) return text.IndexOf(keyword, StringComparison.Ordinal);

        int i = text.IndexOf(keyword[0]);
        while (i >= 0)
        {
            int len = Math.Min(keyword.Length, text.Length - i);
            if (len == keyword.Length && Levenshtein(text.Substring(i, len), keyword) <= 1)
                return i;
            i = text.IndexOf(keyword[0], i + 1);
        }
        return -1;
    }

    /// <summary>
    /// 从文件名提取型号。例：
    /// "02-4K3GR线控器效果图-Model.pdf" -> "4K3GR"
    /// "02-HYXC-VF01效果图-Model.pdf"     -> "HYXC-VF01"
    /// 提取不到时回退为「去掉后缀的文件名」，保证仍可搜索。
    /// </summary>
    public static string ExtractModel(string fileName)
    {
        var baseName = Path.GetFileNameWithoutExtension(fileName);

        // 去掉尾部中文描述与 -Model
        var cleaned = Regex.Replace(baseName,
            @"(效果图|图纸|打标|-?Model|\(.*?\)|\s)+$",
            "", RegexOptions.IgnoreCase);

        // 去掉前导数字编号，如 "02-"
        cleaned = Regex.Replace(cleaned, @"^\d+[\-_]", "");

        // 取首个字母/数字组成的型号片段
        var m = Regex.Match(cleaned, @"[A-Za-z0-9][A-Za-z0-9\-]{1,30}");
        if (m.Success) return m.Value.Trim('-');

        return cleaned.Trim();
    }

    /// <summary>从 OCR 文本中识别型号片段（图纸上印的型号）</summary>
    public static string? ExtractModelFromText(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        var m = Regex.Match(text, @"[A-Za-z]{1,4}[\-_][A-Za-z0-9]{1,6}|[A-Za-z0-9]{3,12}");
        return m.Success ? m.Value : null;
    }

    /// <summary>
    /// 从（已归一化的）文本识别部位关键词（模糊），返回规范部位名或 null。
    /// </summary>
    public static string? DetectPart(string normalizedText)
    {
        if (string.IsNullOrWhiteSpace(normalizedText)) return null;
        foreach (var rule in PartRules)
            foreach (var k in rule.Keys)
                if (ContainsFuzzy(normalizedText, k))
                    return rule.Name;
        return null;
    }

    // ---------- 符号 / 编码 提取 ----------

    /// <summary>从原始 OCR 文本中提取所有「编码 / 符号」候选串。</summary>
    public static List<string> ExtractCodes(string rawText)
    {
        var results = new List<string>();
        if (string.IsNullOrWhiteSpace(rawText)) return results;

        var text = rawText;

        // 1) 图纸编号类：如 JT-191-A-0002、A/0 等（字母+数字+连字符）
        foreach (Match m in Regex.Matches(text, @"[A-Za-z][\w\-]*[\-][\w\-]+"))
            AddIfNew(results, m.Value.Trim());

        // 2) 纯数字长序列（≥4 位）：日期码、数量、编号等
        foreach (Match m in Regex.Matches(text, @"\d{4,}"))
            AddIfNew(results, m.Value);

        // 3) 字母数字混合码（5~25 位，含型号/批号等）
        foreach (Match m in Regex.Matches(text, @"[A-Za-z0-9]{5,25}"))
        {
            var v = m.Value.Trim();
            // 过滤纯中文上下文的噪声（如连续中文间夹的少量英文）
            if (!IsMostlyChinese(v))
                AddIfNew(results, v);
        }

        // 4) 日期格式：YYYY.MM.DD / YYYY-MM-DD / YY.MM.DD 等
        foreach (Match m in Regex.Matches(text, @"\d{2,4}[.\-/]\d{1,2}[.\-/]\d{1,2}"))
            AddIfNew(results, m.Value);

        return results;
    }

    /// <summary>两份代码列表的匹配得分（0~1）。任一 photoCode 与任一 partCode 相似即加分。</summary>
    public static double CodeMatchScore(List<string> photoCodes, List<string> partCodes)
    {
        if (photoCodes.Count == 0 || partCodes.Count == 0) return 0;

        int hits = 0;
        foreach (var pc in photoCodes)
        {
            bool hit = false;
            foreach (var ptc in partCodes)
            {
                // 完全相等 或 一方包含另一方
                if (pc.Equals(ptc, StringComparison.OrdinalIgnoreCase) ||
                    pc.Contains(ptc, StringComparison.OrdinalIgnoreCase) ||
                    ptc.Contains(pc, StringComparison.OrdinalIgnoreCase))
                { hit = true; break; }
                // 编辑距离 ≤2 的短串也算近似命中
                if (pc.Length <= 12 && Math.Abs(pc.Length - ptc.Length) <= 3 &&
                    Levenshtein(pc.ToUpperInvariant(), ptc.ToUpperInvariant()) <= 2)
                { hit = true; break; }
            }
            if (hit) hits++;
        }

        return photoCodes.Count > 0 ? (double)hits / photoCodes.Count : 0;
    }

    /// <summary>
    /// 将图纸 OCR 文本切分为多个「部位片段」。
    /// 命中关键词则取关键词前后一段作为该部位的内容指纹；
    /// 完全没命中则返回单个「整图」片段（内容=全文），保证仍可匹配。
    /// </summary>
    public static List<(string Name, string Content)> SegmentParts(string rawText)
    {
        var text = Normalize(rawText);
        var result = new List<(string, string)>();
        if (string.IsNullOrWhiteSpace(text)) return result;

        foreach (var rule in PartRules)
        {
            foreach (var k in rule.Keys)
            {
                var idx = IndexOfFuzzy(text, k);
                if (idx < 0) continue;
                var start = Math.Max(0, idx - 60);
                var end = Math.Min(text.Length, idx + k.Length + 120);
                result.Add((rule.Name, text.Substring(start, end - start)));
                break; // 同一规范名只取第一次出现
            }
        }

        if (result.Count == 0)
            result.Add(("整图", text));

        return result;
    }

    private static int Levenshtein(string a, string b)
    {
        var d = new int[a.Length + 1, b.Length + 1];
        for (int i = 0; i <= a.Length; i++) d[i, 0] = i;
        for (int j = 0; j <= b.Length; j++) d[0, j] = j;
        for (int i = 1; i <= a.Length; i++)
            for (int j = 1; j <= b.Length; j++)
            {
                var cost = a[i - 1] == b[j - 1] ? 0 : 1;
                d[i, j] = Math.Min(Math.Min(d[i - 1, j] + 1, d[i, j - 1] + 1), d[i - 1, j - 1] + cost);
            }
        return d[a.Length, b.Length];
    }

    private static void AddIfNew(List<string> list, string item)
    {
        if (!list.Exists(x => x.Equals(item, StringComparison.OrdinalIgnoreCase)))
            list.Add(item);
    }

    /// <summary>字符串是否以中文字符为主（>50% 为中文）。</summary>
    private static bool IsMostlyChinese(string s)
    {
        if (string.IsNullOrEmpty(s)) return false;
        int cn = 0;
        foreach (var c in s) if (c >= '\u4e00' && c <= '\u9fff') cn++;
        return cn * 2 > s.Length;
    }
}
