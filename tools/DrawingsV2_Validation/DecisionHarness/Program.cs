using System.Text.Json;
using Platform.Modules.DrawingsV2.Decision;

static DrawingMark TextMark(string id, MarkView view, string text) => new()
{
    Id = id, View = view, Type = MarkType.Text, Text = text,
    NormBbox = new[] { 0.1, 0.1, 0.2, 0.05 }
};

static DrawingMark QrMark(string id, MarkView view) => new()
{
    Id = id, View = view, Type = MarkType.Qr,
    NormBbox = new[] { 0.2, 0.2, 0.1, 0.1 }
};

static JsonDocument Observation(string[] texts, params string?[] codes)
{
    var body = new
    {
        source = new { width = 1000, height = 1000 },
        texts = texts.Select((t, i) => new { text = t, bbox = new[] { 10, 10 + i * 20, 200, 25 + i * 20 } }),
        codes = codes.Select(c => new { data = c, bbox = new[] { 20, 20, 80, 80 } })
    };
    return JsonDocument.Parse(JsonSerializer.Serialize(body));
}

static void Assert(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}

static IEnumerable<DrawingMark> Leaves(IEnumerable<DrawingMark> source)
{
    foreach (var mark in source)
    {
        if (mark.Type == MarkType.Group)
        {
            foreach (var child in Leaves(mark.Children ?? new List<DrawingMark>()))
                yield return child;
        }
        else
        {
            yield return mark;
        }
    }
}

var marks = new List<DrawingMark>
{
    TextMark("top-model", MarkView.TopCover, "PC-P1HJQ"),
    TextMark("top-service", MarkView.TopCover, "服务热线4008601111"),
    TextMark("side-service", MarkView.Side, "服务热线4008601111"),
    QrMark("side-qr", MarkView.Side)
};

using (var obs = Observation(new[] { "PC-P1HJQ" }))
{
    var result = BlockMatcher.InferView(marks, obs.RootElement);
    Assert(result.Chosen == MarkView.TopCover, "型号强证据应选中 TopCover");
}

using (var obs = Observation(new[] { "服务热线4008601111" }))
{
    var result = BlockMatcher.InferView(marks, obs.RootElement);
    Assert(result.Chosen is null, "通用文字同时出现在多视图时不应自动选面");
    Assert(result.Candidates.Count == 2, "应向 H5 返回两个候选");
}

using (var obs = Observation(Array.Empty<string>(), (string?)null))
{
    var result = BlockMatcher.InferView(marks, obs.RootElement);
    Assert(result.Chosen is null, "未解码 QR 的存在性不应单独确定部位");
    Assert(result.Candidates.Any(c => c.View == MarkView.Side), "QR 存在性仍应用于候选排序");
}

var grouped = new DrawingMark
{
    Id = "group", View = MarkView.TopCover, Type = MarkType.Group,
    Children = new List<DrawingMark>
    {
        TextMark("child-1", MarkView.TopCover, "LOW VOLTAGE"),
        TextMark("child-2", MarkView.TopCover, "禁止强电")
    }
};
using (var obs = Observation(new[] { "LOW VOLTAGE", "禁止强电" }))
{
    var result = BlockMatcher.InferView(new[] { grouped }, obs.RootElement);
    Assert(result.Chosen == MarkView.TopCover, "Group 子项应参与视图签名建立");
}

// 说明文字（例如“二维码格式：”）不能成为整张视图的裁剪框。
// ViewBboxOf 应忽略过小的标签框，回退到同视图实际 mark 的并集。
var tinyLabelA = new DrawingMark
{
    Id = "tiny-a", View = MarkView.Side, Type = MarkType.Text, Text = "A",
    NormBbox = new[] { 0.20, 0.30, 0.10, 0.08 },
    ViewBbox = new[] { 0.50, 0.50, 0.01, 0.01 }
};
var tinyLabelB = new DrawingMark
{
    Id = "tiny-b", View = MarkView.Side, Type = MarkType.Qr,
    NormBbox = new[] { 0.70, 0.60, 0.12, 0.12 },
    ViewBbox = tinyLabelA.ViewBbox
};
var safeView = MarkVerifier.ViewBboxOf(new[] { tinyLabelA, tinyLabelB });
Assert(safeView is { Length: >= 4 } && safeView[0] < 0.21 && safeView[2] > 0.60,
    "过小的视图标签框不应压缩视图范围，应回退到实际 mark 并集");

Console.WriteLine("DecisionHarness: 5 tests passed");

foreach (var path in args)
{
    var doc = VectorPdfParser.ParseFile(path);
    var result = MarkBuilder.Build(doc, Path.GetFileNameWithoutExtension(path));
    var leaves = Leaves(result.Marks).ToList();
    Console.WriteLine($"fixture={Path.GetFileName(path)} marks={result.Marks.Count} leaves={leaves.Count} warnings={result.Warnings.Count}");
    foreach (var mark in result.Marks)
        Console.WriteLine($"  {mark.Id} {mark.Type} {mark.View} text={mark.Text ?? "<none>"} " +
                          $"norm={JsonSerializer.Serialize(mark.NormBbox)} view={JsonSerializer.Serialize(mark.ViewBbox)} " +
                          $"conf={mark.Confidence:0.##} rule={mark.Source?.RuleId ?? "-"} " +
                          $"kids={(mark.Children?.Count ?? 0)} vf={result.VisionFallbackRequired} " +
                          $"scopes=[{string.Join(",", result.Scopes)}]");
    foreach (var w in result.Warnings) Console.WriteLine($"    warn: {w}");

    var modelMark = leaves.FirstOrDefault(m => m.Text is not null && m.Text.StartsWith("PC-", StringComparison.Ordinal));
    Assert(modelMark is not null, $"{path}: 应从实际打标块提取型号");
    if (modelMark!.View != MarkView.Unspecified)
    {
        using var modelObs = Observation(new[] { modelMark.Text! });
        Assert(BlockMatcher.InferView(result.Marks, modelObs.RootElement).Chosen == modelMark.View,
            $"{path}: 型号应强匹配到其所属视图");
    }
    Assert(leaves.Count(m => m.Type == MarkType.Qr) == 1,
        $"{path}: 二维码格式说明图不应作为产品必检二维码");
}
