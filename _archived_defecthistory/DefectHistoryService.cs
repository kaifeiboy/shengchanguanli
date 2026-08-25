using System.Diagnostics;
using System.Text.Json;
using Platform.Modules.Drawings;

namespace Platform.Modules.DefectHistory;

/// <summary>
/// 不良履历查询服务：通过 Python 子进程（defect_history.py）读写 E:\生产不良履历 下的 .xls 数据源。
/// 复用 OcrService.CreatePythonPsi（干净最小环境，UTF-8 输出）；写操作经静态 _writeLock 串行化。
/// 数据源根目录可用配置 DefectHistory:Root 覆盖（默认 E:\生产不良履历）。
/// </summary>
public class DefectHistoryService
{
    /// <summary>写操作全局串行化（add / batch / delete / create_month 共用一把锁，读不锁）。</summary>
    private static readonly SemaphoreSlim _writeLock = new(1, 1);

    private readonly OcrService _ocr;
    private readonly string _script;
    private readonly string _root;
    private readonly string _uploadDir;

    public DefectHistoryService(OcrService ocr, IConfiguration config)
    {
        _ocr = ocr;
        _script = FindScript("defect_history.py");
        _root = config["DefectHistory:Root"] ?? @"E:\生产不良履历";
        var dbPath = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
        var dataDir = Path.GetDirectoryName(dbPath) ?? AppContext.BaseDirectory;
        _uploadDir = Path.Combine(dataDir, "defecthistory");
        try { Directory.CreateDirectory(_uploadDir); } catch { }
    }

    /// <summary>从 BaseDirectory 向上查找脚本文件（优先 cwd/Modules/DefectHistory）。</summary>
    private static string FindScript(string filename)
    {
        var baseDir = AppContext.BaseDirectory;
        var candidates = new[]
        {
            Path.Combine(Directory.GetCurrentDirectory(), "Modules", "DefectHistory"),
            Path.Combine(Directory.GetCurrentDirectory(), "Modules", "Drawings"),
            Directory.GetCurrentDirectory(),
            baseDir
        };
        foreach (var dir in candidates)
        {
            var p = Path.Combine(dir, filename);
            if (File.Exists(p)) return p;
        }
        var current = baseDir;
        for (int i = 0; i < 6; i++)
        {
            var parent = Path.GetDirectoryName(current);
            if (string.IsNullOrEmpty(parent) || parent == current) break;
            var p = Path.Combine(parent, filename);
            if (File.Exists(p)) return p;
            var p2 = Path.Combine(parent, "Modules", "DefectHistory", filename);
            if (File.Exists(p2)) return p2;
            current = parent;
        }
        throw new FileNotFoundException($"defect_history.py 未找到（从 {baseDir} 向上搜索 6 层）");
    }

    /// <summary>统一 Python 调用：写操作加锁；最多 3 次重试；返回 stdout 的 JSON 文本（成功）或错误消息。</summary>
    private string RunPython(string[] args, bool isWrite)
    {
        if (isWrite) _writeLock.Wait();
        try
        {
            string jsonText = "", stderr = "";
            int exitCode = -1;
            for (int attempt = 1; attempt <= 3; attempt++)
            {
                var psi = _ocr.CreatePythonPsi(_script, args);
                using (var p = Process.Start(psi)!)
                {
                    var outTask = p.StandardOutput.ReadToEndAsync();
                    var errTask = p.StandardError.ReadToEndAsync();
                    var exited = p.WaitForExit(120_000);
                    if (!exited) { try { p.Kill(); } catch { } }
                    Task.WaitAll(outTask, errTask);
                    jsonText = outTask.Result ?? "";
                    stderr = errTask.Result ?? "";
                    exitCode = p.ExitCode;
                }
                if (!string.IsNullOrWhiteSpace(jsonText) && exitCode == 0) break;
                if (attempt < 3) Thread.Sleep(800 * attempt);
            }

            if (string.IsNullOrWhiteSpace(jsonText))
                throw new DefectHistoryException($"Python 无输出；exit={exitCode} stderr={Trunc(stderr, 200)}");

            int startIdx = jsonText.IndexOf('{');
            int endIdx = jsonText.LastIndexOf('}');
            if (startIdx < 0 || endIdx < 0 || endIdx <= startIdx)
                throw new DefectHistoryException($"stdout 无 JSON 段：{Trunc(jsonText, 200)}");

            var segment = jsonText.Substring(startIdx, endIdx - startIdx + 1);
            using var doc = JsonDocument.Parse(segment);
            var root = doc.RootElement;
            if (!root.TryGetProperty("success", out var sEl) || !sEl.GetBoolean())
            {
                var err = root.TryGetProperty("error", out var eEl) ? eEl.GetString() : "defect_history 执行失败";
                throw new DefectHistoryException(err ?? "defect_history 执行失败");
            }
            return segment;
        }
        finally
        {
            if (isWrite) _writeLock.Release();
        }
    }

    private static string Trunc(string s, int n) =>
        string.IsNullOrEmpty(s) ? "(empty)" : (s.Length <= n ? s : s[..n]);

    private static JsonElement? Prop(JsonElement root, string name) =>
        root.TryGetProperty(name, out var v) ? v : (JsonElement?)null;

    // ---------- 业务方法 ----------

    /// <summary>GET /meta：列出所有月份文件与 sheet 行数，供 H5 月份下拉。</summary>
    public object Meta()
    {
        var json = RunPython(new[] { "meta", "--root", _root }, isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var months = new List<object>();
        if (Prop(root, "months") is { } arr && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var m in arr.EnumerateArray())
            {
                var sheets = new List<object>();
                if (Prop(m, "sheets") is { } sa && sa.ValueKind == JsonValueKind.Array)
                {
                    foreach (var s in sa.EnumerateArray())
                    {
                        sheets.Add(new
                        {
                            name = GetStr(s, "name"),
                            rows = GetInt(s, "rows"),
                            error = GetStr(s, "error")
                        });
                    }
                }
                months.Add(new
                {
                    month = GetStr(m, "month"),
                    file = GetStr(m, "file"),
                    sheets
                });
            }
        }
        return new { months };
    }

    /// <summary>GET /query：跨全部月份文件模糊查询型号（可限定月份）。</summary>
    public object Query(string model, string? month)
    {
        var args = new List<string> { "query", "--root", _root, "--model", model };
        if (!string.IsNullOrWhiteSpace(month)) args.AddRange(new[] { "--month", month.Trim() });
        var json = RunPython(args.ToArray(), isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var groups = new List<object>();
        if (Prop(root, "groups") is { } ga && ga.ValueKind == JsonValueKind.Array)
        {
            foreach (var g in ga.EnumerateArray())
            {
                var rows = new List<object>();
                if (Prop(g, "rows") is { } ra && ra.ValueKind == JsonValueKind.Array)
                {
                    foreach (var r in ra.EnumerateArray())
                    {
                        rows.Add(new
                        {
                            row = GetInt(r, "row"),
                            sheet = GetStr(r, "sheet"),
                            date = GetStr(r, "date"),
                            line = GetStr(r, "line"),
                            model = GetStr(r, "model"),
                            reason = GetStr(r, "reason"),
                            qty = GetStr(r, "qty"),
                            stage = GetStr(r, "stage"),
                            image = GetStr(r, "image"),
                            handle = GetStr(r, "handle")
                        });
                    }
                }
                groups.Add(new
                {
                    month = GetStr(g, "month"),
                    file = GetStr(g, "file"),
                    count = GetInt(g, "count"),
                    rows
                });
            }
        }
        var warnings = new List<object>();
        if (Prop(root, "warnings") is { } wa && wa.ValueKind == JsonValueKind.Array)
        {
            foreach (var w in wa.EnumerateArray())
                warnings.Add(new { file = GetStr(w, "file"), error = GetStr(w, "error") });
        }
        return new { ok = true, total = GetInt(root, "total"), groups, warnings };
    }

    /// <summary>POST /add：单条录入（缺月份自动建表）。</summary>
    public object Add(AddRequest req)
    {
        ValidateRequired(req.Date, "日期");
        ValidateRequired(req.Model, "型号");
        ValidateRequired(req.Reason, "问题原因");
        if (req.Sheet != "SMT" && req.Sheet != "组装")
            throw new DefectHistoryException("sheet 必须是 SMT 或 组装");

        var date = req.Date!.Trim();
        var model = req.Model!.Trim();
        var reason = req.Reason!.Trim();
        var args = new List<string>
        {
            "add_single", "--root", _root,
            "--date", date,
            "--sheet", req.Sheet,
            "--model", model,
            "--reason", reason
        };
        AddOpt(args, "line", req.Line);
        AddOpt(args, "qty", req.Qty);
        AddOpt(args, "stage", req.Stage);
        AddOpt(args, "handle", req.Handle);

        var json = RunPython(args.ToArray(), isWrite: true);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        return new
        {
            ok = true,
            file = GetStr(root, "file"),
            sheet = GetStr(root, "sheet"),
            row = GetInt(root, "row"),
            createdMonth = GetBool(root, "createdMonth")
        };
    }

    /// <summary>POST /batch：批量导入（preview=true 只预览不落盘）。</summary>
    public object Batch(IFormFile file, bool preview)
    {
        if (file == null || file.Length == 0)
            throw new DefectHistoryException("未收到上传文件");
        var ext = Path.GetExtension(file.FileName).ToLowerInvariant();
        if (ext != ".xls" && ext != ".xlsx")
            throw new DefectHistoryException("仅支持 .xls / .xlsx 文件");

        var stamp = Guid.NewGuid().ToString("N")[..12];
        var saved = Path.Combine(_uploadDir, $"upload_{stamp}{ext}");
        try
        {
            using (var ms = new MemoryStream())
            {
                file.CopyTo(ms);
                File.WriteAllBytes(saved, ms.ToArray());
            }
            var args = new List<string> { "batch_import", "--root", _root, "--upload", saved };
            if (preview) args.Add("--dry-run");
            var json = RunPython(args.ToArray(), isWrite: !preview);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var createdMonths = new List<string?>();
            if (Prop(root, "createdMonths") is { } ca && ca.ValueKind == JsonValueKind.Array)
                foreach (var c in ca.EnumerateArray()) createdMonths.Add(c.GetString());
            var errors = new List<object>();
            if (Prop(root, "errors") is { } ea && ea.ValueKind == JsonValueKind.Array)
                foreach (var e in ea.EnumerateArray())
                    errors.Add(new { row = GetInt(e, "row"), error = GetStr(e, "error") });
            var previewRows = new List<object>();
            if (Prop(root, "preview") is { } pa && pa.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in pa.EnumerateArray())
                {
                    previewRows.Add(new
                    {
                        row = GetInt(p, "row"),
                        sheet = GetStr(p, "sheet"),
                        date = GetStr(p, "date"),
                        model = GetStr(p, "model"),
                        reason = GetStr(p, "reason"),
                        qty = GetStr(p, "qty"),
                        stage = GetStr(p, "stage"),
                        handle = GetStr(p, "handle"),
                        targetMonth = GetStr(p, "targetMonth"),
                        valid = GetBool(p, "valid"),
                        error = GetStr(p, "error")
                    });
                }
            }
            return new
            {
                ok = true,
                imported = GetInt(root, "imported"),
                skipped = GetInt(root, "skipped"),
                createdMonths,
                errors,
                preview = previewRows
            };
        }
        finally
        {
            try { if (File.Exists(saved)) File.Delete(saved); } catch { }
        }
    }

    /// <summary>POST /delete：按 文件+sheet+数据行索引 删除一条（写前自动备份 .bak）。</summary>
    public object Delete(string file, string sheet, int row)
    {
        if (string.IsNullOrWhiteSpace(file) || Path.GetFileName(file) != file || !file.EndsWith(".xls", StringComparison.OrdinalIgnoreCase))
            throw new DefectHistoryException("非法的文件名");
        if (sheet != "SMT" && sheet != "组装")
            throw new DefectHistoryException("sheet 必须是 SMT 或 组装");

        var json = RunPython(new[]
        {
            "delete_row", "--root", _root,
            "--file", file,
            "--sheet", sheet,
            "--row", row.ToString()
        }, isWrite: true);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        return new
        {
            ok = true,
            file = GetStr(root, "file"),
            backup = GetStr(root, "backup"),
            remainingRows = GetInt(root, "remainingRows")
        };
    }

    // ---------- 小工具 ----------

    private static void AddOpt(List<string> args, string name, string? value)
    {
        if (!string.IsNullOrWhiteSpace(value)) args.AddRange(new[] { "--" + name, value.Trim() });
    }

    private static void ValidateRequired(string? value, string label)
    {
        if (string.IsNullOrWhiteSpace(value)) throw new DefectHistoryException($"缺少{label}");
    }

    private static string GetStr(JsonElement el, string name)
    {
        if (!el.TryGetProperty(name, out var v)) return "";
        if (v.ValueKind == JsonValueKind.String) return v.GetString() ?? "";
        if (v.ValueKind == JsonValueKind.Number) return v.GetRawText();
        return "";
    }

    private static int GetInt(JsonElement el, string name)
    {
        if (el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var i)) return i;
        return 0;
    }

    private static bool GetBool(JsonElement el, string name)
    {
        if (el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True) return true;
        return false;
    }
}

/// <summary>单条录入请求。</summary>
public sealed class AddRequest
{
    public string? Date { get; set; }
    public string? Sheet { get; set; }
    public string? Line { get; set; }
    public string? Model { get; set; }
    public string? Reason { get; set; }
    public string? Qty { get; set; }
    public string? Stage { get; set; }
    public string? Handle { get; set; }
}

/// <summary>业务错误（模块层捕获转 400）。</summary>
public sealed class DefectHistoryException : Exception
{
    public DefectHistoryException(string message) : base(message) { }
}
