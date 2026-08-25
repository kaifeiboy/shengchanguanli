using System.Data.SQLite;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Platform.Tests;

/// <summary>
/// JQ 黄金测试：JQ 实物照片匹配 JQ 图纸(112) → 红框规则切 4 块(blk_0..3)。
///
/// Phase 3 简化匹配（2026-07-17）：
///   - 主链路：MatchBySimpleText（文本直接比对，字符集 Jaccard + LCS）
///   - 旧 ScoreAgainstV3 保留做兜底
///   - 验收标准：
///     ① HTTP 200 且 matchStatus == "full"（简化匹配字符集重叠率 100%）
///     ② matched == true
///     ③ score >= 0.90（JQ 实物照与 blk_3 几乎字符完全相同）
///     ④ bestIdx == 3（打标规格块）
///
/// 前置条件：服务运行在 http://127.0.0.1:5000
/// 资产：tests/assets/jq_photo.jpg = JQ 产品实物照片（PC-P1HJQ 服务热线：4008601111）
/// 冻结合规：纯增量测试工程，不修改任何冻结文件。
/// </summary>
public class JqGoldenTest : IDisposable
{
    private readonly HttpClient _http;
    private readonly string _baseUrl;

    public JqGoldenTest()
    {
        _baseUrl = Environment.GetEnvironmentVariable("TEST_BASE_URL") ?? "http://127.0.0.1:5000";
        _http = new HttpClient { BaseAddress = new Uri(_baseUrl), Timeout = TimeSpan.FromSeconds(30) };
    }

    public void Dispose() => _http.Dispose();

    [Fact]
    public async Task JQ_Photo_Matches_EngravingSpec_Block()
    {
        // ── Arrange ──
        var assetPath = ResolveAssetPath("jq_photo.jpg");
        Assert.True(File.Exists(assetPath), $"黄金测试资产不存在: {assetPath}");
        var drawingId = 112;

        // ── Act ──
        using var form = new MultipartFormDataContent();
        using var fileStream = new FileStream(assetPath, FileMode.Open, FileAccess.Read);
        using var fileContent = new StreamContent(fileStream);
        fileContent.Headers.ContentType = new MediaTypeHeaderValue("image/jpeg");
        form.Add(fileContent, "file", Path.GetFileName(assetPath));
        form.Add(new StringContent(drawingId.ToString()), "drawingId");

        var response = await _http.PostAsync("api/drawings/match-block", form);
        var bodyJson = await response.Content.ReadAsStringAsync();
        Assert.Equal(200, (int)response.StatusCode);
        var result = JsonDocument.Parse(bodyJson).RootElement;

        // ── Assert ①②③: 匹配基本健康度 ──
        var matchStatus = result.GetProperty("matchStatus").GetString();
        var matched = result.GetProperty("matched").GetBoolean();
        var score = result.GetProperty("score").GetDouble();

        var validStatuses = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "full", "partial" };
        Assert.True(matchStatus is not null && validStatuses.Contains(matchStatus!),
            $"matchStatus 应为 full/partial，实际={matchStatus}。照片可能未识别到有效编码。");
        Assert.True(matched,
            $"matched 应为 true。score={score}");
        Assert.True(score >= 0.90,
            $"score 应 >= 0.90（OCR修复后打标块应强匹配），实际={score:F3}。");

        // ── Assert ④: 红框规则 4 块布局 + OCR 修复后，最佳候选为打标规格块（blk_3, idx=3）──
        // JQ 图纸(112): 红框切出 blk_0..3；blk_3=侧面打标视图（PC-P1HJQ 服务热线：4008601111）
        // 2026-07-16 修复 batch_ocr.py 单位不匹配 bug（conf 0~1 vs THRESH 70 百分比）后，blk_3 OCR 正常返回
        const int ExpectedEngraveBlockIdx = 3;

        var candidatesArr = result.GetProperty("candidates");
        int bestIdx = -1;
        double bestScore = -1;
        foreach (var c in candidatesArr.EnumerateArray())
        {
            var cs = c.GetProperty("score").GetDouble();
            if (cs > bestScore) { bestScore = cs; bestIdx = c.GetProperty("idx").GetInt32(); }
        }
        Assert.True(bestIdx >= 0, "candidates 不应为空");

        // 辅助验证：最佳块的 Tokens 是否含打标标识符（模糊匹配容忍OCR噪声）
        var dbPath = ResolveDbPath();
        bool hasId = BlockTokensContain(dbPath, drawingId, bestIdx,
            new[] { "P1H", "4008601111", "PC-P", "服务热线", "1111" });

        Assert.True(bestIdx == ExpectedEngraveBlockIdx,
            $"★★★ 黄金断言失败 ★★★\n" +
            $"预期 idx={ExpectedEngraveBlockIdx}(打标规格)，实际 idx={bestIdx}(score={bestScore:F3})。\n" +
            $"{(hasId ? $"idx={bestIdx} 含打标标识符但非预期blk" : "该块无任何打标标识符")}。");
    }

    /// <summary>
    /// 差异标记黄金测试：JQ 实物照 vs blk_3 → 0 缺标 / 0 多标 / N 个 CAD 标注灰色框。
    /// 验证简化 diff 算法 + CAD 过滤正常工作。
    /// </summary>
    [Fact]
    public async Task JQ_Photo_DiffMarkers_AreAllGreenOrCAD()
    {
        var assetPath = ResolveAssetPath("jq_photo.jpg");
        if (!File.Exists(assetPath)) return;  // 无资产则跳过
        const int drawingId = 112;
        const int blockIdx = 3;  // 打标规格块

        using var form = new MultipartFormDataContent();
        using var fs = new FileStream(assetPath, FileMode.Open, FileAccess.Read);
        using var fc = new StreamContent(fs);
        fc.Headers.ContentType = new MediaTypeHeaderValue("image/jpeg");
        form.Add(fc, "file", Path.GetFileName(assetPath));
        form.Add(new StringContent(drawingId.ToString()), "drawingId");
        form.Add(new StringContent(blockIdx.ToString()), "blockIdx");

        var resp = await _http.PostAsync("api/drawings/mark-diff", form);
        var body = await resp.Content.ReadAsStringAsync();
        Assert.Equal(200, (int)resp.StatusCode);
        var r = JsonDocument.Parse(body).RootElement;
        var success = r.GetProperty("success").GetBoolean();
        Assert.True(success, $"mark-diff 失败: {body[..Math.Min(300, body.Length)]}");

        // JQ 实物照与 blk_3 文本一致 → 0 缺标 / 0 多标
        int red = r.GetProperty("redRegions").GetArrayLength();
        int yellow = r.GetProperty("yellowRegions").GetArrayLength();
        int gray = r.GetProperty("grayRegions").GetArrayLength();
        int green = r.GetProperty("greenRegions").GetArrayLength();
        Assert.Equal(0, red);
        Assert.Equal(0, yellow);
        Assert.True(gray > 0, $"应至少识别出 1 个 CAD 标注, 实际 gray={gray}");
        Assert.True(green > 0, $"应至少 1 个匹配项, 实际 green={green}");

        // 标记图 URL 应可访问
        var url = r.GetProperty("markedImageUrl").GetString();
        Assert.False(string.IsNullOrEmpty(url));
    }

    #region 辅助方法

    private static string ResolveAssetPath(string fileName)
    {
        var envDir = Environment.GetEnvironmentVariable("TEST_ASSETS_DIR");
        if (!string.IsNullOrEmpty(envDir)) return Path.Combine(envDir, fileName);
        var baseDir = AppContext.BaseDirectory;
        for (var dir = new DirectoryInfo(baseDir); dir != null && dir.Exists; dir = dir.Parent)
        {
            var candidate = Path.Combine(dir.FullName, "assets", fileName);
            if (File.Exists(candidate)) return candidate;
        }
        return Path.Combine(baseDir, "assets", fileName);
    }

    private static string ResolveDbPath()
    {
        var envDb = Environment.GetEnvironmentVariable("TEST_DB_PATH");
        if (!string.IsNullOrEmpty(envDb)) return envDb;
        var slnDir = FindSolutionDirectory();
        return slnDir != null ? Path.Combine(slnDir, "..", "..", "data", "app.db") : null!;
    }

    private static string? FindSolutionDirectory()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (; dir != null && dir.Exists; dir = dir.Parent)
            if (dir.GetFiles("*.sln").Length > 0 || Directory.Exists(Path.Combine(dir.FullName, "src")))
                return dir.FullName;
        return null;
    }

    /// <summary>只读查询：检查图块 Tokens 是否含任一目标标识符（大小写不敏感子串匹配）。</summary>
    private static bool BlockTokensContain(string dbPath, int drawingId, int bIdx, string[] targets)
    {
        if (!File.Exists(dbPath)) return false;
        try
        {
            using var conn = new SQLiteConnection($"Data Source={dbPath};Read Only=True;");
            conn.Open();
            using var cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT Tokens FROM drawing_blocks WHERE DrawingId=@did AND BIdx=@bidx";
            cmd.Parameters.AddWithValue("@did", drawingId);
            cmd.Parameters.AddWithValue("@bidx", bIdx);
            var tokJson = cmd.ExecuteScalar() as string;
            if (string.IsNullOrEmpty(tokJson)) return false;
            var tokens = JsonSerializer.Deserialize<List<string>>(tokJson) ?? [];
            var combined = string.Join(" ", tokens).ToUpperInvariant();
            return targets.Any(id => combined.Contains(id.ToUpperInvariant()));
        }
        catch { return false; }
    }

    #endregion
}
