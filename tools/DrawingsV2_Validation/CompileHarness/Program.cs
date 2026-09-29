using Platform.Modules.DrawingsV2.Decision;

var dbPath = Path.Combine(Path.GetTempPath(), $"drawingsv2-store-{Guid.NewGuid():N}.db");
try
{
    var store = new V2Store(dbPath);
    var doc = new VpdfDocument
    {
        Schema = "vpdf/1",
        Source = new VpdfSource { Sha256 = "fixture-sha", PageCount = 1 },
        Pages = new List<VpdfPage>
        {
            new()
            {
                Index = 0,
                CanonicalSizePt = new[] { 1000.0, 800.0 },
                TextLayer = new VpdfTextLayer { Present = true },
                Diagnostics = new VpdfPageDiagnostics { Source = "pdf_vector" }
            }
        }
    };
    var result = new MarkBuilder.Result { DrawingKey = "fixture" };
    var firstId = store.SaveProfile(doc, result, "/tmp/fixture.pdf", "test");
    store.SaveCompare(firstId, "fixture", "/tmp/photo.jpg", "photo-sha", "photo.jpg",
        true, "none", false, 1, new Dictionary<string, int>(), Array.Empty<string>(), "[]");
    store.SaveReview(firstId, "tester", null, Array.Empty<(string, bool, bool)>());

    var secondId = store.SaveProfile(doc, result, "/tmp/fixture.pdf", "test-2");
    if (secondId != firstId) throw new Exception("重分析同一 PDF 应复用 profile id");
    if (store.ListCompareRecords().Count != 1) throw new Exception("重分析不得删除历史比对记录");
    if (store.GetProfileStatus(secondId) != "draft") throw new Exception("重分析后必须重新复核");

    Console.WriteLine("CompileHarness: source compilation and store re-analysis test passed");
}
finally
{
    foreach (var suffix in new[] { "", "-wal", "-shm" })
    {
        try { File.Delete(dbPath + suffix); } catch { }
    }
}
