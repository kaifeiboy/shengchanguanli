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
    [JsonPropertyName("color")] public double[]? Color { get; set; }
    [JsonPropertyName("fill")] public double[]? Fill { get; set; }
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
    Qr,     // 二维码：只验存在性/外接矩形/中心位置（设计 §7）；录入预期内容仅作提示性人工标注，不影响结论（P4 回正）
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

    /// <summary>标称文本。Text/Icon 一般为 null（只验存在性与位置）；QR 可录入预期解码内容以做身份比对（B 项）。</summary>
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
    /// 视图标签文字的 NormBbox，用于在同一视图内建立局部坐标范围。
    /// <para>null 表示没有可用视图范围。老档案无此字段时按 marks 并集回退。</para>
    /// </summary>
    public double[]? ViewBbox { get; set; }

    /// <summary>文字方向角（度）。</summary>
    public double Direction { get; set; }

    /// <summary>置信度 0~1。</summary>
    public double Confidence { get; set; }

    public MarkSource Source { get; set; } = new();

    /// <summary>子对象（Type=Group 时有值）。</summary>
    public List<DrawingMark>? Children { get; set; }

    /// <summary>A（#37）：是否被排除出比对。仅针对低置信兜底链 R4/R5 的疑似污染
    /// （屏显文案 / 页脚碎片 / 二维码格式说明），正向证据链 R1/R2/R3 永不被排除。</summary>
    public bool Excluded { get; set; }

    /// <summary>A（#37）：排除原因（审计用）。</summary>
    public string? ExcludeReason { get; set; }

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

/// <summary>
/// 视觉兜底（方案 §1.8）感知结果 —— vpdf-fallback/1 契约。
///
/// 只承载「感知」：疑似转曲区域 + 局部 OCR 文本 + 坐标 + 置信度。
/// 哪些算打标对象是 MarkBuilder（决策层）的职责，本结构不做判定。
/// </summary>
public sealed class VisionFallbackResult
{
    [JsonPropertyName("schema")] public string? Schema { get; set; }
    [JsonPropertyName("page")] public VisionFallbackPage? Page { get; set; }
    [JsonPropertyName("diagnostics")] public VisionFallbackDiagnostics? Diagnostics { get; set; }
}

public sealed class VisionFallbackPage
{
    [JsonPropertyName("index")] public int Index { get; set; }
    [JsonPropertyName("regions")] public List<VisionRegion> Regions { get; set; } = new();
    [JsonPropertyName("texts")] public List<VisionText> Texts { get; set; } = new();
}

/// <summary>疑似转曲区（已排除与文字层 span 交叠的簇 —— 零误触发的结构性保证）。</summary>
public sealed class VisionRegion
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("norm_bbox")] public double[]? NormBbox { get; set; }
    [JsonPropertyName("n_chars")] public int NChars { get; set; }
    [JsonPropertyName("n_lines")] public int NLines { get; set; }
}

/// <summary>局部 OCR 读到的一条文本，坐标已是页面归一化 [x,y,w,h]。</summary>
public sealed class VisionText
{
    /// <summary>
    /// 所属页下标 —— 不在 vpdf-fallback/1 契约里（感知层一次只处理一页），
    /// 由 C# 调用方按结果的 page.index 注入，供 MarkBuilder 按页分派。
    /// </summary>
    [JsonIgnore] public int PageIndex { get; set; }

    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("region_id")] public string? RegionId { get; set; }
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("conf")] public double Conf { get; set; }
    [JsonPropertyName("dpi")] public int? Dpi { get; set; }
    [JsonPropertyName("norm_bbox")] public double[]? NormBbox { get; set; }
}

public sealed class VisionFallbackDiagnostics
{
    [JsonPropertyName("total_ms")] public double TotalMs { get; set; }
    [JsonPropertyName("geometry")] public VisionGeometryStats? Geometry { get; set; }
    [JsonPropertyName("ocr")] public VisionOcrStats? Ocr { get; set; }
}

public sealed class VisionGeometryStats
{
    [JsonPropertyName("paths_total")] public int PathsTotal { get; set; }
    [JsonPropertyName("char_like_paths")] public int CharLikePaths { get; set; }
    [JsonPropertyName("clusters")] public int Clusters { get; set; }
    [JsonPropertyName("cluster_candidates")] public int ClusterCandidates { get; set; }
    [JsonPropertyName("rejected_by_span_overlap")] public int RejectedBySpanOverlap { get; set; }
    [JsonPropertyName("regions")] public int Regions { get; set; }
}

public sealed class VisionOcrStats
{
    [JsonPropertyName("ocr_calls")] public int OcrCalls { get; set; }
    [JsonPropertyName("texts")] public int Texts { get; set; }
    [JsonPropertyName("dropped_as_span_duplicate")] public int DroppedAsSpanDuplicate { get; set; }
    [JsonPropertyName("render_total_ms")] public double RenderTotalMs { get; set; }
    [JsonPropertyName("ocr_total_ms")] public double OcrTotalMs { get; set; }
}

/// <summary>决策层异常。与业务数据无关，用于让调用方区分「解析失败」与「正常无结果」。</summary>
public sealed class V2Exception : Exception
{
    public V2Exception(string message) : base(message) { }
}
