using System.Text.Json;
using System.Text.Json.Serialization;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// vpdf/1 契约解析器：把 Python 感知层输出的中性 JSON 变成强类型对象。
///
/// 【为什么必须校验 schema】
/// 感知层与决策层是独立演进的两个进程。若 Python 端升级为 vpdf/2 而 C# 端未同步，
/// 静默按旧字段解析会产生「看起来正常但语义已错」的数据 —— 这正是旧系统最难排查的一类故障。
/// 因此这里对 schema 与必需字段做硬校验，不匹配直接抛 <see cref="V2Exception"/>，
/// 由上层转成 processing_error，而不是产出一份错误的打标清单。
/// </summary>
public static class VectorPdfParser
{
    public const string SupportedSchema = "vpdf/1";
    public const string SupportedFallbackSchema = "vpdf-fallback/1";

    private static readonly JsonSerializerOptions Opts = new()
    {
        PropertyNameCaseInsensitive = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
        NumberHandling = JsonNumberHandling.AllowReadingFromString
    };

    /// <summary>解析 vpdf JSON 文本。</summary>
    /// <exception cref="V2Exception">schema 不匹配或结构不完整。</exception>
    public static VpdfDocument Parse(string json)
    {
        if (string.IsNullOrWhiteSpace(json))
            throw new V2Exception("vpdf 输出为空");

        VpdfDocument? doc;
        try
        {
            doc = JsonSerializer.Deserialize<VpdfDocument>(json, Opts);
        }
        catch (JsonException e)
        {
            throw new V2Exception("vpdf 输出不是合法 JSON：" + e.Message);
        }

        if (doc is null) throw new V2Exception("vpdf 输出解析为空对象");

        var schema = doc.Schema ?? "";
        if (!string.Equals(schema, SupportedSchema, StringComparison.Ordinal))
            throw new V2Exception($"vpdf schema 不匹配：期望 {SupportedSchema}，实际 {schema}");

        if (doc.Pages is null || doc.Pages.Count == 0)
            throw new V2Exception("vpdf 输出不含任何页面");

        return doc;
    }

    /// <summary>从文件解析（离线批量与人工复核用）。</summary>
    public static VpdfDocument ParseFile(string path)
    {
        if (!File.Exists(path)) throw new V2Exception("vpdf 文件不存在：" + path);
        return Parse(File.ReadAllText(path));
    }

    /// <summary>
    /// 解析 vpdf-fallback/1 视觉兜底输出。
    ///
    /// 与 <see cref="Parse"/> 同样的硬校验逻辑：感知层与决策层独立演进，
    /// schema 不匹配必须显式报错，不允许静默按旧字段解析出「看起来正常但语义已错」的结果。
    /// </summary>
    /// <exception cref="V2Exception">schema 不匹配或结构不完整。</exception>
    public static VisionFallbackResult ParseFallback(string json)
    {
        if (string.IsNullOrWhiteSpace(json))
            throw new V2Exception("视觉兜底输出为空");

        VisionFallbackResult? r;
        try
        {
            r = JsonSerializer.Deserialize<VisionFallbackResult>(json, Opts);
        }
        catch (JsonException e)
        {
            throw new V2Exception("视觉兜底输出不是合法 JSON：" + e.Message);
        }

        if (r is null) throw new V2Exception("视觉兜底输出解析为空对象");

        var schema = r.Schema ?? "";
        if (!string.Equals(schema, SupportedFallbackSchema, StringComparison.Ordinal))
            throw new V2Exception($"视觉兜底 schema 不匹配：期望 {SupportedFallbackSchema}，实际 {schema}");

        if (r.Page is null) throw new V2Exception("视觉兜底输出不含 page 段");

        return r;
    }

    /// <summary>取页面标准坐标系宽高（pt）。缺失或非法时抛异常 —— 坐标是所有下游判定的基础。</summary>
    public static (double W, double H) PageSize(VpdfPage page)
    {
        var s = page.CanonicalSizePt;
        if (s is null || s.Length < 2 || s[0] <= 0 || s[1] <= 0)
            throw new V2Exception($"页面 {page.Index} 缺少合法 canonical_size_pt");
        return (s[0], s[1]);
    }
}
