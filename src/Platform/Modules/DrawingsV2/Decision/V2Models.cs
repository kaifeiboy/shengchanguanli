using System.Text.Json.Serialization;

namespace Platform.Modules.DrawingsV2.Decision;

/*
 * v2 决策层模型。
 *
 * 【边界】
 * 这里只有两类中性数据：
 *   1. VpdfXxx  —— 与 Python 感知层 vpdf/1 契约一一对应的传输对象（不含任何业务判定）
 *   2. DrawingMark / MarkSource —— 决策层产出的「应打标对象」（C# 判定，非 Python）
 * Python 永远不参与「哪些是打标对象」「合格与否」的判定。
 *
 * 【为什么不用旧 drawing_blocks】
 * 旧模型是「PNG 切图 + RawText」，语义丢失、不可追溯、不可单测。
 * v2 的业务数据单元是「应打标对象」，渲染产物降级为可重建的展示资源。
 */

// ---------------- vpdf/1 契约对象（与 Python 端字段一一对应） ----------------

public sealed class VpdfDocument
{
    [JsonPropertyName("schema")] public string? Schema { get; set; }
    [JsonPropertyName("generator")] public VpdfGenerator? Generator { get; set; }
    [JsonPropertyName("source")] public VpdfSource? Source { get; set; }
    [JsonPropertyName("pages")] public List<VpdfPage>? Pages { get; set; }
    [JsonPropertyName("diagnostics")] public VpdfDocDiagnostics? Diagnostics { get; set; }
}

public sealed class VpdfGenerator
{
    [JsonPropertyName("lib")] public string? Lib { get; set; }
    [JsonPropertyName("version")] public string? Version { get; set; }
    [JsonPropertyName("pymupdf")] public string? PyMuPdf { get; set; }
}

public sealed class VpdfSource
{
    [JsonPropertyName("file")] public string? File { get; set; }
    [JsonPropertyName("file_name")] public string? FileName { get; set; }
    [JsonPropertyName("sha256")] public string? Sha256 { get; set; }
    [JsonPropertyName("page_count")] public int PageCount { get; set; }
    [JsonPropertyName("format")] public string? Format { get; set; }
    [JsonPropertyName("creator")] public string? Creator { get; set; }
    [JsonPropertyName("producer")] public string? Producer { get; set; }
    [JsonPropertyName("title")] public string? Title { get; set; }
}

public sealed class VpdfPage
{
    [JsonPropertyName("index")] public int Index { get; set; }
    [JsonPropertyName("canonical_size_pt")] public double[]? CanonicalSizePt { get; set; }
    [JsonPropertyName("orientation")] public string? Orientation { get; set; }
    [JsonPropertyName("aspect")] public double Aspect { get; set; }
    [JsonPropertyName("page_rotation")] public int PageRotation { get; set; }
    [JsonPropertyName("text_layer")] public VpdfTextLayer? TextLayer { get; set; }
    [JsonPropertyName("text_spans")] public List<VpdfSpan>? TextSpans { get; set; }
    [JsonPropertyName("images")] public List<VpdfImage>? Images { get; set; }
    [JsonPropertyName("graphics")] public VpdfGraphics? Graphics { get; set; }
    [JsonPropertyName("diagnostics")] public VpdfPageDiagnostics? Diagnostics { get; set; }
}

public sealed class VpdfTextLayer
{
    [JsonPropertyName("present")] public bool Present { get; set; }
    [JsonPropertyName("word_count")] public int WordCount { get; set; }
    [JsonPropertyName("text_block_count")] public int TextBlockCount { get; set; }
    [JsonPropertyName("span_count")] public int SpanCount { get; set; }
}

public sealed class VpdfSpan
{
    [JsonPropertyName("id")] public string? Id { get; set; }
    [JsonPropertyName("text")] public string? Text { get; set; }
    [JsonPropertyName("bbox")] public double[]? Bbox { get; set; }
    [JsonPropertyName("norm_bbox")] public double[]? NormBbox { get; set; }
    [JsonPropertyName("font")] public string? Font { get; set; }
    [JsonPropertyName("size")] public double Size { get; set; }
    [JsonPropertyName("dir")] public double[]? Dir { get; set; }
    [JsonPropertyName("angle")] public double Angle { get; set; }
    [JsonPropertyName("color")] public long? Color { get; set; }
    [JsonPropertyName("flags")] public int? Flags { get; set; }
    [JsonPropertyName("block_index")] public int BlockIndex { get; set; }
}

public sealed class VpdfImage
{
    [JsonPropertyName("id")] public string? Id { get; set; }
    [JsonPropertyName("xref")] public int? Xref { get; set; }
    [JsonPropertyName("bbox")] public double[]? Bbox { get; set; }
    [JsonPropertyName("norm_bbox")] public double[]? NormBbox { get; set; }
    [JsonPropertyName("width")] public int? Width { get; set; }
    [JsonPropertyName("height")] public int? Height { get; set; }
    [JsonPropertyName("colorspace_n")] public int? ColorspaceN { get; set; }
    [JsonPropertyName("bpc")] public int? Bpc { get; set; }
    [JsonPropertyName("has_alpha")] public bool HasAlpha { get; set; }
}

public sealed class VpdfGraphics
{
    [JsonPropertyName("count")] public int Count { get; set; }
    [JsonPropertyName("total_items")] public int TotalItems { get; set; }
    [JsonPropertyName("truncated")] public bool Truncated { get; set; }
    [JsonPropertyName("parse_ms")] public double ParseMs { get; set; }
    [JsonPropertyName("paths")] public List<VpdfPath>? Paths { get; set; }
}

public sealed class VpdfPath
{
    [JsonPropertyName("id")] public string? Id { get; set; }
    [JsonPropertyName("bbox")] public double[]? Bbox { get; set; }
    [JsonPropertyName("norm_bbox")] public double[]? NormBbox { get; set; }
    [JsonPropertyName("type")] public string? Type { get; set; }
    [JsonPropertyName("n_items")] public int NItems { get; set; }
    [JsonPropertyName("color")] public string? Color { get; set; }
    [JsonPropertyName("fill")] public string? Fill { get; set; }
    [JsonPropertyName("width")] public double? Width { get; set; }
    [JsonPropertyName("closed")] public bool Closed { get; set; }
}

public sealed class VpdfPageDiagnostics
{
    [JsonPropertyName("source")] public string? Source { get; set; }
    [JsonPropertyName("vision_fallback_required")] public bool VisionFallbackRequired { get; set; }
    [JsonPropertyName("parse_ms")] public double ParseMs { get; set; }
}

public sealed class VpdfDocDiagnostics
{
    [JsonPropertyName("with_graphics")] public bool WithGraphics { get; set; }
    [JsonPropertyName("total_ms")] public double TotalMs { get; set; }
}

// ---------------- 决策层领域模型 ----------------

/// <summary>应打标对象的类型（方案 §2）。</summary>
public enum MarkType
{
    Text,   // 文字内容：型号 / MAC / 服务热线 / 警示语 / 编码
    Qr,     // 二维码：只验存在性与位置，不解码内容
    Icon,   // 图标：只验存在性与位置
    Group   // 组合：多个子 mark 构成的整体（如「编码块」）
}

/// <summary>八态结果枚举（方案 §8）。判定发生在比对阶段，此处仅定义。</summary>
public enum MarkState
{
    Matched,          // 一致（绿）
    Missing,          // 缺标（红）
    Wrong,            // 错标（红）
    Extra,            // 多标（黄）
    LowConfidence,    // 低置信（黄）
    NotDetected,      // 未检出（黄，实物可读但该项未 OCR 到，需人工复核，非确认缺标）
    NotApplicable,    // 不适用（灰）
    NotComparable,    // 不可比（灰）
    ProcessingError   // 处理异常（灰）
}

/// <summary>打标对象所处视图/部位。</summary>
public enum MarkView
{
    Unspecified,
    TopCover,     // 上盖 / 面板
    BottomCover,  // 下盖 / 底壳
    Side,         // 侧面
    Nameplate,    // 铭牌
    Cable,        // 接线 / 线材
    Other
}

/// <summary>
/// 应打标对象 —— v2 的核心业务数据单元。
/// 每条都必须带 <see cref="Source"/>，说明它是依据哪条规则、从哪些 span/image 得出的，保证可追溯。
/// </summary>
public sealed class DrawingMark
{
    public string Id { get; set; } = "";          // 稳定 id：{drawingKey}#{seq}
    public MarkType Type { get; set; }
    public MarkView View { get; set; } = MarkView.Unspecified;

    /// <summary>标称文本。Qr/Icon 为 null（方案：二维码与图标只验存在性与位置）。</summary>
    public string? Text { get; set; }

    /// <summary>是否必须打标（由条款或标注强制性决定）。</summary>
    public bool Required { get; set; } = true;

    /// <summary>适用条件（如「按实际制造编码打印」），无则 null。</summary>
    public string? Condition { get; set; }

    /// <summary>归一化位置 [x, y, w, h] ∈ [0,1]（相对标准坐标系）。</summary>
    public double[]? NormBbox { get; set; }

    /// <summary>原始 pt 位置（与 NormBbox 互为校验）。</summary>
    public double[]? Bbox { get; set; }

    /// <summary>
    /// 视图标签文字的 NormBbox（M6 修正·视图词范围）。
    /// <para>InferView 在页面上找到的、标注此 mark 所属视图的**视图标签文字块**
    /// （如「上盖视图」「下盖」）的 NormBbox。用于视图级配准时替代 marks 的 UnionBbox
    /// 作为 viewBbox —— 视图标签是页面上的固定锚点，比散乱 marks 的并集更稳定。</para>
    /// <para>null 表示该 mark 无视图标签（View == Unspecified 或 InferView 未找到视图标签块）。
    /// 向后兼容：老档案反序列化时无此字段 → null → 走 marks UnionBbox 回退。</para>
    /// </summary>
    public double[]? ViewBbox { get; set; }

    /// <summary>文字方向角（度）。</summary>
    public double Direction { get; set; }

    /// <summary>置信度 0~1。</summary>
    public double Confidence { get; set; }

    public MarkSource Source { get; set; } = new();

    /// <summary>子对象（Type=Group 时有值）。</summary>
    public List<DrawingMark>? Children { get; set; }
}

/// <summary>打标对象的来源证据 —— 可追溯性硬要求。</summary>
public sealed class MarkSource
{
    /// <summary>来源通道：pdf_vector（原生矢量）/ vision_fallback（视觉兜底）/ manual（人工确认）。</summary>
    public string Kind { get; set; } = "pdf_vector";

    public int PageIndex { get; set; }

    /// <summary>命中的规则 id（见 MarkRules）。</summary>
    public string? RuleId { get; set; }

    /// <summary>依据的文字 span id 列表。</summary>
    public List<string> SpanIds { get; set; } = new();

    /// <summary>依据的图像对象 id 列表。</summary>
    public List<string> ImageIds { get; set; } = new();

    /// <summary>人类可读的判定依据（审计用）。</summary>
    public string? Evidence { get; set; }
}

/// <summary>决策层异常。与业务数据无关，用于让调用方区分「解析失败」与「正常无结果」。</summary>
public sealed class V2Exception : Exception
{
    public V2Exception(string message) : base(message) { }
}
