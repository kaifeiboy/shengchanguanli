using System.Diagnostics;
using System.Text.Json;
using Platform.Infrastructure;

namespace Platform.Modules.DefectHistory;

/// <summary>
/// 不良履历服务（v2 底层重构）：单工作簿单 Sheet 数据源 {root}/不良履历汇总.xlsx。
/// 通过 Python 子进程（defect_history.py）读写；使用平台级 IPythonProcessFactory
/// 构造干净最小环境（UTF-8）。不再借用 Drawings 的 OcrService —— 那会造成跨业务模块
/// 依赖，在 Drawings 模块被替换时导致本模块 DI 解析失败、平台启动崩溃。
/// 写操作（add/batch/delete/analysis_auto）经静态 _writeLock 串行化。
/// 数据源根目录可用配置 DefectHistory:Root 覆盖（默认 E:\生产不良履历）。
/// </summary>
public class DefectHistoryService
{
    private static readonly SemaphoreSlim _writeLock = new(1, 1);
    private const string BOOK_FILE = "不良履历汇总.xlsx";
    private const string SHEET_NAME = "不良履历";

    private readonly IPythonProcessFactory _python;
    private readonly string _script;
    private readonly string _root;
    private readonly string _uploadDir;
    private readonly string _exportDir;

    public DefectHistoryService(IPythonProcessFactory python, IConfiguration config)
    {
        _python = python;
        _script = FindScript("defect_history.py");
        _root = config["DefectHistory:Root"] ?? @"E:\生产不良履历";
        var dbPath = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
        var dataDir = Path.GetDirectoryName(dbPath) ?? AppContext.BaseDirectory;
        _uploadDir = Path.Combine(dataDir, "defecthistory");
        _exportDir = Path.Combine(_uploadDir, "export");
        try { Directory.CreateDirectory(_uploadDir); } catch { }
        try { Directory.CreateDirectory(_exportDir); } catch { }
    }

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

    /// <summary>统一 Python 调用：写操作加锁；最多 3 次重试；返回 stdout 的 JSON 文本（成功）或抛 DefectHistoryException。</summary>
    private string RunPython(string[] args, bool isWrite)
    {
        if (isWrite) _writeLock.Wait();
        try
        {
            string jsonText = "", stderr = "";
            int exitCode = -1;
            for (int attempt = 1; attempt <= 3; attempt++)
            {
                var psi = _python.Create(_script, args);
                using (var p = Process.Start(psi)!)
                {
                    var outTask = p.StandardOutput.ReadToEndAsync();
                    var errTask = p.StandardError.ReadToEndAsync();
                    var exited = p.WaitForExit(180_000);
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

    private static string GetStr(JsonElement el, string name)
    {
        if (!el.TryGetProperty(name, out var v)) return "";
        if (v.ValueKind == JsonValueKind.String) return v.GetString() ?? "";
        if (v.ValueKind == JsonValueKind.Number) return v.GetRawText();
        if (v.ValueKind == JsonValueKind.True) return "true";
        if (v.ValueKind == JsonValueKind.False) return "false";
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

    private static JsonElement? Prop(JsonElement root, string name) =>
        root.TryGetProperty(name, out var v) ? v : (JsonElement?)null;

    private static void AddOpt(List<string> args, string name, string? value)
    {
        if (!string.IsNullOrWhiteSpace(value)) args.AddRange(new[] { "--" + name, value.Trim() });
    }

    private static void ValidateRequired(string? value, string label)
    {
        if (string.IsNullOrWhiteSpace(value)) throw new DefectHistoryException($"缺少{label}");
    }

    // ---------- 业务方法 ----------

    /// <summary>GET /meta：数据源概览 + 月份清单（基于第一列日期统计）。进程内缓存（按 book mtime），手机端下拉高频请求秒回。</summary>
    private static readonly object MetaLock = new();
    private static long _metaBookMtime = 0;
    private static string _metaJson = "";

    public object Meta()
    {
        var book = Path.Combine(_root, BOOK_FILE);
        long mt = 0;
        try { if (File.Exists(book)) mt = File.GetLastWriteTimeUtc(book).Ticks; } catch { }
        if (mt != _metaBookMtime || string.IsNullOrEmpty(_metaJson))
        {
            var json = RunPython(new[] { "meta", "--root", _root }, isWrite: false);
            lock (MetaLock)
            {
                if (mt != _metaBookMtime || string.IsNullOrEmpty(_metaJson))
                {
                    _metaBookMtime = mt;
                    _metaJson = json;
                }
            }
        }
        using var doc = JsonDocument.Parse(_metaJson);
        var root = doc.RootElement;
        var months = new List<object>();
        if (Prop(root, "months") is { } arr && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var m in arr.EnumerateArray())
                months.Add(new { month = GetStr(m, "month"), count = GetInt(m, "count") });
        }
        var lines = new List<string>();
        if (Prop(root, "lines") is { } larr && larr.ValueKind == JsonValueKind.Array)
        {
            foreach (var x in larr.EnumerateArray()) lines.Add(x.GetString() ?? "");
        }
        var stages = new List<string>();
        if (Prop(root, "stages") is { } sarr && sarr.ValueKind == JsonValueKind.Array)
        {
            foreach (var x in sarr.EnumerateArray()) stages.Add(x.GetString() ?? "");
        }
        var handles = new List<string>();
        if (Prop(root, "handles") is { } harr && harr.ValueKind == JsonValueKind.Array)
        {
            foreach (var x in harr.EnumerateArray()) handles.Add(x.GetString() ?? "");
        }
        return new
        {
            file = GetStr(root, "file"),
            sheet = GetStr(root, "sheet"),
            totalRows = GetInt(root, "totalRows"),
            minDate = GetStr(root, "minDate"),
            maxDate = GetStr(root, "maxDate"),
            months,
            lines,
            stages,
            handles
        };
    }

    /// <summary>GET /query：按型号模糊查询（可限定月份），返回平铺行列表（基于第一列日期）。</summary>
    public object Query(string model = "", string? month = null)
    {
        var args = new List<string> { "query", "--root", _root };
        if (!string.IsNullOrWhiteSpace(model)) args.AddRange(new[] { "--model", model.Trim() });
        if (!string.IsNullOrWhiteSpace(month)) args.AddRange(new[] { "--month", month.Trim() });
        var json = RunPython(args.ToArray(), isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var rows = new List<object>();
        if (Prop(root, "rows") is { } ra && ra.ValueKind == JsonValueKind.Array)
        {
            foreach (var r in ra.EnumerateArray())
            {
                rows.Add(new
                {
                    row = GetInt(r, "row"),
                    date = GetStr(r, "date"),
                    line = GetStr(r, "line"),
                    model = GetStr(r, "model"),
                    reason = GetStr(r, "reason"),
                    qty = GetStr(r, "qty"),
                    stage = GetStr(r, "stage"),
                    handle = GetStr(r, "handle"),
                    hasImage = GetBool(r, "hasImage"),
                    imgCount = GetInt(r, "imgCount")
                });
            }
        }
        return new { ok = true, file = GetStr(root, "file"), total = GetInt(root, "total"), rows };
    }

    /// <summary>POST /add：单条录入（multipart：表单字段 + files["image"] 可多张）。发生工程/处理方式支持手动录入（自由文本）。</summary>
    public object Add(IFormCollection form, IFormFileCollection files)
    {
        var req = new AddRequest
        {
            Date = form["date"].ToString(),
            Line = form["line"].ToString(),
            Model = form["model"].ToString(),
            Reason = form["reason"].ToString(),
            Qty = form["qty"].ToString(),
            Stage = form["stage"].ToString(),
            Handle = form["handle"].ToString()
        };
        ValidateRequired(req.Date, "日期");
        ValidateRequired(req.Model, "型号");
        ValidateRequired(req.Reason, "问题原因");

        var imageFiles = new List<string>();
        var stamp = Guid.NewGuid().ToString("N")[..12];
        try
        {
            var idx = 0;
            foreach (var f in files)
            {
                if (f.Length == 0) continue;
                var ext = Path.GetExtension(f.FileName).ToLowerInvariant();
                if (ext != ".png" && ext != ".jpg" && ext != ".jpeg" && ext != ".bmp")
                    throw new DefectHistoryException("仅支持 png/jpg/bmp 图片");
                var saved = Path.Combine(_uploadDir, $"add_{stamp}_{idx}{ext}");
                using (var ms = new MemoryStream())
                {
                    f.CopyTo(ms);
                    File.WriteAllBytes(saved, ms.ToArray());
                }
                imageFiles.Add(saved);
                idx++;
            }

            var args = new List<string>
            {
                "add_single", "--root", _root,
                "--date", req.Date!.Trim(),
                "--model", req.Model!.Trim(),
                "--reason", req.Reason!.Trim()
            };
            AddOpt(args, "line", req.Line);
            AddOpt(args, "qty", req.Qty);
            AddOpt(args, "stage", req.Stage);
            AddOpt(args, "handle", req.Handle);
            if (imageFiles.Count > 0) args.AddRange(new[] { "--images", string.Join(",", imageFiles) });

            var json = RunPython(args.ToArray(), isWrite: true);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            return new
            {
                ok = true,
                file = GetStr(root, "file"),
                inserted = GetInt(root, "inserted"),
                images = GetInt(root, "images")
            };
        }
        finally
        {
            foreach (var p in imageFiles)
            {
                try { if (File.Exists(p)) File.Delete(p); } catch { }
            }
        }
    }

    /// <summary>POST /batch：批量导入（?preview=true 仅预览；上传文件含 SMT 工作表将自动舍弃）。</summary>
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
            if (preview) args.AddRange(new[] { "--preview", "1" });
            var json = RunPython(args.ToArray(), isWrite: !preview);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var errors = new List<object>();
            if (Prop(root, "errors") is { } ea && ea.ValueKind == JsonValueKind.Array)
                foreach (var e in ea.EnumerateArray())
                    errors.Add(new { row = GetInt(e, "row"), error = GetStr(e, "error") });
            var previewRows = new List<object>();
            if (Prop(root, "rows") is { } pa && pa.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in pa.EnumerateArray())
                {
                    previewRows.Add(new
                    {
                        row = GetInt(p, "row"),
                        sheet = GetStr(p, "sheet"),
                        date = GetStr(p, "date"),
                        model = GetStr(p, "model"),
                        line = GetStr(p, "line"),
                        stage = GetStr(p, "stage"),
                        valid = GetBool(p, "valid"),
                        error = GetStr(p, "error")
                    });
                }
            }
            return new
            {
                ok = true,
                preview = preview,
                total = GetInt(root, "total"),
                valid = GetInt(root, "valid"),
                imported = GetInt(root, "imported"),
                skipped = GetInt(root, "skipped"),
                droppedSmt = GetInt(root, "droppedSmt"),
                imgTotal = GetInt(root, "imgTotal"),
                imgImported = GetInt(root, "imgImported"),
                errors,
                rows = previewRows
            };
        }
        finally
        {
            // 上传文件归档保留（便于事后排查丢图/数据问题），不再删除
            try
            {
                if (File.Exists(saved))
                {
                    var archDir = Path.Combine(_uploadDir, "uploads");
                    Directory.CreateDirectory(archDir);
                    var dst = Path.Combine(archDir, $"upload_{DateTime.Now:yyyyMMdd_HHmmss}_{Guid.NewGuid().ToString("N")[..6]}{ext}");
                    File.Move(saved, dst, overwrite: true);
                }
            }
            catch { try { if (File.Exists(saved)) File.Delete(saved); } catch { } }
        }
    }

    /// <summary>POST /delete：按数据行索引删除一条（file/sheet 参数兼容保留，实际定位固定汇总工作簿）。</summary>
    public object Delete(string? file, string? sheet, int row)
    {
        if (row < 0)
            throw new DefectHistoryException("row 参数不合法");
        var json = RunPython(new[] { "delete_row", "--root", _root, "--row", row.ToString() }, isWrite: true);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        return new { ok = true, file = GetStr(root, "file"), row = GetInt(root, "row") };
    }

    /// <summary>POST /batch-delete：按行号数组批量删除（一次加载+一次写入，避免逐条全文件重写；行号去重后降序处理，杜绝索引位移误删）。
    /// body: {"rows":[3,5,7]} → {ok, deleted}</summary>
    public object BatchDelete(JsonElement body)
    {
        var rows = new List<int>();
        if (body.TryGetProperty("rows", out var ra) && ra.ValueKind == JsonValueKind.Array)
        {
            foreach (var x in ra.EnumerateArray())
            {
                if (x.TryGetInt32(out var v) && v >= 0)
                    rows.Add(v);
            }
        }
        if (rows.Count == 0)
            throw new DefectHistoryException("rows 不能为空（行号 >= 0）");
        var json = RunPython(new[] { "batch_delete", "--root", _root, "--rows", JsonSerializer.Serialize(rows) }, isWrite: true);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        return new { ok = true, deleted = GetInt(root, "deleted"), file = GetStr(root, "file") };
    }

    /// <summary>GET /image：返回某行第 i 张不良图片（固定汇总工作簿；thumb=true 返回缩略图）。
    /// 性能优化：先确定性 sidecar 路径直读（零 Python 启动）；sidecar 缺失或过期才调 Python extract_images 重建。
    /// 并发安全：重建用串行锁（SemaphoreSlim），失败降级直读 stale sidecar，杜绝 Permission denied 400。</summary>
    private static readonly System.Threading.SemaphoreSlim ImageRebuildLock = new(1, 1);
    public IResult Image(HttpContext ctx, string? file, string? sheet, int row, int i, bool thumb)
    {
        if (row < 0 || i < 0)
            return ErrNoCache(ctx, "row/i 参数不合法");
        try
        {
            // 1) 确定性 sidecar 路径直读（快路径，避免每个图片请求都启动 Python）
            var target = "";
            var sideDir = Path.Combine(_root, "_images", Path.GetFileNameWithoutExtension(BOOK_FILE), SHEET_NAME);
            var sideUpDir = Path.Combine(_root, "_images", Path.GetFileNameWithoutExtension(BOOK_FILE));
            var bookPath = Path.Combine(_root, BOOK_FILE);
            var stamp = Path.Combine(sideUpDir, ".stamp");
            bool sideFresh = File.Exists(stamp) && File.Exists(bookPath)
                && File.GetLastWriteTimeUtc(stamp) >= File.GetLastWriteTimeUtc(bookPath);
            if (sideFresh && Directory.Exists(sideDir))
            {
                var prefix = $"row_{row}_{i}" + (thumb ? "_thumb" : "");
                foreach (var c in Directory.GetFiles(sideDir, prefix + ".*"))
                {
                    if (thumb || !c.Contains("_thumb."))
                    {
                        target = c;
                        break;
                    }
                }
            }
            // 2) 兜底：sidecar 缺失/过期 → Python extract_images 重建（串行锁防并发竞争；失败降级直读 stale sidecar，不 400）
            if (string.IsNullOrEmpty(target) || !File.Exists(target))
            {
                try
                {
                    ImageRebuildLock.Wait();
                    try
                    {
                        var json = RunPython(new[] { "extract_images", "--root", _root }, isWrite: false);
                        using var doc = JsonDocument.Parse(json);
                        var root = doc.RootElement;
                        if (Prop(root, "files") is { } fa && fa.ValueKind == JsonValueKind.Array)
                        {
                            foreach (var f in fa.EnumerateArray())
                            {
                                if (Prop(f, "sheets") is { } sa && sa.ValueKind == JsonValueKind.Array)
                                {
                                    foreach (var s in sa.EnumerateArray())
                                    {
                                        if (GetStr(s, "name") != SHEET_NAME) continue;
                                        if (Prop(s, "images") is { } ia && ia.ValueKind == JsonValueKind.Array)
                                        {
                                            foreach (var im in ia.EnumerateArray())
                                            {
                                                if (GetInt(im, "row") != row) continue;
                                                var listName = thumb ? "thumbs" : "files";
                                                if (Prop(im, listName) is { } fla && fla.ValueKind == JsonValueKind.Array)
                                                {
                                                    var list = new List<string>();
                                                    foreach (var fl in fla.EnumerateArray())
                                                        list.Add(fl.GetString() ?? "");
                                                    if (i >= 0 && i < list.Count)
                                                        target = list[i];
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    finally
                    {
                        ImageRebuildLock.Release();
                    }
                }
                catch (Exception)
                {
                    // Python 重建失败（文件竞争/临时锁）→ 降级直读 stale sidecar，不 400
                }
            }
            // 3) 兜底：确定性路径再找一次（Python 重建后/失败降级）
            if (string.IsNullOrEmpty(target))
            {
                if (Directory.Exists(sideDir))
                {
                    var prefix = $"row_{row}_{i}" + (thumb ? "_thumb" : "");
                    foreach (var c in Directory.GetFiles(sideDir, prefix + ".*"))
                    {
                        if (thumb || !c.Contains("_thumb."))
                        {
                            target = c;
                            break;
                        }
                    }
                }
            }
            if (string.IsNullOrEmpty(target) || !File.Exists(target))
                return ErrNoCache(ctx, "未找到图片（该行可能无图）", 404);
            var ext = Path.GetExtension(target).ToLowerInvariant();
            var ct = ext == ".jpg" || ext == ".jpeg" ? "image/jpeg" : (ext == ".bmp" ? "image/bmp" : "image/png");
            // no-cache+must-revalidate：浏览器每次都需向服务端校验（304 复用），防止手机端缓存旧 thumb 导致"重传后缩略图错位"
            ctx.Response.Headers["Cache-Control"] = "private, no-cache, must-revalidate";
            return Results.File(target, ct);
        }
        catch (DefectHistoryException e)
        {
            return ErrNoCache(ctx, e.Message);
        }
        catch (Exception e)
        {
            return ErrNoCache(ctx, "服务异常：" + e.Message);
        }
    }

    private static IResult ErrNoCache(HttpContext ctx, string msg, int status = 400)
    {
        ctx.Response.Headers["Cache-Control"] = "no-store";
        return status == 404
            ? Results.NotFound(new { error = msg })
            : Results.BadRequest(new { error = msg });
    }

    /// <summary>GET /export：导出 .xlsx（full=true 全量；否则按 model[&month] 查询结果）。表头+框线+日期单元格+图片锚定。</summary>
    public IResult Export(string? model, string? month, bool full)
    {
        var outPath = Path.Combine(_exportDir, $"不良履历导出_{DateTime.Now:yyyyMMdd_HHmmss}.xlsx");
        var args = new List<string> { "export", "--root", _root, "--out", outPath };
        if (full) args.Add("--full");
        if (!string.IsNullOrWhiteSpace(model)) args.AddRange(new[] { "--model", model!.Trim() });
        if (!string.IsNullOrWhiteSpace(month)) args.AddRange(new[] { "--month", month!.Trim() });
        var json = RunPython(args.ToArray(), isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (!File.Exists(outPath))
            return Results.BadRequest(new { error = "导出失败：" + GetStr(root, "error") });
        return Results.File(outPath, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            $"不良履历导出_{DateTime.Now:yyyyMMdd_HHmmss}.xlsx");
    }

    /// <summary>GET /template：上传录入模板（含填写说明；示例行可删）。</summary>
    public IResult Template()
    {
        var path = Path.Combine(_uploadDir, $"template_{Guid.NewGuid().ToString("N")[..8]}.xlsx");
        var json = RunPython(new[] { "template", "--out", path }, isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (!File.Exists(path))
            return Results.BadRequest(new { error = "模板生成失败：" + GetStr(root, "error") });
        return Results.File(path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "不良履历录入模板.xlsx");
    }

    /// <summary>GET /analysis：按月×线别聚合 IPQC/QA 次数（JSON，供 H5 绘图）。model 可选，空=全型号。</summary>
    public object Analysis(string? month, string? model)
    {
        if (string.IsNullOrWhiteSpace(month))
            throw new DefectHistoryException("缺少 month（YYYY-MM）");
        var args = new List<string> { "analysis", "--root", _root, "--month", month!.Trim() };
        if (!string.IsNullOrWhiteSpace(model)) args.AddRange(new[] { "--model", model!.Trim() });
        var json = RunPython(args.ToArray(), isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var lines = new List<string?>();
        if (Prop(root, "lines") is { } la && la.ValueKind == JsonValueKind.Array)
            foreach (var l in la.EnumerateArray()) lines.Add(l.GetString());
        var data = new List<object>();
        if (Prop(root, "data") is { } da && da.ValueKind == JsonValueKind.Array)
        {
            foreach (var d in da.EnumerateArray())
                data.Add(new { month = GetStr(d, "month"), line = GetStr(d, "line"), model = GetStr(d, "model"),
                               ipqc = GetInt(d, "ipqc"), qa = GetInt(d, "qa"), total = GetInt(d, "total") });
        }
        var models = new List<string?>();
        if (Prop(root, "models") is { } ma && ma.ValueKind == JsonValueKind.Array)
            foreach (var m in ma.EnumerateArray()) models.Add(m.GetString());
        var all = GetBool(root, "all");
        var monthsList = new List<string?>();
        if (Prop(root, "months") is { } mo && mo.ValueKind == JsonValueKind.Array)
            foreach (var mm in mo.EnumerateArray()) if (mm.ValueKind == JsonValueKind.String) monthsList.Add(mm.GetString());
        return new { ok = true, all, month = GetStr(root, "month"), model = GetStr(root, "model"),
                     title = GetStr(root, "title"), months = monthsList, lines, models, data };
    }

    /// <summary>GET /analysis/export：生成分析报告。单月→docx（A4 可编辑）；跨月(all)→xlsx（X=月份 堆叠图）。</summary>
    public IResult AnalysisExport(string? month, string? model)
    {
        if (string.IsNullOrWhiteSpace(month))
            return Results.BadRequest(new { error = "缺少 month（YYYY-MM 或 all）" });
        var isAll = month!.Trim().Equals("all", StringComparison.OrdinalIgnoreCase);
        string outPath, contentType, fileName;
        if (isAll)
        {
            outPath = Path.Combine(_exportDir, $"品质分析_跨月_{Guid.NewGuid().ToString("N")[..6]}.xlsx");
            contentType = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
            fileName = "品质分析_跨月.xlsx";
        }
        else
        {
            outPath = Path.Combine(_exportDir, $"品质分析_{month.Trim()}_{Guid.NewGuid().ToString("N")[..6]}.docx");
            contentType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
            fileName = $"品质分析_{month.Trim()}.docx";
        }
        var args = new List<string> { "analysis", "--root", _root, "--month", month.Trim(), "--out", outPath };
        if (!string.IsNullOrWhiteSpace(model)) args.AddRange(new[] { "--model", model!.Trim() });
        var json = RunPython(args.ToArray(), isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (!File.Exists(outPath))
            return Results.BadRequest(new { error = "分析导出失败：" + GetStr(root, "error") });
        return Results.File(outPath, contentType, fileName);
    }

    /// <summary>GET /analysis/status：最新自动分析状态（每月 2 号生成上月全型号报告）。</summary>
    public object AnalysisStatus()
    {
        var json = RunPython(new[] { "analysis_status", "--root", _root }, isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var models = new List<string?>();
        if (Prop(root, "models") is { } ma && ma.ValueKind == JsonValueKind.Array)
            foreach (var m in ma.EnumerateArray()) models.Add(m.GetString());
        return new
        {
            ok = true,
            available = GetBool(root, "available"),
            month = GetStr(root, "month"),
            file = GetStr(root, "file"),
            title = GetStr(root, "title"),
            generatedAt = GetStr(root, "generatedAt"),
            models
        };
    }

    /// <summary>定时任务调用：生成上月全型号分析报告（幂等）。</summary>
    public object AutoAnalysis()
    {
        var json = RunPython(new[] { "analysis_auto", "--root", _root }, isWrite: true);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        return new { ok = true, month = GetStr(root, "month"), file = GetStr(root, "file"),
                     already = GetBool(root, "already"), message = GetStr(root, "message") };
    }

    /// <summary>供 Worker 判断是否已生成上月报告。返回 (month, file, available)。</summary>
    public (string month, string file, bool available) AnalysisStatusInternal()
    {
        var json = RunPython(new[] { "analysis_status", "--root", _root }, isWrite: false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        return (GetStr(root, "month"), GetStr(root, "file"), GetBool(root, "available"));
    }
}

/// <summary>单条录入请求（multipart 表单字段；发生工程/处理方式为自由文本可手动录入）。</summary>
public sealed class AddRequest
{
    public string? Date { get; set; }
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
