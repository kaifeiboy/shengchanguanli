using System.Text;
using System.Text.Json;
using Platform.Modules.DrawingsV2.Decision;

/*
 * v2 决策层离线验证台。
 *
 * 用法：
 *   v2harness <sweepDir> <outDir>
 *
 * 作用：把 M1 产出的 vpdf/1 JSON 喂给 C# 决策层，产出可人工审阅的打标清单。
 * 这是 M2 的验收入口 —— 在接入任何端点/H5 之前，先离线证明决策层是对的。
 */

if (args.Length < 2)
{
    Console.WriteLine("usage: v2harness <sweepDir> <outDir>");
    return 1;
}

var sweep = args[0];
var outDir = args[1];
Directory.CreateDirectory(outDir);

if (!Directory.Exists(sweep))
{
    Console.WriteLine("sweep dir not found: " + sweep);
    return 1;
}

var files = Directory.GetFiles(sweep, "*.vpdf.json")
    .Where(f => !Path.GetFileName(f).StartsWith("_"))
    .OrderBy(f => f, StringComparer.Ordinal)
    .ToList();

Console.WriteLine($"files={files.Count}");

var opts = new JsonSerializerOptions
{
    WriteIndented = true,
    Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
};

var rows = new List<Dictionary<string, object?>>();
int ok = 0, failed = 0, totalMarks = 0, childTotal = 0, declaredNoLoc = 0;
var ruleTotals = new Dictionary<string, int>();
var allWarnings = new List<string>();

// 落库验证：写独立的临时库文件，绝不触碰平台 app.db
var dbPath = Path.Combine(outDir, "_v2_verify.db");
if (File.Exists(dbPath)) File.Delete(dbPath);
var store = new V2Store(dbPath);

foreach (var f in files)
{
    var name = Path.GetFileName(f).Replace(".vpdf.json", "");
    try
    {
        var doc = VectorPdfParser.ParseFile(f);
        var res = MarkBuilder.Build(doc, name);
        // 解析器版本：harness 消费的是 M1 已产出的快照，此处沿用产出该快照的 vpdf 版本
        store.SaveProfile(doc, res, f, "vpdf-py-1.1.0");

        // 单份明细（人工复核用）
        var detail = new Dictionary<string, object?>
        {
            ["drawing"] = name,
            ["schema"] = doc.Schema,
            ["sha256"] = doc.Source?.Sha256,
            ["visionFallbackRequired"] = res.VisionFallbackRequired,
            ["declaredItems"] = res.DeclaredItems,
            ["scopes"] = res.Scopes,
            ["warnings"] = res.Warnings,
            ["ruleHits"] = res.RuleHits,
            ["marks"] = res.Marks.Select(m => new Dictionary<string, object?>
            {
                ["id"] = m.Id,
                ["type"] = m.Type.ToString(),
                ["view"] = m.View.ToString(),
                ["text"] = m.Text,
                ["required"] = m.Required,
                ["condition"] = m.Condition,
                ["normBbox"] = m.NormBbox,
                ["confidence"] = m.Confidence,
                ["ruleId"] = m.Source.RuleId,
                ["spanIds"] = m.Source.SpanIds,
                ["imageIds"] = m.Source.ImageIds,
                ["evidence"] = m.Source.Evidence
            }).ToList()
        };
        File.WriteAllText(Path.Combine(outDir, name + ".marks.json"),
            JsonSerializer.Serialize(detail, opts), new UTF8Encoding(false));

        totalMarks += res.Marks.Count;
        childTotal += res.Marks.Sum(m => m.Children?.Count ?? 0);
        declaredNoLoc += res.Marks.Count(m => m.Source.RuleId == "R1-noloc");
        foreach (var (k, v) in res.RuleHits)
            ruleTotals[k] = ruleTotals.TryGetValue(k, out var n) ? n + v : v;
        foreach (var w in res.Warnings) allWarnings.Add($"[{name}] {w}");

        rows.Add(new Dictionary<string, object?>
        {
            ["file"] = name,
            ["marks"] = res.Marks.Count,
            ["declared"] = res.DeclaredItems,
            ["types"] = res.Marks.GroupBy(m => m.Type.ToString()).ToDictionary(g => g.Key, g => g.Count()),
            ["fallback"] = res.VisionFallbackRequired,
            ["warnings"] = res.Warnings.Count
        });
        ok++;

        Console.WriteLine($"{name,-52} marks={res.Marks.Count,3}  declared=[{string.Join("|", res.DeclaredItems)}] scope=[{string.Join("|", res.Scopes)}]");
    }
    catch (V2Exception e)
    {
        failed++;
        allWarnings.Add($"[{name}] PARSE-FAIL {e.Message}");
        Console.WriteLine($"{name,-52} PARSE-FAIL {e.Message}");
    }
    catch (Exception e)
    {
        failed++;
        allWarnings.Add($"[{name}] ERROR {e.GetType().Name}: {e.Message}");
        Console.WriteLine($"{name,-52} ERROR {e.GetType().Name}: {e.Message}");
    }
}

// ---- 落库交叉校验：内存里的 mark 数 vs 数据库里的行数 ----
// Group 子项也各自成行（parent_id 关联），故 DB 行数 = 顶层 mark + 所有子项
var counts = store.Counts();
var dbRuleCounts = store.RuleCounts();

var summary = new Dictionary<string, object?>
{
    ["sweepDir"] = sweep,
    ["files"] = files.Count,
    ["ok"] = ok,
    ["failed"] = failed,
    ["totalMarks"] = totalMarks,
    ["childMarks"] = childTotal,
    ["declaredNotLocated"] = declaredNoLoc,
    ["dbCounts"] = counts,
    ["dbRuleCounts"] = dbRuleCounts,
    ["ruleTotals"] = ruleTotals,
    ["warnings"] = allWarnings,
    ["rows"] = rows
};
File.WriteAllText(Path.Combine(outDir, "_summary.json"),
    JsonSerializer.Serialize(summary, opts), new UTF8Encoding(false));

Console.WriteLine();
Console.WriteLine($"OK={ok} FAILED={failed} totalMarks={totalMarks} declaredNotLocated={declaredNoLoc}");
Console.WriteLine("ruleTotals: " + JsonSerializer.Serialize(ruleTotals));

// ---- 落库交叉校验：内存里的 mark 数 vs 数据库里的行数 ----
// Group 子项也各自成行（parent_id 关联），故 DB 行数 = 顶层 mark + 所有子项
Console.WriteLine("dbCounts: " + JsonSerializer.Serialize(counts));
Console.WriteLine("dbRuleCounts: " + JsonSerializer.Serialize(dbRuleCounts));
var expected = totalMarks + childTotal;
Console.WriteLine($"落库校验: 内存={expected}  DB={counts["marks"]}  " +
                  (expected == counts["marks"] ? "PASS" : "FAIL"));
if (counts["marks_top"] != totalMarks)
    Console.WriteLine($"  ! 顶层 mark 数不符：内存={totalMarks} DB={counts["marks_top"]}");
if (allWarnings.Count > 0)
{
    Console.WriteLine($"--- warnings ({allWarnings.Count}) ---");
    foreach (var w in allWarnings.Take(40)) Console.WriteLine("  " + w);
}
Console.WriteLine("-> " + Path.Combine(outDir, "_summary.json"));
return failed == 0 ? 0 : 1;
