using System.Diagnostics;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using Platform.Infrastructure;
using Platform.Modules.Drawings;

namespace Platform.Modules.InventoryTail;

/// <summary>库存尾数模块业务异常。</summary>
public class InventoryTailException : Exception
{
    public InventoryTailException(string message) : base(message) { }
}

/// <summary>
/// 库存尾数服务：SQLite 存储（独立于共享 DbContext 建表，仅用其 Open() 连接）；
/// OCR / 导出走 Python 子进程（inventory_ocr.py / inventory_export.py），
/// 复用 OcrService.CreatePythonPsi（干净最小环境、UTF-8）。
/// 只操作本模块的数据表，不影响平台及其它模块。
/// </summary>
public class InventoryTailService
{
    private static readonly SemaphoreSlim _writeLock = new(1, 1);

    private readonly OcrService _ocr;
    private readonly DbContext _db;
    private readonly string _ocrScript;
    private readonly string _exportScript;
    private readonly string _workDir;

    public InventoryTailService(OcrService ocr, DbContext db, IConfiguration config)
    {
        _ocr = ocr;
        _db = db;
        _ocrScript = FindScript("inventory_ocr.py");
        _exportScript = FindScript("inventory_export.py");
        var dbPath = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
        var dataDir = Path.GetDirectoryName(dbPath) ?? AppContext.BaseDirectory;
        _workDir = Path.Combine(dataDir, "inventory");
        try { Directory.CreateDirectory(_workDir); } catch { }
        EnsureSchema();
    }

    private static string FindScript(string filename)
    {
        var baseDir = AppContext.BaseDirectory;
        var candidates = new[]
        {
            Path.Combine(Directory.GetCurrentDirectory(), "Modules", "InventoryTail"),
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
            var p = Path.Combine(parent, "Modules", "InventoryTail", filename);
            if (File.Exists(p)) return p;
            current = parent;
        }
        throw new InventoryTailException($"{filename} 未找到（从 {baseDir} 向上搜索 6 层）");
    }

    // ---------- 建表（幂等，只建本模块表） ----------
    private void EnsureSchema()
    {
        using var conn = _db.Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            CREATE TABLE IF NOT EXISTS inventory_materials (
                Id           INTEGER PRIMARY KEY AUTOINCREMENT,
                KuHao        TEXT NOT NULL,
                Code         TEXT NOT NULL,
                MaterialInfo TEXT NOT NULL,
                Qty          INTEGER NOT NULL DEFAULT 0,
                CreatedAt    TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_inventory_mat
                ON inventory_materials (KuHao, Code, MaterialInfo);
            CREATE TABLE IF NOT EXISTS inventory_records (
                Id         INTEGER PRIMARY KEY AUTOINCREMENT,
                MaterialId INTEGER NOT NULL,
                OpType     TEXT NOT NULL,      -- in / out
                Qty        INTEGER NOT NULL,
                Handler    TEXT NOT NULL,      -- 经手人
                CreatedAt  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_rec_mid
                ON inventory_records (MaterialId, CreatedAt);
        ";
        cmd.ExecuteNonQuery();
    }

    // ---------- 新增 ----------
    public object AddMaterial(JsonElement body)
    {
        var kuHao = GetStr(body, "kuHao").Trim();
        var code = GetStr(body, "code").Trim();
        var material = GetStr(body, "materialInfo").Trim();
        var qty = GetInt(body, "qty");
        if (string.IsNullOrWhiteSpace(kuHao) || string.IsNullOrWhiteSpace(code) || string.IsNullOrWhiteSpace(material))
            throw new InventoryTailException("库号/编码/材料信息均不能为空");
        if (qty < 0) throw new InventoryTailException("数量不能为负");

        using var conn = _db.Open();
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT COUNT(*) FROM inventory_materials WHERE KuHao=$k AND Code=$c AND MaterialInfo=$m";
            c.Parameters.AddWithValue("$k", kuHao);
            c.Parameters.AddWithValue("$c", code);
            c.Parameters.AddWithValue("$m", material);
            if (Convert.ToInt32(c.ExecuteScalar()) > 0)
                throw new InventoryTailException("该库号+编码+材料信息已存在");
        }
        var ts = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
        using var ins = conn.CreateCommand();
        ins.CommandText = "INSERT INTO inventory_materials (KuHao, Code, MaterialInfo, Qty, CreatedAt) VALUES ($k,$c,$m,$q,$t)";
        ins.Parameters.AddWithValue("$k", kuHao);
        ins.Parameters.AddWithValue("$c", code);
        ins.Parameters.AddWithValue("$m", material);
        ins.Parameters.AddWithValue("$q", qty);
        ins.Parameters.AddWithValue("$t", ts);
        ins.ExecuteNonQuery();
        using var idc = conn.CreateCommand();
        idc.CommandText = "SELECT last_insert_rowid()";
        var id = Convert.ToInt32(idc.ExecuteScalar());
        return new { id, kuHao, code, materialInfo = material, qty, createdAt = ts };
    }

    /// <summary>批量新增（拍照识别多条）。每条独立成功/失败，返回明细。</summary>
    public object AddMaterialsBatch(JsonElement body)
    {
        if (body.ValueKind != JsonValueKind.Object ||
            !body.TryGetProperty("items", out var items) || items.ValueKind != JsonValueKind.Array)
            throw new InventoryTailException("缺少 items 数组");

        var results = new List<object>();
        int okCount = 0;
        foreach (var it in items.EnumerateArray())
        {
            try
            {
                results.Add(AddMaterial(it));
                okCount++;
            }
            catch (InventoryTailException ex)
            {
                results.Add(new { error = ex.Message,
                    kuHao = GetStr(it, "kuHao"), code = GetStr(it, "code"),
                    materialInfo = GetStr(it, "materialInfo") });
            }
        }
        return new { okCount, total = results.Count, results };
    }

    // ---------- 查询 ----------
    public List<object> Search(string q)
    {
        q = (q ?? "").Trim();
        var list = new List<object>();
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        if (q.Length == 0)
        {
            c.CommandText = "SELECT Id, KuHao, Code, MaterialInfo, Qty, CreatedAt FROM inventory_materials ORDER BY CreatedAt DESC, Id DESC LIMIT 500";
        }
        else
        {
            c.CommandText = @"SELECT Id, KuHao, Code, MaterialInfo, Qty, CreatedAt FROM inventory_materials
                              WHERE KuHao LIKE $q OR Code LIKE $q OR MaterialInfo LIKE $q
                              ORDER BY CreatedAt DESC, Id DESC LIMIT 500";
            c.Parameters.AddWithValue("$q", "%" + q + "%");
        }
        using var r = c.ExecuteReader();
        while (r.Read())
            list.Add(ReadMaterial(r));
        return list;
    }

    public object? GetDetail(int id)
    {
        using var conn = _db.Open();
        object? mat = null;
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT Id, KuHao, Code, MaterialInfo, Qty, CreatedAt FROM inventory_materials WHERE Id=$id";
            c.Parameters.AddWithValue("$id", id);
            using var r = c.ExecuteReader();
            if (r.Read()) mat = ReadMaterial(r);
        }
        if (mat is null) return null;

        var records = new List<object>();
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT Id, OpType, Qty, Handler, CreatedAt FROM inventory_records WHERE MaterialId=$id ORDER BY CreatedAt DESC, Id DESC";
            c.Parameters.AddWithValue("$id", id);
            using var r = c.ExecuteReader();
            while (r.Read())
            {
                records.Add(new
                {
                    id = r.GetInt32(0),
                    opType = r.GetString(1),      // in / out
                    qty = r.GetInt32(2),
                    handler = r.GetString(3),
                    createdAt = r.GetString(4)
                });
            }
        }
        return new { material = mat, records };
    }

    private static object ReadMaterial(SqliteDataReader r) => new
    {
        id = r.GetInt32(0),
        kuHao = r.GetString(1),
        code = r.GetString(2),
        materialInfo = r.GetString(3),
        qty = r.GetInt32(4),
        createdAt = r.GetString(5)
    };

    // ---------- 出入库 ----------
    public object AddRecord(int id, JsonElement body)
    {
        var opType = GetStr(body, "opType").Trim().ToLowerInvariant();
        var qty = GetInt(body, "qty");
        var handler = GetStr(body, "handler").Trim();
        if (opType != "in" && opType != "out") throw new InventoryTailException("opType 必须为 in 或 out");
        if (qty <= 0) throw new InventoryTailException("数量必须大于 0");
        if (string.IsNullOrWhiteSpace(handler)) throw new InventoryTailException("请填写经手人");

        var ts = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
        using var conn = _db.Open();
        using var tx = conn.BeginTransaction();
        try
        {
            // 读当前库存（锁行）
            int curQty;
            using (var c = conn.CreateCommand())
            {
                c.Transaction = tx;
                c.CommandText = "SELECT Qty FROM inventory_materials WHERE Id=$id";
                c.Parameters.AddWithValue("$id", id);
                var v = c.ExecuteScalar();
                if (v is null || v == DBNull.Value)
                    throw new InventoryTailException("材料不存在");
                curQty = Convert.ToInt32(v);
            }
            var newQty = opType == "in" ? curQty + qty : curQty - qty;
            if (newQty < 0)
                throw new InventoryTailException($"出库数量超过当前库存（当前 {curQty}）");

            using (var u = conn.CreateCommand())
            {
                u.Transaction = tx;
                u.CommandText = "UPDATE inventory_materials SET Qty=$q WHERE Id=$id";
                u.Parameters.AddWithValue("$q", newQty);
                u.Parameters.AddWithValue("$id", id);
                u.ExecuteNonQuery();
            }
            using (var ins = conn.CreateCommand())
            {
                ins.Transaction = tx;
                ins.CommandText = "INSERT INTO inventory_records (MaterialId, OpType, Qty, Handler, CreatedAt) VALUES ($mid,$op,$q,$h,$t)";
                ins.Parameters.AddWithValue("$mid", id);
                ins.Parameters.AddWithValue("$op", opType);
                ins.Parameters.AddWithValue("$q", qty);
                ins.Parameters.AddWithValue("$h", handler);
                ins.Parameters.AddWithValue("$t", ts);
                ins.ExecuteNonQuery();
            }
            tx.Commit();
            return new { id, opType, qty, handler, createdAt = ts, currentQty = newQty };
        }
        catch
        {
            try { tx.Rollback(); } catch { }
            throw;
        }
    }

    // ---------- 经手人（历史去重按最近排序，最后操作人最前） ----------
    public List<object> Handlers()
    {
        var list = new List<object>();
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        c.CommandText = "SELECT Handler, MAX(CreatedAt) AS Last FROM inventory_records WHERE Handler <> '' GROUP BY Handler ORDER BY Last DESC, Handler";
        using var r = c.ExecuteReader();
        while (r.Read())
            list.Add(new { name = r.GetString(0), lastAt = r.GetString(1) });
        return list;
    }

    // ---------- 拍照：透视矫正 + 白平衡 ----------
    public object OcrCorrect(IFormFile file, string pts)
    {
        var ext = Path.GetExtension(file.FileName);
        if (string.IsNullOrWhiteSpace(ext)) ext = ".jpg";
        var stamp = Guid.NewGuid().ToString("N");
        var inFile = Path.Combine(_workDir, "in_" + stamp + ext);
        var outFile = Path.Combine(_workDir, "correct_" + stamp + ".jpg");
        try
        {
            using var ms = new MemoryStream();
            file.CopyTo(ms);
            File.WriteAllBytes(inFile, ms.ToArray());

            var args = new List<string> { "correct", "--in", inFile, "--out", outFile };
            if (!string.IsNullOrWhiteSpace(pts)) args.AddRange(new[] { "--pts", pts });
            var json = RunPython(_ocrScript, args.ToArray(), isWrite: false);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.TryGetProperty("error", out var errEl) && errEl.ValueKind == JsonValueKind.String)
                throw new InventoryTailException(errEl.GetString()!);

            if (!File.Exists(outFile))
                throw new InventoryTailException("矫正输出文件未生成");
            var bytes = File.ReadAllBytes(outFile);
            return new
            {
                ok = true,
                imageBase64 = Convert.ToBase64String(bytes),
                width = GetInt(root, "w"),
                height = GetInt(root, "h"),
                auto = GetBool(root, "auto")
            };
        }
        finally
        {
            TryDelete(inFile);
            TryDelete(outFile);
        }
    }

    // ---------- 拍照：框选区域识别 ----------
    public object OcrBoxes(IFormFile file, string boxesJson)
    {
        if (string.IsNullOrWhiteSpace(boxesJson))
            throw new InventoryTailException("缺少 boxes（框选区域 JSON）");
        var ext = Path.GetExtension(file.FileName);
        if (string.IsNullOrWhiteSpace(ext)) ext = ".jpg";
        var stamp = Guid.NewGuid().ToString("N");
        var inFile = Path.Combine(_workDir, "in_" + stamp + ext);
        var boxesFile = Path.Combine(_workDir, "boxes_" + stamp + ".json");
        var outFile = Path.Combine(_workDir, "ocr_" + stamp + ".json");
        try
        {
            using var ms = new MemoryStream();
            file.CopyTo(ms);
            File.WriteAllBytes(inFile, ms.ToArray());
            File.WriteAllText(boxesFile, boxesJson);

            var args = new[] { "boxes", "--in", inFile, "--boxes", boxesFile, "--out", outFile };
            var json = RunPython(_ocrScript, args, isWrite: false);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.TryGetProperty("error", out var errEl) && errEl.ValueKind == JsonValueKind.String)
                throw new InventoryTailException(errEl.GetString()!);

            if (!File.Exists(outFile))
                throw new InventoryTailException("OCR 输出文件未生成");
            var outText = File.ReadAllText(outFile);
            using var outDoc = JsonDocument.Parse(outText);
            var boxes = new List<object>();
            if (outDoc.RootElement.ValueKind == JsonValueKind.Array)
            {
                foreach (var b in outDoc.RootElement.EnumerateArray())
                {
                    boxes.Add(new
                    {
                        idx = GetInt(b, "idx"),
                        text = GetStr(b, "text")
                    });
                }
            }
            return new { ok = true, count = boxes.Count, boxes };
        }
        finally
        {
            TryDelete(inFile);
            TryDelete(boxesFile);
            TryDelete(outFile);
        }
    }

    // ---------- 导出 xlsx ----------
    public IResult Export(string q)
    {
        var rows = Search(q);
        var stamp = Guid.NewGuid().ToString("N");
        var rowsFile = Path.Combine(_workDir, "rows_" + stamp + ".json");
        var outFile = Path.Combine(_workDir, "inventory_export_" + stamp + ".xlsx");
        try
        {
            File.WriteAllText(rowsFile, JsonSerializer.Serialize(rows));
            var args = new[] { "export", "--rows", rowsFile, "--out", outFile };
            var json = RunPython(_exportScript, args, isWrite: false);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.TryGetProperty("error", out var errEl) && errEl.ValueKind == JsonValueKind.String)
                return Results.BadRequest(new { error = "导出失败：" + errEl.GetString() });

            if (!File.Exists(outFile))
                return Results.BadRequest(new { error = "导出文件未生成" });
            return Results.File(outFile,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                $"库存尾数_{DateTime.Now:yyyyMMdd_HHmmss}.xlsx");
        }
        finally
        {
            TryDelete(rowsFile);
            TryDelete(outFile);
        }
    }

    // ---------- 工具 ----------
    /// <summary>统一 Python 调用：写操作加锁；stdout 返回 JSON 文本。</summary>
    private string RunPython(string script, string[] args, bool isWrite)
    {
        if (isWrite) _writeLock.Wait();
        try
        {
            string jsonText = "", stderr = "";
            int exitCode = -1;
            for (int attempt = 1; attempt <= 3; attempt++)
            {
                var psi = _ocr.CreatePythonPsi(script, args);
                using var p = Process.Start(psi)!;
                var outTask = p.StandardOutput.ReadToEndAsync();
                var errTask = p.StandardError.ReadToEndAsync();
                var exited = p.WaitForExit(120_000);
                if (!exited) { try { p.Kill(); } catch { } }
                Task.WaitAll(outTask, errTask);
                jsonText = outTask.Result ?? "";
                stderr = errTask.Result ?? "";
                exitCode = p.ExitCode;
                if (!string.IsNullOrWhiteSpace(jsonText) && exitCode == 0) break;
                if (attempt < 3) Thread.Sleep(500 * attempt);
            }
            if (string.IsNullOrWhiteSpace(jsonText))
                throw new InventoryTailException($"Python 无输出；exit={exitCode} stderr={Trunc(stderr, 200)}");
            int startIdx = jsonText.IndexOf('{');
            int endIdx = jsonText.LastIndexOf('}');
            if (startIdx < 0 || endIdx <= startIdx)
                throw new InventoryTailException($"stdout 无 JSON 段：{Trunc(jsonText, 200)}");
            return jsonText.Substring(startIdx, endIdx - startIdx + 1);
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
        if (el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String)
            return v.GetString() ?? "";
        return "";
    }

    private static int GetInt(JsonElement el, string name)
    {
        if (el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var i))
            return i;
        return 0;
    }

    private static bool GetBool(JsonElement el, string name)
    {
        if (el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True)
            return true;
        return false;
    }

    private static void TryDelete(string p)
    {
        try { File.Delete(p); } catch { }
    }
}
