using Microsoft.AspNetCore.Http;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Configuration;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Platform.Infrastructure;

namespace Platform.Modules.Drawings;

/// <summary>
/// 图纸检索业务逻辑：
///  - 建库（仅文件名提取型号，快）
///  - 索引列表 / 模糊搜索
///  - 懒加载单图 OCR（按内容切分部位）
///  - 照片 → 匹配该图纸部位（部位自动识别）
/// 对应需求 FR-01~FR-06 / SR-04。
/// </summary>
public class DrawingService
{
    private readonly DbContext _db;
    private readonly FileAccessService _files;
    private readonly OcrService _ocr;
    private readonly string _graphicScript;
    private readonly string _segmentScript;
    private readonly string _dataDir;
    private readonly string _dbPath;
    private readonly IConfiguration _config;

    // ---- 缓存层（任务42：减少匹配耗时）----
    // 块签名内存缓存：免每次查库+反序列化（命中即毫秒级返回）。
    private readonly ConcurrentDictionary<int, List<BlockRec>> _blockCache = new();
    // 照片结果缓存：同一张照片（按内容 MD5）秒回，不必重跑 OCR+匹配。
    private readonly ConcurrentDictionary<string, BlockMatchResult> _photoCache = new();
    // 图块 bbox 缓存（提升差异标记速度）：避免每次 mark-diff 都对图块重跑 OCR 加载模型。
    // Key = "{drawingId}_{blockIdx}"，Value = OCR lines JSON
    private readonly ConcurrentDictionary<string, string> _blockBboxCache = new();

    public DrawingService(DbContext db, FileAccessService files, OcrService ocr, IConfiguration config)
    {
        _db = db;
        _files = files;
        _ocr = ocr;
        _graphicScript = FindScript("graphic_match.py");
        _segmentScript = FindScript("segment_blocks.py");
        _config = config;
        // 与 Database:Path 同树（项目根 data/），避免落到 bin/ 下被重建清空
        var dbPath = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
        _dbPath = dbPath;
        _dataDir = Path.GetDirectoryName(dbPath) ?? AppContext.BaseDirectory;
    }

    // ---------- 建库 ----------
    /// <summary>扫描图纸目录，从文件名提取型号，建/重建图纸库（不含 OCR，快）。</summary>
    public int InitLibrary()
    {
        var files = _files.ListDrawings();
        using var conn = _db.Open();
        using (var c = conn.CreateCommand()) { c.CommandText = "DELETE FROM drawings; DELETE FROM drawing_parts;"; c.ExecuteNonQuery(); }

        var now = DateTime.Now.ToString("o");
        var n = 0;
        foreach (var f in files)
        {
            var model = ModelParser.ExtractModel(f.Name);
            using var c = conn.CreateCommand();
            c.CommandText = "INSERT INTO drawings (Model,FileName,VirtualPath,CreatedAt) VALUES ($m,$fn,$vp,$t)";
            c.Parameters.AddWithValue("$m", model);
            c.Parameters.AddWithValue("$fn", f.Name);
            c.Parameters.AddWithValue("$vp", "drawings/" + f.Name);
            c.Parameters.AddWithValue("$t", now);
            c.ExecuteNonQuery();
            n++;
        }
        return n;
    }

    // ---------- 新增：添加图纸（仅入库，懒切割，同固有图纸处理流程） ----------
    public record AddDrawingResult(int? Id, string? FileName, string? VirtualPath, string? Model, string? Error);

    public AddDrawingResult AddDrawing(IFormFile? file, string? modelOverride = null)
    {
        if (file is null || file.Length == 0) return new(null, null, null, null, "未收到文件");
        // 1) PDF 魔数校验（同一流：读魔数后复位到 0，复用给落盘，避免依赖框架多次 OpenReadStream 的实现细节）
        using var ms = file.OpenReadStream();
        var head = new byte[5];
        var nRead = ms.Read(head, 0, 5);
        if (nRead < 4 || System.Text.Encoding.ASCII.GetString(head)[..4] != "%PDF")
            return new(null, null, null, null, "仅支持 PDF 图纸");
        ms.Position = 0;
        // 2) 根目录 + 安全文件名
        var root = _config["Files:Root"] ?? @"E:\生产打标效果图";
        try { if (!Directory.Exists(root)) Directory.CreateDirectory(root); }
        catch (Exception ex) { return new(null, null, null, null, "无法访问图纸目录：" + ex.Message); }
        var safe = SanitizeFileName(file.FileName);
        var target = Path.Combine(root, safe);
        int k = 1;
        while (File.Exists(target)) { target = Path.Combine(root, InsertBeforeExt(safe, $"_{k}")); k++; }
        var safeName = Path.GetFileName(target);
        // 3) 落盘（失败则结构化报错，不抛 500）
        try { using var outStream = File.Create(target); ms.CopyTo(outStream); }
        catch (Exception ex)
        {
            try { if (File.Exists(target)) File.Delete(target); } catch { }
            return new(null, safeName, null, null, "保存文件失败：" + ex.Message);
        }
        // 4) 型号（与 InitLibrary 一致：ExtractModel 可能返回 null，保留 null）
        var model = modelOverride is { Length: >0 } ? modelOverride!.Trim() : ModelParser.ExtractModel(safeName);
        // 5) 入库（同 InitLibrary 的 INSERT 模式；失败时回滚已写文件，不遗留孤儿）
        var now = DateTime.Now.ToString("o");
        try
        {
            using var conn = _db.Open();
            using (var c = conn.CreateCommand())
            {
                c.CommandText = "INSERT INTO drawings (Model,FileName,VirtualPath,CreatedAt) VALUES ($m,$fn,$vp,$t)";
                c.Parameters.AddWithValue("$m", model);
                c.Parameters.AddWithValue("$fn", safeName);
                c.Parameters.AddWithValue("$vp", "drawings/" + safeName);
                c.Parameters.AddWithValue("$t", now);
                c.ExecuteNonQuery();
            }
            int newId;
            using (var idc = conn.CreateCommand()) { idc.CommandText = "SELECT last_insert_rowid()"; newId = Convert.ToInt32(idc.ExecuteScalar()); }
            return new(newId, safeName, "drawings/" + safeName, model, null);
        }
        catch (Exception ex)
        {
            try { if (File.Exists(target)) File.Delete(target); } catch { }
            return new(null, safeName, null, null, "入库失败：" + ex.Message);
        }
    }

    public List<AddDrawingResult> AddDrawingsBatch(IFormFileCollection? files, string? modelOverride = null)
    {
        var list = new List<AddDrawingResult>();
        if (files is null || files.Count == 0) { list.Add(new(null, null, null, null, "未收到文件")); return list; }
        foreach (var f in files)
        {
            if (f is null || f.Length == 0) { list.Add(new(null, f?.FileName, null, null, "空文件")); continue; }
            list.Add(AddDrawing(f, modelOverride)); // 非 PDF 时 AddDrawing 内部返回 Error，不中断其余
        }
        return list;
    }

    private static string SanitizeFileName(string name)
    {
        var baseName = Path.GetFileName(name ?? "");
        var invalid = Path.GetInvalidFileNameChars();
        var sb = new System.Text.StringBuilder();
        foreach (var ch in baseName) sb.Append(invalid.Contains(ch) || char.IsControl(ch) ? '_' : ch);
        var s = sb.ToString().Trim();
        if (s.Length == 0) s = "drawing_" + Guid.NewGuid().ToString("N")[..8];
        if (!s.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase)) s += ".pdf";
        return s;
    }

    private static string InsertBeforeExt(string name, string insert)
    {
        var dot = name.LastIndexOf('.');
        return dot < 0 ? name + insert : name[..dot] + insert + name[dot..];
    }

    // ---------- 索引 / 搜索 ----------
    public record DrawingDto(int Id, string Model, string FileName, string VirtualPath);

    /// <summary>全部型号索引（用于首屏列表）。</summary>
    public List<DrawingDto> ListAll()
    {
        var list = new List<DrawingDto>();
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        c.CommandText = "SELECT Id,Model,FileName,VirtualPath FROM drawings ORDER BY FileName";
        using var r = c.ExecuteReader();
        while (r.Read())
            list.Add(new DrawingDto(r.GetInt32(0), r.GetString(1), r.GetString(2), r.GetString(3)));
        return list;
    }

    /// <summary>模糊搜索：子串命中优先，否则按编辑距离相似度排序。</summary>
    public List<DrawingDto> Search(string q)
    {
        q = (q ?? "").Trim();
        var all = ListAll();
        if (q.Length == 0) return all;

        var scored = all.Select(d =>
        {
            var name = d.Model ?? "";
            var fn = d.FileName ?? "";
            double s = name.Contains(q, StringComparison.OrdinalIgnoreCase)
                     || fn.Contains(q, StringComparison.OrdinalIgnoreCase)
                ? 1.0
                : Math.Max(Similarity(name, q), Similarity(fn, q));
            return (d, s);
        })
        .Where(x => x.s > 0.25)
        .OrderByDescending(x => x.s)
        .Take(50)
        .Select(x => x.d)
        .ToList();
        return scored;
    }

    // ---------- 详情 / 懒加载 OCR ----------
    public record PartDto(int Id, string PartName, string Content);
    public record DrawingDetail(
        int Id, string Model, string FileName, string VirtualPath,
        bool Analyzed, List<PartDto> Parts);

    /// <summary>图纸详情 + 其部位片段。</summary>
    public DrawingDetail? GetDetail(int id)
    {
        using var conn = _db.Open();
        int? ocrLen = null;
        string? model = null, fn = null, vp = null;
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT Model,FileName,VirtualPath,OcrText FROM drawings WHERE Id=$id";
            c.Parameters.AddWithValue("$id", id);
            using var r = c.ExecuteReader();
            if (!r.Read()) return null;
            model = r.GetString(0);
            fn = r.GetString(1);
            vp = r.GetString(2);
            if (!r.IsDBNull(3)) ocrLen = r.GetString(3)?.Length;
        }

        var parts = new List<PartDto>();
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT Id,PartName,PartText FROM drawing_parts WHERE DrawingId=$id ORDER BY Id";
            c.Parameters.AddWithValue("$id", id);
            using var r = c.ExecuteReader();
            while (r.Read())
                parts.Add(new PartDto(r.GetInt32(0), r.GetString(1), r.GetString(2)));
        }
        return new DrawingDetail(id, model!, fn!, vp!, ocrLen.HasValue, parts);
    }

    /// <summary>懒加载：若该图纸尚未 OCR，则渲染 PDF + OCR，按内容切分部位并入库（缓存）。</summary>
    public void EnsureAnalyzed(int id)
    {
        string? fileName = null, ocrText = null;
        using (var conn = _db.Open())
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT FileName,OcrText FROM drawings WHERE Id=$id";
            c.Parameters.AddWithValue("$id", id);
            using var r = c.ExecuteReader();
            if (r.Read()) { fileName = r.GetString(0); if (!r.IsDBNull(1)) ocrText = r.GetString(1); }
        }
        if (fileName == null) return; // 无此图纸
        // 已有“有效”OCR 文本才跳过；空文本（之前 OCR 失败）则允许重试。
        if (ocrText != null && ocrText.Trim().Length > 0) return;

        var phys = _files.ResolvePhysical("drawings/" + fileName);
        if (phys == null || !File.Exists(phys)) return;

        var raw = _ocr.RecognizePdf(phys);

        var parts = ModelParser.SegmentParts(raw);
        var now = DateTime.Now.ToString("o");

        using var c2 = _db.Open();
        using var tx = c2.BeginTransaction();
        using (var u = c2.CreateCommand())
        {
            u.CommandText = "UPDATE drawings SET OcrText=$t WHERE Id=$id";
            u.Parameters.AddWithValue("$t", ModelParser.Normalize(raw));
            u.Parameters.AddWithValue("$id", id);
            u.ExecuteNonQuery();
        }
        using (var d = c2.CreateCommand())
        {
            d.CommandText = "DELETE FROM drawing_parts WHERE DrawingId=$id";
            d.Parameters.AddWithValue("$id", id);
            d.ExecuteNonQuery();
        }
        foreach (var p in parts)
        {
            using var i = c2.CreateCommand();
            i.CommandText = "INSERT INTO drawing_parts (DrawingId,PartName,PartText,CreatedAt) VALUES ($id,$n,$c,$t)";
            i.Parameters.AddWithValue("$id", id);
            i.Parameters.AddWithValue("$n", p.Name);
            i.Parameters.AddWithValue("$c", p.Content);
            i.Parameters.AddWithValue("$t", now);
            i.ExecuteNonQuery();
        }
        tx.Commit();
    }

    // ---------- 部位识别（照片 → 匹配图纸部位） ----------
    public record PartScore(string PartName, double Score);
    public record PartMatch(
        int DrawingId, string? Model, string? FileName, string? VirtualPath,
        string? Part, double Score, string PhotoText, bool FromKeyword,
        List<PartScore> AllScores,
        double TextScore, double SymbolScore, double GraphicScore, string? PartImageUrl);

    /// <summary>
    /// 上传效果图照片：三路信号匹配 → 自动识别部位。
    ///   Signal-1 文字：OCR 照片文字 → 模糊包含/重叠/关键词 共现
    ///   Signal-2 符号：提取编码（型号码/日期/编号）→ 与部位内容编码比对
    ///   Signal-3 图形：渲染图纸页面 → 裁剪各部位区域 → 感知哈希(aHash)比较
    /// 最终得分 = 加权融合 (文字 50% + 符号 30% + 图形 20%)。
    /// </summary>
    public PartMatch RecognizePart(int drawingId, IFormFile file)
    {
        EnsureAnalyzed(drawingId);

        var photoText = _ocr.Recognize(file);
        var photoNorm = ModelParser.Normalize(photoText);
        var photoCodes = ModelParser.ExtractCodes(photoText);

        var parts = GetParts(drawingId);
        string? part = null;
        double score = 0;
        bool fromKw = false;
        var allScores = new List<PartScore>();
        double bestSymbolScore = 0, bestGraphicScore = 0, bestTextScore = 0;
        string? bestPartImageUrl = null;

        if (parts.Count > 0)
        {
            // 预渲染图纸页面 + 裁剪各部位区域图（缓存）
            EnsurePageRendered(drawingId, parts);
            var partCrops = BuildPartCrops(drawingId, parts);
            var photoHash = ComputeAverageHash(file);

            // 第一遍：计算每部位的原始三路信号
            var rows = new List<(string Name, double Txt, double Sym, double Gfx, string? CropUrl)>();
            foreach (var p in parts)
            {
                double txtScore = 0;
                // ── Signal-1: 文字（部位名）匹配，最高优先 ──
                // 复合效果图的各部位共享同一段 OCR 文字，通用重叠/关键词共现
                // 会制造假阳性（所有部位同分），故仅以「照片是否含该部位名/核心字」
                // 作为文字信号，噪声最小、判定最稳。
                if (p.PartName != null && photoNorm.Contains(p.PartName))
                    txtScore = 0.95;
                else if (p.PartName != null && p.PartName.Length >= 2)
                {
                    var core = p.PartName.Substring(0, 2);
                    if (photoNorm.Contains(core))
                        txtScore = 0.6;
                    else if (photoNorm.Contains(p.PartName[0].ToString()))
                        txtScore = 0.3;
                }
                txtScore = Math.Min(txtScore, 1.0);

                // ── Signal-2: 符号 / 编码 匹配 ──
                var partCodes = ModelParser.ExtractCodes(p.Content);
                var symScore = Math.Min(ModelParser.CodeMatchScore(photoCodes, partCodes) * 1.5, 1.0);

                // ── Signal-3: 图形感知哈希 ──
                double gfxScore = 0;
                // 始终给出部位图地址（端点对找不到裁剪图的情况回退到整页）
                string? cropUrl = "api/drawings/part-image?id=" + drawingId +
                                      "&part=" + System.Web.HttpUtility.UrlEncode(p.PartName ?? "");
                if (partCrops.TryGetValue(p.PartName ?? "", out var cropPath) &&
                    !string.IsNullOrEmpty(cropPath))
                {
                    // 回退到整页的图（_full_/_fallback）无法区分部位 → 图形分置 0，交由文字/符号判定
                    var isFallback = cropPath.Contains("_full_") || cropPath.Contains("_fallback");
                    if (!isFallback)
                    {
                        var partHash = ComputeAverageHashFromPath(cropPath);
                        if (!string.IsNullOrEmpty(partHash))
                            gfxScore = HashSimilarity(photoHash, partHash);
                    }
                }
                rows.Add((p.PartName ?? "未知", txtScore, symScore, gfxScore, cropUrl));
            }

            // 判别性检查：某路信号若「最大值被多个部位并列」，说明它无法区分部位 → 该路置 0。
            // 例：复合效果图的各部位共享同一段文字/编码，符号信号对所有部位同分 → 不采信。
            bool Discriminates(IEnumerable<double> vals)
            {
                var max = vals.Max();
                return vals.Count(v => Math.Abs(v - max) < 0.01) == 1;
            }
            var symOk = Discriminates(rows.Select(r => r.Sym));
            var gfxOk = Discriminates(rows.Select(r => r.Gfx));

            // 第二遍：仅用「可判别」的信号做加权融合（不可判别的路不参与，避免假阳性）
            foreach (var r in rows)
            {
                var sym = symOk ? r.Sym : 0;
                var gfx = gfxOk ? r.Gfx : 0;
                var fused = r.Txt * 0.50 + sym * 0.30 + gfx * 0.20;
                allScores.Add(new PartScore(r.Name, Math.Round(fused, 3)));
                if (fused > score)
                {
                    score = fused; part = r.Name;
                    bestTextScore = r.Txt; bestSymbolScore = r.Sym; bestGraphicScore = r.Gfx;
                    bestPartImageUrl = r.CropUrl;
                }
            }
        }

        // Level 3: 回退 — 照片内直接 DetectPart
        // 低置信度（< 0.4）且无明确部位名命中时，若连任何部位关键词都检测不到，
        // 则判为「未识别」，避免给出低置信度的错误猜测。
        if (part == null || score < 0.4)
        {
            var kw = ModelParser.DetectPart(photoNorm);
            if (kw != null) { part = kw; score = Math.Max(score, 0.5); fromKw = true; }
            else if (score < 0.4) { part = null; score = 0; }
        }

        var d = GetDrawing(drawingId);
        var matched = part != null;

        using (var conn = _db.Open())
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "INSERT INTO drawing_histories (QueryType,QueryText,DetectedModel,DetectedPart,DrawingId,Matched,CreatedAt) VALUES ($t,$q,$dm,$dp,$did,$m,$ts)";
            c.Parameters.AddWithValue("$t", "part");
            c.Parameters.AddWithValue("$q", photoText.Length > 2000 ? photoText.Substring(0, 2000) : photoText);
            c.Parameters.AddWithValue("$dm", (object?)d?.Model ?? DBNull.Value);
            c.Parameters.AddWithValue("$dp", (object?)part ?? DBNull.Value);
            c.Parameters.AddWithValue("$did", (object?)drawingId ?? DBNull.Value);
            c.Parameters.AddWithValue("$m", matched ? 1 : 0);
            c.Parameters.AddWithValue("$ts", DateTime.Now.ToString("o"));
            c.ExecuteNonQuery();
        }

        return new PartMatch(
            drawingId, d?.Model, d?.FileName, d?.VirtualPath,
            part, Math.Round(score, 2), photoText, fromKw, allScores,
            Math.Round(bestTextScore, 2), Math.Round(bestSymbolScore, 2), Math.Round(bestGraphicScore, 2), bestPartImageUrl);
    }

    // ---------- 内部辅助：图形信号（渲染 / 裁剪 / 感知哈希） ----------
    private string GetImageCacheDir()
    {
        var dir = Path.Combine(_dataDir, "part_images");
        if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
        return dir;
    }

    /// <summary>确保图纸已渲染为页面图 + 各部位区域裁剪图，并把「部位名→裁剪图路径」映射写入 {did}_crops.json（缓存）。</summary>
    private void EnsurePageRendered(int drawingId, List<PartDto> parts)
    {
        var dir = GetImageCacheDir();
        if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
        var jsonPath = Path.Combine(dir, $"{drawingId}_crops.json");

        // 已有映射缓存则跳过（裁剪图已生成过）
        if (File.Exists(jsonPath) && new FileInfo(jsonPath).Length > 0) return;

        var d = GetDrawing(drawingId);
        if (d == null) return;
        var phys = _files.ResolvePhysical(d.VirtualPath);
        if (phys == null || !File.Exists(phys)) return;

        var partNames = parts.Select(p => p.PartName).Where(p => !string.IsNullOrEmpty(p)).ToList();
        if (partNames.Count == 0) return;

        // 用 ArgumentList 传参，避免把 JSON 嵌进引号字符串导致的 Windows 解析错位
        var psi = new ProcessStartInfo(_ocr.PythonExe)
        {
            RedirectStandardOutput = true, RedirectStandardError = true,
            UseShellExecute = false, CreateNoWindow = true
        };
        psi.ArgumentList.Add(_graphicScript);
        psi.ArgumentList.Add("prepare");
        psi.ArgumentList.Add(phys);
        psi.ArgumentList.Add(drawingId.ToString());
        psi.ArgumentList.Add(dir);
        psi.ArgumentList.Add(JsonSerializer.Serialize(partNames));
        try
        {
            using var p = Process.Start(psi)!;
            var outText = p.StandardOutput.ReadToEnd();
            p.StandardError.ReadToEnd();
            p.WaitForExit(30_000);
            // 解析 Python 返回的 crops 映射并持久化（路径统一为反斜杠规范）
            if (!string.IsNullOrWhiteSpace(outText))
            {
                using var doc = JsonDocument.Parse(outText);
                if (doc.RootElement.TryGetProperty("crops", out var cropsEl))
                {
                    var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                    foreach (var prop in cropsEl.EnumerateObject())
                        map[prop.Name] = prop.Value.GetString() ?? "";
                    File.WriteAllText(jsonPath, JsonSerializer.Serialize(map));
                }
            }
        }
        catch { /* 图形信号失败不影响主流程 */ }
    }

    /// <summary>读取部位→裁剪图路径映射（来自 {did}_crops.json）。</summary>
    private Dictionary<string, string> BuildPartCrops(int drawingId, List<PartDto> parts)
    {
        var dir = GetImageCacheDir();
        var jsonPath = Path.Combine(dir, $"{drawingId}_crops.json");
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!File.Exists(jsonPath)) return result;
        try
        {
            var map = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(jsonPath));
            if (map != null)
                foreach (var kv in map)
                    if (!string.IsNullOrEmpty(kv.Value))
                        result[kv.Key] = kv.Value;
        }
        catch { }
        return result;
    }

    /// <summary>计算上传图片的感知哈希（保存临时文件 → 调 Python 脚本）。</summary>
    private string ComputeAverageHash(IFormFile file)
    {
        try
        {
            var tmp = Path.Combine(Path.GetTempPath(), "ahash_" + Guid.NewGuid().ToString("N")) + ".png";
            using (var fs = File.Create(tmp)) file.CopyTo(fs);
            var hash = ComputeAverageHashFromPath(tmp);
            try { File.Delete(tmp); } catch { }
            return hash ?? "";
        }
        catch { return ""; }
    }

    private static string? ComputeAverageHashFromPath(string imagePath)
    {
        if (!File.Exists(imagePath)) return null;
        var dir = Path.GetDirectoryName(typeof(DrawingService).Assembly.Location) ?? AppContext.BaseDirectory;
        var script = FindScript("graphic_match.py");
        if (!File.Exists(script)) return null;
        var py = @"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe";
        // 用 ArgumentList 传参，避免 Windows 引号嵌套解析错位
        var psi = new ProcessStartInfo(py)
        {
            RedirectStandardOutput = true, RedirectStandardError = true,
            UseShellExecute = false, CreateNoWindow = true
        };
        psi.ArgumentList.Add(script);
        psi.ArgumentList.Add("hash");
        psi.ArgumentList.Add(imagePath);
        try
        {
            using var p = Process.Start(psi)!;
            var outText = p.StandardOutput.ReadToEnd();
            p.StandardError.ReadToEnd();
            if (p.WaitForExit(10_000))
                return outText.Trim();
        }
        catch { }
        return null;
    }

    /// <summary>两个 aHash（64位 01 串）的汉明距离相似度。</summary>
    private static double HashSimilarity(string h1, string h2)
    {
        if (string.IsNullOrEmpty(h1) || string.IsNullOrEmpty(h2) || h1.Length != h2.Length) return 0;
        int diff = 0;
        for (int i = 0; i < h1.Length; i++)
            if (h1[i] != h2[i]) diff++;
        return 1.0 - (double)diff / h1.Length;
    }

    /// <summary>从 BaseDirectory 向上查找脚本文件。</summary>
    private static string FindScript(string filename)
    {
        var baseDir = AppContext.BaseDirectory;
        var candidates = new[] {
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
            // 同时检查 Modules/Drawings 子目录（脚本实际位置）
            var p2 = Path.Combine(parent, "Modules", "Drawings", filename);
            if (File.Exists(p2)) return p2;
            current = parent;
        }
        return Path.Combine(baseDir, filename);
    }
    /// <summary>返回指定部位的裁剪区域图（PNG）。</summary>
    public IResult ServePartImage(int drawingId, string partName)
    {
        var dir = GetImageCacheDir();
        // 优先用 crops 映射里的真实裁剪图（可能是整页回退图）
        var jsonPath = Path.Combine(dir, $"{drawingId}_crops.json");
        if (File.Exists(jsonPath))
        {
            try
            {
                var map = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(jsonPath));
                if (map != null && map.TryGetValue(partName, out var crop) && !string.IsNullOrEmpty(crop))
                {
                    var cropNorm = crop.Replace("\\", "/");
                    // 复合效果图里的“裁剪图”往往只是标签小区域（几十~几百字节），
                    // 信息量极低；小于阈值时回退到整页，更完整地展示产品与部位位置。
                    if (File.Exists(cropNorm) && new FileInfo(cropNorm).Length >= 4096)
                        return Results.File(cropNorm, "image/png", $"{partName}.png");
                }
            }
            catch { }
        }
        // 兜底：返回第一页全图
        var fallback = Directory.GetFiles(dir, $"{drawingId}_p1.png").FirstOrDefault()
            ?? Directory.GetFiles(dir, $"{drawingId}_*.png").FirstOrDefault();
        if (fallback != null)
            return Results.File(fallback, "image/png", $"drawing_{drawingId}.png");
        return Results.NotFound(new { error = "部位图未生成" });
    }

    private List<PartDto> GetParts(int id)
    {
        var list = new List<PartDto>();
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        c.CommandText = "SELECT Id,PartName,PartText FROM drawing_parts WHERE DrawingId=$id ORDER BY Id";
        c.Parameters.AddWithValue("$id", id);
        using var r = c.ExecuteReader();
        while (r.Read()) list.Add(new PartDto(r.GetInt32(0), r.GetString(1), r.GetString(2)));
        return list;
    }

    public DrawingDto? GetDrawing(int id)
    {
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        c.CommandText = "SELECT Id,Model,FileName,VirtualPath FROM drawings WHERE Id=$id";
        c.Parameters.AddWithValue("$id", id);
        using var r = c.ExecuteReader();
        return r.Read()
            ? new DrawingDto(r.GetInt32(0), r.GetString(1), r.GetString(2), r.GetString(3))
            : null;
    }

    /// <summary>字符二元组重叠度：照片文本里有多少二元组出现在部位内容中。</summary>
    private static double Overlap(string a, string b)
    {
        if (a.Length < 2 || b.Length < 2) return 0;
        int hit = 0, total = a.Length - 1;
        for (int i = 0; i + 1 < a.Length; i++)
        {
            var g = a.Substring(i, 2);
            if (b.Contains(g)) hit++;
        }
        return (double)hit / total;
    }

    /// <summary>关键词共现率：部位关键词在照片和部位内容中同时出现的比例。</summary>
    private static double KeywordCoOccurrence(string photo, string partContent)
    {
        if (string.IsNullOrWhiteSpace(photo) || string.IsNullOrWhiteSpace(partContent)) return 0;
        var keywords = new[] { "激光", "打标", "二维码", "拉力", "测试", "模具", "BOM", "编号",
            "生产", "日期", "流水", "供应商", "线控器", "液晶", "效果", "图纸" };
        int hit = 0, total = 0;
        foreach (var kw in keywords)
        {
            if (photo.Contains(kw) && partContent.Contains(kw)) hit++;
            if (photo.Contains(kw) || partContent.Contains(kw)) total++;
        }
        return total > 0 ? (double)hit / total : 0;
    }

    /// <summary>归一化编辑距离相似度（0~1）。</summary>
    private static double Similarity(string a, string b)
    {
        if (a == b) return 1;
        if (a.Length == 0 || b.Length == 0) return 0;
        var dist = Levenshtein(a, b);
        return 1.0 - (double)dist / Math.Max(a.Length, b.Length);
    }

    private static int Levenshtein(string a, string b)
    {
        var d = new int[a.Length + 1, b.Length + 1];
        for (int i = 0; i <= a.Length; i++) d[i, 0] = i;
        for (int j = 0; j <= b.Length; j++) d[0, j] = j;
        for (int i = 1; i <= a.Length; i++)
            for (int j = 1; j <= b.Length; j++)
            {
                var cost = a[i - 1] == b[j - 1] ? 0 : 1;
                d[i, j] = Math.Min(Math.Min(d[i - 1, j] + 1, d[i, j - 1] + 1), d[i - 1, j - 1] + cost);
            }
        return d[a.Length, b.Length];
    }

    // ================= 新版：小图块内容匹配（M2） =================
    // 思路（用户给定，为准）：
    //   1) 先识别上传照片的内容（OCR → 内容签名，忽略尺寸/部位标注）
    //   2) 与「已选图纸」切割出的小图块逐一比对内容签名
    //   3) 内容重合度最高的小图块即命中；都低于阈值 → 报错（不瞎猜）

    private static readonly JsonSerializerOptions _jsonOpts = new() { PropertyNameCaseInsensitive = true };

    private static readonly HashSet<string> PartLabelStopwords = new(StringComparer.OrdinalIgnoreCase)
    {
        "下盖","上盖","上端","下端","左端","右端","顶部","底部","背面","正面","左侧","右侧","侧面",
        "技术要求","序号","名称","材料","备注","比例","单位","图号","设计","审核","批准","制图",
        "共","第","页","日期","版本","数量","重量","未注","公差","粗糙度","标记","处数","阶段",
        "更改","签名","质量","标准","说明","检验","审查","型号","规格","项目"
    };

    // 整行是尺寸标注：纯数字、或带单位/符号（R Ø Φ M ± ° × / 等）
    private static readonly Regex DimLineRegex = new(
        @"^[\d][\d.,]*\s*([RØΦMmcmkg°±×/]\S*)?$|[±×/°]",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    private static bool IsDimensionLine(string line) => DimLineRegex.IsMatch(line);

    /// <summary>从文本抽取「内容签名」：编码/型号/品牌(字母数字码) + 中文 2-gram。
    /// 预先剔除「尺寸标注」整行与「部位标注」停用词，只保留可比对的内容本身。</summary>
    public static List<string> ExtractContentSignatures(string? text)
    {
        var sigs = new List<string>();
        if (string.IsNullOrWhiteSpace(text)) return sigs;

        // 1) 字母数字码（型号/品牌/流水号等）—— 语言无关，最稳的信号
        sigs.AddRange(ModelParser.ExtractCodes(text));

        // 2) 中文内容 2-gram：先剔除尺寸行与部位标注词，再取相邻二字对
        var cleaned = StripDimensionAndLabels(text);
        for (int i = 0; i + 1 < cleaned.Length; i++)
            sigs.Add(cleaned.Substring(i, 2));

        return sigs.Distinct(StringComparer.OrdinalIgnoreCase).ToList();
    }

    private static string StripDimensionAndLabels(string text)
    {
        var sb = new StringBuilder();
        foreach (var raw in text.Split('\n', '\r'))
        {
            var line = ModelParser.Normalize(raw);
            if (line.Length == 0) continue;
            if (IsDimensionLine(line)) continue;       // 整行是尺寸标注 → 跳过
            var s = line;
            foreach (var w in PartLabelStopwords) s = s.Replace(w, "");  // 去掉部位标注词
            sb.Append(s);
        }
        return sb.ToString();
    }

    private static bool IsGram(string s)
        => s.Length == 2 && s.All(c => c >= '\u4e00' && c <= '\u9fff');

    /// <summary>照片签名 vs 小图块签名 的匹配得分（v3 — 返回详细命中类型供门控使用）。
    ///
    /// 评分优先级（从高到低）：
    ///   1. 唯一标识符命中：≥6位纯数字串（电话/序列号等）在双方都出现 → 极强信号(+0.55)
    ///   2. 型号前缀模糊命中：PC-P1HJQ 类模式在双方出现（容许编辑距离≤2）→ 强信号(+0.30)
    ///   3. 通用编码重合：其他字母数字编码重叠 → 中等信号(+0.15)
    ///   4. 中文 2-gram 重合率 → 弱信号(×0.15)，防止大段通用文字淹没精确匹配
    ///   5. 聚焦加分：候选块签名越少（内容越聚焦），在分数接近时略优</summary>
    private static (double Score, bool CodeHit, double GramScore, bool UniqueIdHit, bool ModelFuzzyHit, bool EngraveHit) ScoreAgainstV3(
        List<string> photo, List<string> block)
    {
        if (photo.Count == 0) return (0, false, 0, false, false, false);
        var setB = new HashSet<string>(block, StringComparer.OrdinalIgnoreCase);

        // ---- 1. 唯一标识符：≥6位数字串（电话号码/序列号/日期码） ----
        //     精确相等 或 前缀/包含重叠（如 OCR 把 4008601111 揉成 400860 时仍能命中）。
        bool uniqueIdHit = false;
        var photoDigits = photo.Where(s => s.Length >= 6 && s.All(char.IsDigit)).ToList();
        var blockDigits = block.Where(s => s.Length >= 6 && s.All(char.IsDigit)).ToList();
        if (photoDigits.Count > 0 && blockDigits.Count > 0)
        {
            bool exact = photoDigits.Any(d => blockDigits.Contains(d, StringComparer.OrdinalIgnoreCase));
            bool overlap = photoDigits.Any(pd => blockDigits.Any(bd =>
                bd.Length >= 6 && (bd.Contains(pd, StringComparison.OrdinalIgnoreCase)
                               || pd.Contains(bd, StringComparison.OrdinalIgnoreCase)
                               || LongestCommonSubstring(bd, pd) >= 6)));   // OCR 揉字时仍能命中（如 4008601111 vs 00580111 共享 0860111）
            uniqueIdHit = exact || overlap;
        }

        // ---- 2. 型号前缀模糊匹配（如 PC-P1HJQ ≈ PO-PIHJO）----
        bool modelFuzzyHit = false;
        // 照片侧放宽：取「len≥4 且含字母」的 token（不强制先像型号，因为 OCR 常把 PC-P1HJQ 揉成 pO-PIHIG 而丢失数字/前缀）
        var photoModelCandidates = photo.Where(s => s.Length >= 4 && s.Any(char.IsLetter)).ToList();
        var blockModelLike = block.Where(IsModelLike).ToList();
        if (photoModelCandidates.Count > 0 && blockModelLike.Count > 0)
        {
            foreach (var pm in photoModelCandidates)
                foreach (var bm in blockModelLike)
                    if (EditDistance(pm, bm) <= Math.Max(3, pm.Length / 4))
                        { modelFuzzyHit = true; goto ModelDone; }
        }
        ModelDone:

        // ---- 3. 通用编码重合 ----
        var photoCodes = photo.Where(IsCodeLike).ToList();
        var blockCodes = block.Where(IsCodeLike).ToList();
        bool codeHit = photoCodes.Count > 0 &&
            photoCodes.Any(c => blockCodes.Contains(c, StringComparer.OrdinalIgnoreCase));

        // ---- 4. 中文 2-gram 重合 ----
        int gramHit = 0, gramTotal = 0;
        foreach (var s in photo)
        {
            if (IsGram(s))
            {
                gramTotal++;
                if (setB.Contains(s, StringComparer.OrdinalIgnoreCase)) gramHit++;
            }
        }
        double gramScore = gramTotal > 0 ? (double)gramHit / gramTotal : 0;

        // ---- 4.5 激光打标关键词互证（新增，对齐需求①）----
        //   照片与图块共享「打标关键词 token」（英文≥3字母 / 中文2-gram），
        //   容许编辑距离≤2 的近似（OCR 把 VOLTAGE 揉花时仍能命中）。
        //   命中即强信号：词表内关键词 +0.35，通用词 +0.20，模糊近似 +0.25。
        bool engraveHit = false;
        double engraveScore = 0;
        var pe = EngraveTokens(photo);
        var be = EngraveTokens(block);
        foreach (var t in pe)
        {
            if (be.Contains(t, StringComparer.OrdinalIgnoreCase))
            {
                engraveHit = true;
                engraveScore = Math.Max(engraveScore, EngraveVocab.Contains(t) ? 0.35 : 0.20);
            }
            else
            {
                var fm = be.FirstOrDefault(b => b.Length >= 4 && b.Any(char.IsLetter)
                    && EditDistance(t, b) <= 2);
                if (fm != null) { engraveHit = true; engraveScore = Math.Max(engraveScore, 0.25); }
            }
        }

        // ---- 综合得分 ----
        double score = 0;
        score += engraveScore;
        if (uniqueIdHit) score += 0.55;     // 唯一ID → 决定性
        if (modelFuzzyHit) score += 0.30;    // 型号匹配 → 很强
        if (codeHit) score += 0.15;          // 通用编码 → 辅助
        score += gramScore * 0.15;           // 文字重合（降权到 0.15）

        // 聚焦加分：块签名越少越聚焦（但不超过 0.05 的微调）
        if (block.Count > 0 && block.Count <= 30) score += 0.03;

        // 无强信号封顶：垃圾 OCR（无任何可恢复的型号/编号/打标词信号）不应压过真命中块。
        //   强信号 = modelFuzzyHit || uniqueIdHit || engraveHit；无则最多 partial（<0.70）。
        if (!uniqueIdHit && !modelFuzzyHit && !engraveHit && score > 0.69)
            score = 0.69;

        return (Math.Min(score, 1.0), codeHit, gramScore, uniqueIdHit, modelFuzzyHit, engraveHit);
    }

    /// <summary>v2 兼容旧调用（保留不删，仅 MatchBlock 改用 v3）。</summary>
    private static (double Score, bool CodeHit, double GramScore) ScoreAgainst(List<string> photo, List<string> block)
    {
        var (s, ch, gs, _, _, _) = ScoreAgainstV3(photo, block);
        return (s, ch, gs);
    }

    /// <summary>判断是否像型号编码（含字母且长度 ≥ 6，或符合 PC-/QHR 等已知前缀）</summary>
    private static bool IsModelLike(string s)
    {
        if (string.IsNullOrWhiteSpace(s) || s.Length < 4) return false;
        bool hasLetter = s.Any(char.IsLetter);
        bool hasDigit = s.Any(char.IsDigit);
        // ① 型号最常见形态：字母+数字混合
        if (hasLetter && hasDigit) return true;
        // ② 已知产品前缀开头的字母串（如 PC-PHJQ、QHRxx）。
        //    OCR 常把型号里的数字揉成纯字母（PC-P1HJQ → PC-PHJQ），
        //    若强制要求含数字会把这类真型号漏掉，使「型号模糊匹配」整条失效。
        //    故对「前缀已知」的纯字母型号也认定为型号。
        if (hasLetter && Regex.IsMatch(s,
                @"^(PC|QHR|HITACHI|SHINTECH|POWERLINE)[-A-Za-z]*$",
                RegexOptions.IgnoreCase))
            return true;
        return false;
    }

    // ================= 激光打标内容专项匹配（需求①：识别的是激光打标文字/符号/图案） =================
    // 用户明确：识别内容都是激光打标出来的（白/灰基材上的灰度文字、警示语、电压/型号标注等），
    // 这类内容往往「不是型号、也不是电话长数字」，旧门控(≥6位数字 / 型号前缀)会把它们全拒掉。
    // 故新增一套「打标关键词」体系：质量门控放行 + 评分加权，使 LOW VOLTAGE / 禁止强电 / DC15-24V
    // 这类激光打标内容也能被匹配到对应图纸小图块。
    /// <summary>激光打标常见「高价值关键词」词表（中英混合）。
    /// 命中即视为「这张照片确实拍到了有意义的打标内容」，放行质量门控。</summary>
    private static readonly HashSet<string> EngraveVocab = new(StringComparer.OrdinalIgnoreCase)
    {
        // ---- 英文：电压/电流/电源/接口类 ----
        "VOLTAGE","VOLT","LOW","HIGH","DC","AC","INPUT","OUTPUT","POWER","SUPPLY",
        "VOL","AMP","CURRENT","MAX","MIN","ON","OFF","GND","GROUND","POS","NEG",
        // ---- 英文：认证/型号/标识类 ----
        "MODEL","CE","ROHS","FCC","UL","GS","TUV","PSE","KC","EMC","IP","ID","SN",
        "SERIAL","PART","PN","NO","REV","VER","HW","SW","FW","TYPE","LOT","DATE",
        "MFR","MFD","EXP","QTY","MADE","CHINA","QR","BAR","CODE","TEST","PASS","FAIL",
        "WARN","CAUTION","DANGER","LED","USB","RESET","WIFI","BLUETOOTH",
        // ---- 中文：警示/电压/服务类（作为 2-gram 出现在签名中）----
        "禁止","强电","弱电","危险","警告","注意","电压","电流","功率","接地",
        "输入","输出","正极","负极","服务","热线","规格","生产","日期","批号",
        "二维码","条码","型号"
    };

    /// <summary>从内容签名中抽取「打标关键词 token」：
    ///  - 英文：含字母且长度≥3 的码（大写归一），如 VOLTAGE / LOW / MODEL；
    ///  - 中文：2-gram（已在 ExtractContentSignatures 中剔除尺寸/部位停用词）。
    /// 这些 token 是照片↔图块互证的核心信号。</summary>
    private static List<string> EngraveTokens(List<string> sigs)
    {
        var outp = new List<string>();
        if (sigs == null) return outp;
        foreach (var s in sigs)
        {
            if (s.Length >= 2 && s.All(c => c >= '\u4e00' && c <= '\u9fff'))
                outp.Add(s);                                   // 中文 2-gram
            else if (s.Length >= 3 && s.Any(char.IsLetter))
                outp.Add(s.ToUpperInvariant());               // 英文/混排词
        }
        return outp.Distinct(StringComparer.OrdinalIgnoreCase).ToList();
    }

    // ================= 任务#50：标准表格块显式过滤 =================
    // docx 明确要求："带标准表格的可以舍弃，标注类的线段可以忽略"。
    // 图纸中的「标准表格」= BOM表、模具编号、技术要求、标题栏、明细表、修订记录等——
    // 这些是图纸的"装饰性家具"，不是激光打标内容区域。参与匹配只会制造噪声假阳性。
    //
    // 判定规则：
    //   ① 强标记器命中任一即判为表格块（BOM / 模具编号 / 技术要求 / 标题栏 / 明细表）
    //   ② 弱标记器命中 ≥3 个不同词也判为表格块（序号+名称+材料+备注…这类密集出现）
    // 过滤在 MatchBlock 中执行（不影响切割/存储），被过滤的块不进入评分候选。

    /// <summary>图纸"标准表格/标题栏/BOM表"的典型词汇表（中文为主）。</summary>
    private static readonly HashSet<string> TableHeaderVocab = new(StringComparer.OrdinalIgnoreCase)
    {
        // ---- 强标记（单独命中即判定为表格块）----
        "BOM", "模具编号", "技术要求", "标题栏", "明细表", "明细",
        // ---- 表格列头 / 标题栏常用词（来自 PartLabelStopwords 扩展）----
        "序号","名称","材料","备注","比例","单位","图号","设计","审核","批准","制图",
        "共","第","页","日期","版本","数量","重量","未注","公差","粗糙度","标记",
        "处数","阶段","更改","签名","质量","标准","说明","检验","审查","型号","规格","项目",
        // ---- 部门 / 状态类 ----
        "发放部门","市场部","生产部","采购部","修订"
    };

    /// <summary>判断一个图块的 OCR 原始文本是否属于"标准表格/家具块"（应从匹配候选中剔除）。
    ///
    /// 判定策略（对齐 docx "带标准表格的可以舍弃"）：
    ///   - 强标记命中（BOM/模具编号/技术要求等）→ 直接判定为表格
    ///   - 弱标记命中 ≥3 个不同词汇 → 高概率是表格/标题栏
    ///   使用 ContainsFuzzy 容忍 OCR 单字误识（如 盖→盎、注→主 等）</summary>
    private static bool IsTableFurnitureBlock(string rawText)
    {
        if (string.IsNullOrWhiteSpace(rawText)) return false;

        // 强标记：任一命中 → 立刻判定为家具表格块
        string[] strongMarkers = { "BOM", "模具编号", "技术要求", "标题栏", "明细表" };
        foreach (var m in strongMarkers)
            if (ModelParser.ContainsFuzzy(rawText, m)) return true;

        // 弱标记统计：模糊匹配到的表格词汇数量
        int hits = 0;
        foreach (var w in TableHeaderVocab)
        {
            if (w.Length < 2) continue;           // 单字跳过（太易误命中）
            if (ModelParser.ContainsFuzzy(rawText, w))
                hits++;
        }
        return hits >= 3;                         // ≥3 个不同表格词汇 → 家具块
    }

    /// <summary>Rule 2 增强：判断是否为非工程视图块（应从匹配中排除）。
    ///
    /// 非工程视图块包括：
    ///   - 标准表格/BOM/标题栏（IsTableFurnitureBlock 已覆盖）
    ///   - 大段文字说明块 (type=text_only)
    ///   - 格式规范/说明块 (type=spec_format)，如"上盖二维码格式"、二维码样例等
    ///   - 纯表格块 (type=table)
    ///
    /// 只有 engineering 和 unknown 类型的块参与匹配。</summary>
    private static bool IsNonEngineeringBlock(BlockRec block, string blockType)
    {
        // 方案 B 正向选择优先：命中打标标识区的块一律保留（即使文本含表格词，如型号在标题栏内）
        if (KeepViewBlock(block)) return false;

        // 类型标签直接判定
        if (blockType is "table" or "text_only" or "spec_format")
            return true;

        // Rule 2 兜底：无论 type 标签如何，OCR 文本命中即判为非工程视图块。
        // （切割时部分块被误标为 engineering，必须在匹配候选池入口再拦一道）
        var raw = block.RawText ?? "";
        if (!string.IsNullOrWhiteSpace(raw))
        {
            // ① 标准表格/标题栏/BOM（IsTableFurnitureBlock 已用模糊匹配覆盖 BOM/模具编号/技术要求等）
            if (IsTableFurnitureBlock(raw)) return true;
            // ② 二维码格式块（Rule 2："没有数字标线标示长度的图块"；二维码作图标识别、非文字匹配内容）
            if (ModelParser.ContainsFuzzy(raw, "二维码") || ModelParser.ContainsFuzzy(raw, "二维码格式") || ModelParser.ContainsFuzzy(raw, "上盖二维码"))
                return true;
        }

        // ③ 大段文字说明块（q-1）：token 极多且为说明性文字（部门/技术要求等），非激光打标内容
        //     token 数阈值取 100（JQ 的"发放部门/技术要求"整段约 191~229 token）；
        //     并至少含 2 个部门/说明类词，避免把"多 token 的型号块"误杀。
        if (!string.IsNullOrWhiteSpace(raw))
        {
            int tokCount = 0;
            try { tokCount = (JsonSerializer.Deserialize<List<string>>(block.Tokens) ?? new()).Count; } catch { }
            if (tokCount >= 100)
            {
                string[] deptWords = { "发放部门", "市场部", "生产部", "采购部", "技术要求", "说明" };
                int deptHits = deptWords.Count(w => ModelParser.ContainsFuzzy(raw, w));
                if (deptHits >= 2) return true;
            }
        }

        // engineering / unknown → 保留参与匹配
        return false;
    }

    /// <summary>新切割规则（R1/R2/R4/R7）：保留"打标标识区"与"带数字标线的工程视图"块，
    /// 舍弃表格 / 大段文字 / 格式块 / 无标线空白块。
    /// 判定 = 文本特征 + 位置/几何结合：
    ///   - 几何优先：分类为 engineering（带尺寸线视图）无条件保留（R1，不依赖 OCR 文本，避免视图 OCR 漏读被误舍）
    ///   - 文本优先：含打标标识文本（型号/服务热线/二维码+型号）→ 保留（R4）
    ///   - 纯表格/大段文字/格式块（type 标签）→ 直接舍弃（R7/R2）
    ///   - 其余未知块：含数字标线（尺寸标注）→ 工程视图，保留（R1 兜底）</summary>
    private static bool KeepViewBlock(BlockRec block)
    {
        var raw = block.RawText ?? "";
        var type = block.BlockType ?? "";
        // ⭐ 描述性二维码格式说明块严格舍弃（前置拦截）：
        //   图纸上"盖底部二维码标签激光打标格式：QHRLA 厂商制造编码..."等示例说明块，
        //   非产品面板真实打标区，但其内含二维码图形会干扰图标位置比对。
        //   即使被 ONNX 误标为 engineering(image)，也必须在此拦截——原 R1 几何优先分支
        //   会直接保留 engineering 块，导致漏网进匹配。
        if (IsDescriptiveQrFormatBlock(raw)) return false;
        // R1（几何判据）：分类为工程视图（带尺寸线）的块，无条件保留。
        //   标准 R1 按几何（带尺寸线=视图）留，不依赖 OCR 文本——避免视图 OCR 没读全尺寸数字被误舍（假阴性）。
        if (type == "engineering") return true;
        if (string.IsNullOrWhiteSpace(raw)) return false;
        // 文本优先：命中打标标识区的块一律保留（即使 type 标签为表格，如型号在标题栏内）
        if (HasLabelingText(raw)) return true;
        // R7/R2：纯表格 / 大段文字 / 格式块 → 直接舍弃
        if (type is "table" or "text_only" or "spec_format") return false;
        // 工程视图（带数字标线）→ 保留（R1 兜底）
        return HasDimensionNumbers(raw);
    }

    /// <summary>数字标线：尺寸标注行（工程视图特征）。包容 OCR 噪声：
    ///   - 强信号：尺寸符号 ± / Φ / ⌀ / Ø 后接数字（如 ±0.5、Φ12、⌀8、Ø20）
    ///   - 强信号：数字 ± 数字（如 6±0.5、74±0.5）
    ///   - 弱信号(兜底)：疑似测量值——含小数(12.5) 或 4+位整数(0745/1250) 或带单位(mm/cm)</summary>
    private static bool HasDimensionNumbers(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return false;
        // 强信号：尺寸符号（±/Φ/⌀/Ø）后接数字 → 工程视图标注（不含普通字母 R，避免 R12 电阻误判）
        if (Regex.IsMatch(raw, @"[±Φ⌀Ø][\s]*[0-9]")) return true;
        // 强信号：数字 ± 数字（如 6±0.5、74±0.5）
        if (Regex.IsMatch(raw, @"[0-9]+[\s]*[±][\s]*[0-9]")) return true;
        // 弱信号(兜底)：测量值形态——小数 / 4+位整数 / 带单位，收窄以避免年份·页码·零件号误留
        if (Regex.IsMatch(raw, @"(?:\d+\.\d+|\d{4,}|\d+\s*(?:mm|cm))")) return true;
        return false;
    }

    /// <summary>打标标识文本：型号 / 服务热线 / 二维码+型号（R5 二维码作图标，但"二维码+型号"的块仍属打标区）。
    /// 与旧过滤器同义，现仅作"保留"判据之一（文本优先）。</summary>
    private static bool HasLabelingText(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return false;
        return ContainsModelIdentifier(raw)
            || raw.Contains("服务热线")
            || Regex.IsMatch(raw, @"400[-]?\d{7}")
            || (ContainsQrMarker(raw) && ContainsModelIdentifier(raw));
    }

    private static bool ContainsQrMarker(string raw)
        => ModelParser.ContainsFuzzy(raw, "二维码") || ModelParser.ContainsFuzzy(raw, "QR")
           || ModelParser.ContainsFuzzy(raw, "NFC") || raw.Contains("扫码");

    /// <summary>描述性二维码格式说明块检测：图纸上"二维码标签激光打标格式"等示例说明块
    /// （列出厂商制造编码/供应商代码/流水号/生产日期代码），非产品面板真实打标区，应严格舍弃。
    /// 判定：命中任一描述性短语 AND 不含真实面板打标内容（LOW VOLTAGE/禁止强电/地暖阀/DC15/24V/
    /// 服务热线/400电话）。注：不能用型号码(QHR/QHS/YCWA)区分——格式示例块里也有样品型号。</summary>
    private static bool IsDescriptiveQrFormatBlock(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return false;
        string[] descPhrases = {
            "二维码标签激光打标格式", "二维码标签打印格式", "二维码格式",
            "标签打印格式", "标签激光打标格式",
            "此处激光打标二维码", "二维码内容以实际生产为准", "此处激光打标二维"
        };
        bool hasDesc = descPhrases.Any(p => raw.Contains(p));
        if (!hasDesc) return false;
        // 含真实面板打标内容 → 是真实打标区（如"下盖此处激光打标二维码 + LOWVOLTAGE/禁止强电/地暖阀"），保留
        string[] realMarking = {
            "LOW VOLTAGE", "LOWVOLTAGE", "禁止强电", "禁止塑电",
            "地暖阀", "DC15/24V", "DC15-24V", "服务热线", "400-", "4006", "4008", "400620"
        };
        bool hasReal = realMarking.Any(k => raw.Contains(k));
        return !hasReal;
    }

    /// <summary>型号标识识别：
    ///   - 品牌词（模糊）：HITACHI / HYXC
    ///   - 型号码模式：2+ 字母，可选连字符，后接含数字的字母数字串
    ///     （包容 OCR 噪声：pC-P1HVQ / PC-P1HVO / HYXC-123 均命中；Fig-1 / No-2 这类过短不命中）</summary>
    private static bool ContainsModelIdentifier(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return false;
        foreach (var brand in new[] { "HITACHI", "HYXC" })
            if (ModelParser.ContainsFuzzy(raw, brand)) return true;
        // 宽松型号码：去连字符后至少 5 字符、必须含数字、字母开头
        foreach (Match m in Regex.Matches(raw, @"[A-Za-z]{2,}-?[A-Za-z]*[0-9][A-Za-z0-9]*"))
        {
            if (m.Value.Replace("-", "").Length >= 5) return true;
        }
        return false;
    }

    /// <summary>简单编辑距离（Levenshtein），用于型号模糊比较</summary>
    private static int EditDistance(string a, string b)
    {
        int la = a.Length, lb = b.Length;
        if (la == 0) return lb;
        if (lb == 0) return la;
        var d = new int[la + 1, lb + 1];
        for (int i = 0; i <= la; i++) d[i, 0] = i;
        for (int j = 0; j <= lb; j++) d[0, j] = j;
        for (int i = 1; i <= la; i++)
            for (int j = 1; j <= lb; j++)
            {
                int cost = char.ToLowerInvariant(a[i - 1]) == char.ToLowerInvariant(b[j - 1]) ? 0 : 1;
                d[i, j] = Math.Min(Math.Min(d[i - 1, j] + 1, d[i, j - 1] + 1), d[i - 1, j - 1] + cost);
            }
        return d[la, lb];
    }

    /// <summary>最长公共子串长度（用于 OCR 揉字后的数字串互证）。</summary>
    private static int LongestCommonSubstring(string a, string b)
    {
        int la = a.Length, lb = b.Length, best = 0;
        var dp = new int[la + 1, lb + 1];
        for (int i = 1; i <= la; i++)
            for (int j = 1; j <= lb; j++)
            {
                if (char.ToLowerInvariant(a[i - 1]) == char.ToLowerInvariant(b[j - 1]))
                {
                    dp[i, j] = dp[i - 1, j - 1] + 1;
                    if (dp[i, j] > best) best = dp[i, j];
                }
            }
        return best;
    }

    // 2-char 纯中文 → 视为内容 2-gram；其余（含字母数字）视为编码类
    private static bool IsCodeLike(string s)
        => !(s.Length == 2 && s.All(c => c >= '\u4e00' && c <= '\u9fff'));

    // ---------- 切割块缓存（懒加载，不破坏原 PDF） ----------
    public record BlockRec(int BIdx, int X, int Y, int W, int H, string FileRel, string Tokens, string RawText, string BlockType = "");
    public record BlockHit(int Idx, double Score, string ImageUrl, int X = 0, int Y = 0, int W = 0, int H = 0);
    public record BlockMatchResult(
        int DrawingId, string? Model, string? FileName, string? VirtualPath,
        double Score, string? PhotoText, string? BlockImageUrl, bool Matched,
        List<BlockHit> Candidates, string? QualityError = null,
        string MatchStatus = "",          // "full" | "partial" | "none"
        List<string>? MismatchedTokens = null,  // photo tokens not found in best block (for partial)
        DiffMarkers? Diff = null,                // 差异标记（后台自动计算，无需二次调用）
        string? PhotoImageUrl = null              // 实际产品照片 URL（H5 兜底显示，不依赖 diff）
    );

    /// <summary>差异标记数据（与匹配结果一并返回）</summary>
    public record DiffMarkers(
        List<DiffRegion> RedRegions,      // 🔴 图块有但照片无
        List<DiffRegion> YellowRegions,   // 🟡 照片有但图块无
        List<DiffRegion> GrayRegions,     // ⚪ CAD 标注（已排除）
        List<DiffRegion> GreenRegions,    // 🟢 一致
        List<string> PhotoExclusive,      // 照片独有文本
        List<string> BlockExclusive,      // 图块独有文本
        List<string> CadOnly,             // CAD 标注文本
        string? MarkedImageUrl,           // 标记图 URL
        List<DiffRegion> IconRegions,     // 🔮 图标区域（QR/NFC/非文字小块，紫色框=到位）
        List<DiffRegion> IconMissingRegions, // 🔴 块有照片无（缺图标，红框）
        List<DiffRegion> IconExtraRegions,   // 🟡 照片有块无（多图标，仅计数）
        List<CharDiff> MissingChars = null,  // 块中缺失的具体字符（字符级 diff）
        List<CharDiff> ExtraChars = null     // 照片中多出的具体字符
    );

    /// <summary>字符级 diff：块中缺失/照片中多出的具体字符</summary>
    public record CharDiff(string BlockText, List<string> PhotoTexts, string Missing, List<int>? Positions);

    // DrawingManifest / BlockEntry 反序列化模型（与 segment_blocks.py 输出对齐）
    public record BlockEntry(int Idx, int X, int Y, int W, int H, string File, string Type = "");
    public record DrawingManifest(int drawingId, string source, int width, int height, List<BlockEntry> blocks);

    // 小于该尺寸的图块视为噪声碎片（标注线/噪点），不入库、不参与匹配，提升精度。
    private const int MinBlockW = 70, MinBlockH = 70;
    private string SegDir(int drawingId) => Path.Combine(_dataDir, "seg", drawingId.ToString());
    private string BlockImageUrl(int drawingId, int idx) => $"api/drawings/block-image?id={drawingId}&idx={idx}";

    private bool BlocksCached(int drawingId)
        => File.Exists(Path.Combine(SegDir(drawingId), ".done"));

    /// <summary>懒加载入口（带 force 重切 + 内存缓存刷新）。
    /// force=true 时删除 .done 缓存标记与内存块缓存，触发「重新切割+OCR+入库」。</summary>
    public void EnsureSegmented(int drawingId, bool force = false)
    {
        if (force)
        {
            try { File.Delete(Path.Combine(SegDir(drawingId), ".done")); } catch { }
            _blockCache.TryRemove(drawingId, out _);
            // A2：清该图纸的块 bbox 内存缓存（DB 行在重切时 DELETE+INSERT 重置为 NULL）
            foreach (var k in _blockBboxCache.Keys.Where(k => k.StartsWith(drawingId + "_")).ToList())
                _blockBboxCache.TryRemove(k, out _);
            _photoCache.Clear(); // 块变了，照片结果缓存整体失效（量小，全清最稳）
        }
        EnsureSegmentedCore(drawingId);
        // 重新预热该图纸的块到内存
        _blockCache.TryRemove(drawingId, out _);
        GetBlocks(drawingId);
    }

        /// <summary>幂等迁移 drawing_blocks 表（Type/BBox/Icons 列）。
        /// 必须在「缓存早退」之前调用，否则已切割图纸永不补齐新列，导致持久化静默失败。</summary>
        private void EnsureBlockSchema()
        {
            // v4: 兼容旧库 —— 新增 drawing_blocks.Type 列（块类型分类结果）。
            try
            {
                using var mig = _db.Open().CreateCommand();
                mig.CommandText = "ALTER TABLE drawing_blocks ADD COLUMN Type TEXT;";
                mig.ExecuteNonQuery();
            }
            catch { /* 列已存在则忽略 */ }
            // A2: 新增 BBox / Icons 列（差异标记所需块 bbox 与图标框的持久化缓存）
            try
            {
                using var mig = _db.Open().CreateCommand();
                mig.CommandText = "ALTER TABLE drawing_blocks ADD COLUMN BBox TEXT;";
                mig.ExecuteNonQuery();
            }
            catch { /* 列已存在则忽略 */ }
            try
            {
                using var mig = _db.Open().CreateCommand();
                mig.CommandText = "ALTER TABLE drawing_blocks ADD COLUMN Icons TEXT;";
                mig.ExecuteNonQuery();
            }
            catch { /* 列已存在则忽略 */ }
        }

        /// <summary>懒加载：切割图纸 + 逐块 OCR + 内容签名入库（缓存标记 .done 防重跑）。原 PDF 不动。</summary>
        private void EnsureSegmentedCore(int drawingId)
        {
            EnsureBlockSchema();   // 先迁移表结构（幂等），再判断缓存
            if (BlocksCached(drawingId)) return;
            var segDir = SegDir(drawingId);
            Directory.CreateDirectory(segDir);

            var d = GetDrawing(drawingId);
        if (d == null) { try { File.WriteAllText(Path.Combine(segDir, ".done"), "empty"); } catch { } return; }
        var phys = _files.ResolvePhysical(d.VirtualPath);
        if (phys == null || !File.Exists(phys)) { try { File.WriteAllText(Path.Combine(segDir, ".done"), "empty"); } catch { } return; }

        // T-D3 收尾修复：segment_blocks.py 顶层 import fitz(PyMuPDF, 原生 C 扩展)，
        // 必须在「干净环境」下启动，否则继承 ASP.NET 宿主被污染的 PATH / PYTHONPATH，
        // 在原生 DLL 加载阶段静默崩溃(exit=1、无 stdout) → 空 JSON → .done=nojson → 提前 return。
        // 复用 OcrService.CreatePythonPsi（与 BatchOcr 同一套干净环境构造），并改异步排空
        // stdout/stderr，规避「先读 stdout 阻塞到 EOF、子进程因 stderr 缓冲满卡写」死锁。
        var psi = _ocr.CreatePythonPsi(_segmentScript,
            "segment", phys, drawingId.ToString(), segDir, "200");

        var jsonText = "";
        var segErr = "";
        try
        {
            using var p = Process.Start(psi)!;
            var outTask = p.StandardOutput.ReadToEndAsync();
            var errTask = p.StandardError.ReadToEndAsync();
            // 纪律修复（MEMORY 续14）：绝不强杀切图子进程。强杀 Paddle 子进程会毒化
            // Paddle 原生推理引擎会话、导致其之后每次 create_predictor 都确定性崩溃
            // 直到重启。worker 自身带 100s 硬超时看门狗（segment_blocks.py main）会自行
            // 退出（早于本 120s），故此处只等待、不 Kill；即便超时未退也仅视为失败走
            // .done=nojson 兜底，绝不 p.Kill()（否则毒化需重启才能恢复）。
            p.WaitForExit(120_000);
            Task.WaitAll(outTask, errTask);
            jsonText = outTask.Result ?? "";
            segErr = errTask.Result ?? "";
        }
        catch { try { File.WriteAllText(Path.Combine(segDir, ".done"), "failed"); } catch { } return; }

        DrawingManifest? man = null;
        try { man = JsonSerializer.Deserialize<DrawingManifest>(jsonText, _jsonOpts); }
        catch (Exception ex)
        {
            try { File.WriteAllText(Path.Combine(segDir, "deser_err.txt"),
                ex.GetType().Name + ": " + ex.Message
                + "\nSTDERR=" + (segErr?.Length > 2000 ? segErr[..2000] : segErr)
                + "\nJSON_HEAD=" + (jsonText?.Length > 2000 ? jsonText[..2000] : jsonText)); } catch { }
            man = null;
        }
        if (man?.blocks == null) { try { File.WriteAllText(Path.Combine(segDir, ".done"), "nojson"); } catch { } return; }

        // 仅对「尺寸达标」的图块做 OCR + 入库；过小的碎片（噪点/标注线）直接丢弃，
        // 既提升匹配精度，也避免无意义内容制造假阳性。
        var keep = man.blocks
            .Where(b => b.W >= MinBlockW && b.H >= MinBlockH)
            .ToList();
        var pngPaths = keep
            .Select(b => Path.Combine(segDir, b.File))
            .Where(File.Exists)
            .ToList();
        // 一次 Python 进程批量 OCR（规避 .NET 反复拉起 Tesseract 原生进程的退化）。
        var ocrMap = pngPaths.Count > 0
            ? _ocr.BatchOcr(pngPaths)
            : new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        // ── 方案 B：正向选择，只保留"打标标识区"块 ──
        // 严格零候选（用户确认）：若没有任何块含型号标识，则该图候选为空，不降级保留 engineering 块。
        var labeling = new List<(BlockEntry b, string ocr)>();
        foreach (var b in keep)
        {
            var pngPath = Path.Combine(segDir, b.File);
            ocrMap.TryGetValue(pngPath, out var ocrText);
            ocrText = ocrText ?? "";
            var rec = new BlockRec(b.Idx, b.X, b.Y, b.W, b.H, b.File, "[]", ocrText, b.Type ?? "");
            if (KeepViewBlock(rec)) labeling.Add((b, ocrText));
        }
        // 删除被舍弃块的磁盘 PNG（使"切出来的图"在磁盘 = 只留打标区，符合标准 P1）
        var keptFiles = new HashSet<string>(labeling.Select(x => x.b.File), StringComparer.OrdinalIgnoreCase);
        foreach (var b in keep)
        {
            if (!keptFiles.Contains(b.File))
            {
                var discardPath = Path.Combine(segDir, b.File);
                try { if (File.Exists(discardPath)) File.Delete(discardPath); } catch { }
            }
        }

        var now = DateTime.Now.ToString("o");
        using var conn = _db.Open();
        using (var del = conn.CreateCommand())
        {
            del.CommandText = "DELETE FROM drawing_blocks WHERE DrawingId=$id";
            del.Parameters.AddWithValue("$id", drawingId);
            del.ExecuteNonQuery();
        }
        using var tx = conn.BeginTransaction();
        foreach (var (b, ocrText) in labeling)
        {
            var sigs = ExtractContentSignatures(ocrText);
            using var ins = conn.CreateCommand();
            ins.CommandText = "INSERT INTO drawing_blocks (DrawingId,BIdx,X,Y,W,H,FileRel,Tokens,RawText,Type,CreatedAt) " +
                              "VALUES ($id,$idx,$x,$y,$w,$h,$f,$t,$r,$type,$ts)";
            ins.Parameters.AddWithValue("$id", drawingId);
            ins.Parameters.AddWithValue("$idx", b.Idx);
            ins.Parameters.AddWithValue("$x", b.X);
            ins.Parameters.AddWithValue("$y", b.Y);
            ins.Parameters.AddWithValue("$w", b.W);
            ins.Parameters.AddWithValue("$h", b.H);
            ins.Parameters.AddWithValue("$f", b.File);
            ins.Parameters.AddWithValue("$t", JsonSerializer.Serialize(sigs));
            ins.Parameters.AddWithValue("$r", ocrText);
            ins.Parameters.AddWithValue("$type", b.Type ?? "");
            ins.Parameters.AddWithValue("$ts", now);
            ins.ExecuteNonQuery();
        }
        tx.Commit();
        try { File.WriteAllText(Path.Combine(segDir, ".done"), "ok:" + labeling.Count); } catch { }
    }

    private List<BlockRec> GetBlocks(int drawingId)
    {
        // 内存缓存命中：免查库+反序列化，毫秒级返回（任务42）
        if (_blockCache.TryGetValue(drawingId, out var cached))
            return cached;

        var list = new List<BlockRec>();
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        c.CommandText = "SELECT BIdx,X,Y,W,H,FileRel,Tokens,RawText,Type FROM drawing_blocks WHERE DrawingId=$id ORDER BY BIdx";
        c.Parameters.AddWithValue("$id", drawingId);
        using var r = c.ExecuteReader();
        while (r.Read())
            list.Add(new BlockRec(r.GetInt32(0), r.GetInt32(1), r.GetInt32(2), r.GetInt32(3), r.GetInt32(4),
                r.GetString(5), r.IsDBNull(6) ? "[]" : r.GetString(6), r.IsDBNull(7) ? "" : r.GetString(7),
                r.IsDBNull(8) ? "" : r.GetString(8)));
        _blockCache[drawingId] = list; // 回填内存缓存
        return list;
    }

    /// <summary>A2：从 DB 读取某块的持久化 bbox(JSON) 与图标框(JSON)。
    /// 命中后可跳过块图 OCR 与图标检测，差异标记彻底免 RapidOCR 模型加载，且重启后仍可用。</summary>
    private (string? BBox, string? Icons) LoadBlockBBoxIcons(int drawingId, int blockIdx)
    {
        try
        {
            using var conn = _db.Open();
            using var c = conn.CreateCommand();
            c.CommandText = "SELECT BBox, Icons FROM drawing_blocks WHERE DrawingId=$id AND BIdx=$idx";
            c.Parameters.AddWithValue("$id", drawingId);
            c.Parameters.AddWithValue("$idx", blockIdx);
            using var r = c.ExecuteReader();
            if (r.Read())
                return (r.IsDBNull(0) ? null : r.GetString(0), r.IsDBNull(1) ? null : r.GetString(1));
        }
        catch { }
        return (null, null);
    }

    /// <summary>A2：持久化某块的 bbox(JSON) 与图标框(JSON) 到 DB。任一项为 null 则保留 DB 中已有值（合并写，避免互相清空）。</summary>
    private void SaveBlockBBoxIcons(int drawingId, int blockIdx, string? bbox, string? icons)
    {
        try
        {
            // 合并：若本次只提供一项，保留另一项已有值
            var (existingBbox, existingIcons) = LoadBlockBBoxIcons(drawingId, blockIdx);
            var finalBbox = bbox ?? existingBbox;
            var finalIcons = icons ?? existingIcons;
            using var conn = _db.Open();
            using var c = conn.CreateCommand();
            c.CommandText = "UPDATE drawing_blocks SET BBox=$bbox, Icons=$icons WHERE DrawingId=$id AND BIdx=$idx";
            c.Parameters.AddWithValue("$bbox", (object?)finalBbox ?? DBNull.Value);
            c.Parameters.AddWithValue("$icons", (object?)finalIcons ?? DBNull.Value);
            c.Parameters.AddWithValue("$id", drawingId);
            c.Parameters.AddWithValue("$idx", blockIdx);
            c.ExecuteNonQuery();
        }
        catch { }
    }

    /// <summary>方案 B 兜底：源 PDF 缺失时，对【已有切块】（DB+磁盘）重新套用 KeepViewBlock 过滤器。
    /// 只保留打标标识区，删除其余块（DB 行 + 磁盘 PNG）。效果与"重切"一致，不依赖源 PDF。
    /// 严格零候选（用户确认）：若没有任何块含型号标识，则该图候选为空。</summary>
    public void RefilterDrawing(int drawingId)
    {
        var segDir = SegDir(drawingId);
        var blocks = GetBlocksDirect(drawingId);   // 直读 DB，避内存缓存陈旧
        if (blocks.Count == 0)
        {
            try { File.WriteAllText(Path.Combine(segDir, ".done"), "refilter:0"); } catch { }
            _blockCache.TryRemove(drawingId, out _);
            return;
        }
        // 正向选择：只留打标区
        var kept = blocks.Where(b => KeepViewBlock(b)).ToList();
        var keptFiles = new HashSet<string>(kept.Select(b => b.FileRel), StringComparer.OrdinalIgnoreCase);

        // 删磁盘上被舍弃块 + 历史孤儿 PNG（仅保留 kept 的基名集，路径无关、对孤儿免疫）
        var blocksDir = Path.Combine(segDir, "blocks");
        var keptBase = new HashSet<string>(kept.Select(b => Path.GetFileName(b.FileRel ?? "")), StringComparer.OrdinalIgnoreCase);
        if (Directory.Exists(blocksDir))
        {
            foreach (var f in Directory.GetFiles(blocksDir, "*_blk_*.png"))
            {
                if (!keptBase.Contains(Path.GetFileName(f)))
                    try { File.Delete(f); } catch { }
            }
        }

        // 重写 DB：只留 kept
        using var conn = _db.Open();
        using (var del = conn.CreateCommand())
        {
            del.CommandText = "DELETE FROM drawing_blocks WHERE DrawingId=$id";
            del.Parameters.AddWithValue("$id", drawingId);
            del.ExecuteNonQuery();
        }
        using var tx = conn.BeginTransaction();
        var now = DateTime.Now.ToString("o");
        foreach (var b in kept)
        {
            using var ins = conn.CreateCommand();
            ins.CommandText = "INSERT INTO drawing_blocks (DrawingId,BIdx,X,Y,W,H,FileRel,Tokens,RawText,Type,CreatedAt) " +
                              "VALUES ($id,$idx,$x,$y,$w,$h,$f,$t,$r,$type,$ts)";
            ins.Parameters.AddWithValue("$id", drawingId);
            ins.Parameters.AddWithValue("$idx", b.BIdx);
            ins.Parameters.AddWithValue("$x", b.X);
            ins.Parameters.AddWithValue("$y", b.Y);
            ins.Parameters.AddWithValue("$w", b.W);
            ins.Parameters.AddWithValue("$h", b.H);
            ins.Parameters.AddWithValue("$f", b.FileRel);
            ins.Parameters.AddWithValue("$t", b.Tokens);
            ins.Parameters.AddWithValue("$r", b.RawText);
            ins.Parameters.AddWithValue("$type", b.BlockType ?? "");
            ins.Parameters.AddWithValue("$ts", now);
            ins.ExecuteNonQuery();
        }
        tx.Commit();

        _blockCache.TryRemove(drawingId, out _);
        // A2：清该图纸的块 bbox 内存缓存（DB 行已被重写，BBox/Icons 重置为 NULL）
        foreach (var k in _blockBboxCache.Keys.Where(k => k.StartsWith(drawingId + "_")).ToList())
            _blockBboxCache.TryRemove(k, out _);
        try { File.WriteAllText(Path.Combine(segDir, ".done"), "refilter:" + kept.Count); } catch { }
    }

    /// <summary>批量重过滤：循环所有图纸执行 RefilterDrawing。返回成功张数。</summary>
    public int RefilterAll(bool force = false)
    {
        var ids = new List<int>();
        using (var conn = _db.Open())
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT Id FROM drawings ORDER BY Id";
            using var r = c.ExecuteReader();
            while (r.Read()) ids.Add(r.GetInt32(0));
        }
        var done = 0;
        foreach (var id in ids)
        {
            try { RefilterDrawing(id); done++; }
            catch { /* 单张失败不影响其它 */ }
        }
        return done;
    }

    /// <summary>直读 DB 切块（避 _blockCache 陈旧），供重过滤使用。</summary>
    private List<BlockRec> GetBlocksDirect(int drawingId)
    {
        var list = new List<BlockRec>();
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        c.CommandText = "SELECT BIdx,X,Y,W,H,FileRel,Tokens,RawText,Type FROM drawing_blocks WHERE DrawingId=$id ORDER BY BIdx";
        c.Parameters.AddWithValue("$id", drawingId);
        using var r = c.ExecuteReader();
        while (r.Read())
            list.Add(new BlockRec(r.GetInt32(0), r.GetInt32(1), r.GetInt32(2), r.GetInt32(3), r.GetInt32(4),
                r.GetString(5), r.IsDBNull(6) ? "[]" : r.GetString(6), r.IsDBNull(7) ? "" : r.GetString(7),
                r.IsDBNull(8) ? "" : r.GetString(8)));
        return list;
    }

    /// <summary>启动预热：把所有图纸一次性切割（若未缓存）+ 载入内存块缓存，
    /// 使后续首拍匹配即命中内存、无需现场等切割。</summary>
    public void WarmupCache()
    {
        var ids = new List<int>();
        using (var conn = _db.Open())
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT Id FROM drawings ORDER BY Id";
            using var r = c.ExecuteReader();
            while (r.Read()) ids.Add(r.GetInt32(0));
        }
        // 1. 预热所有图纸块缓存
        foreach (var id in ids)
        {
            try { EnsureSegmentedCore(id); _blockCache.TryRemove(id, out _); GetBlocks(id); }
            catch { /* 单张失败不影响其它 */ }
        }
        // 2. 预热 OCR worker（L1 引擎锁定：启动即拉起常驻进程 + 引擎 warm-up，首拍跳过冷启动窗口）
        try { _ocr.WarmupWorker(); }
        catch { }
    }

    /// <summary>Python 健康检查：验证 diff_visualizer.py 可正常导入。</summary>
    public (bool Ok, string Message) PythonHealthCheck()
    {
        try
        {
            var psi = new ProcessStartInfo(_ocr.PythonExe)
            {
                RedirectStandardOutput = true, RedirectStandardError = true,
                UseShellExecute = false, CreateNoWindow = true,
            };
            var pyScriptsDir = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
            if (pyScriptsDir != null)
                psi.WorkingDirectory = Path.GetFullPath(Path.Combine(pyScriptsDir, "..", "..", "..", "Modules", "Drawings"));
            psi.Arguments = "-c \"import sys; from diff_visualizer import run; print('OK')\"";
            using var p = Process.Start(psi);
            if (p == null) return (false, "无法启动 Python 进程");
            if (!p.WaitForExit(30_000)) { p.Kill(); return (false, "Python 进程超时"); }
            var stdout = p.StandardOutput.ReadToEnd().Trim();
            var stderr = p.StandardError.ReadToEnd().Trim();
            if (stdout == "OK") return (true, "Python 编译检查通过");
            if (!string.IsNullOrWhiteSpace(stderr))
                return (false, stderr.Substring(0, Math.Min(400, stderr.Length)));
            return (false, $"stdout={stdout}, stderr={stderr}");
        }
        catch (Exception ex)
        {
            return (false, $"异常: {ex.Message}");
        }
    }

    /// <summary>批量切割：循环所有图纸执行「切割+OCR+入库」。
    /// force=true 时删除 .done 缓存标记强制重切（用于上线新算法后全量刷新）。返回成功张数。</summary>
    public int SegmentAll(bool force = false)
    {
        var ids = new List<int>();
        using (var conn = _db.Open())
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT Id FROM drawings ORDER BY Id";
            using var r = c.ExecuteReader();
            while (r.Read()) ids.Add(r.GetInt32(0));
        }
        var done = 0;
        foreach (var id in ids)
        {
            try { EnsureSegmented(id, force); done++; }
            catch { /* 单张失败不影响其它 */ }
        }
        return done;
    }

    /// <summary>照片内容 MD5（用于结果缓存键，避免同一张照片重复 OCR+匹配）。</summary>
    private static string PhotoMd5(IFormFile file)
    {
        using var md5 = MD5.Create();
        using var s = file.OpenReadStream();
        var hash = md5.ComputeHash(s);
        var sb = new StringBuilder(hash.Length * 2);
        foreach (var b in hash) sb.Append(b.ToString("x2"));
        return sb.ToString();
    }

    /// <summary>
    /// 简化匹配 v1：照片OCR文本 ↔ 图块RawText 直接文本重叠率比对。
    /// 替代复杂 ScoreAgainstV3 作为主链路，旧评分保留做兜底。
    ///
    /// 评分：双轨制
    ///   Track 1 — LCS 连续子串重叠率（处理完全一致的情况）
    ///   Track 2 — 字符集 Jaccard 相似度（处理 OCR 顺序不同的情况，如"型号+热线" vs "热线+型号"）
    ///   最终分 = max(Track1, Track2)
    ///
    /// 阈值：≥0.50=full, ≥0.20=partial, &lt;0.20=none(返回null)
    /// </summary>
    private BlockMatchResult? MatchBySimpleText(int drawingId, string photoText,
        List<BlockRec> blocks, string? qualityError)
    {
        if (string.IsNullOrWhiteSpace(photoText)) return null;
        var photoNorm = ModelParser.Normalize(photoText);
        if (photoNorm.Length < 3) return null;
        // 照片字符集
        var photoChars = new HashSet<char>(photoNorm.ToLowerInvariant());

        var matchable = blocks.Where(b => !IsNonEngineeringBlock(b, b.BlockType ?? "")).ToList();
        if (matchable.Count == 0) return null;

        var scored = new List<(BlockRec Block, double Score)>();
        foreach (var b in matchable)
        {
            var raw = b.RawText ?? "";
            if (string.IsNullOrWhiteSpace(raw)) continue;
            var rawNorm = ModelParser.Normalize(raw);
            if (rawNorm.Length < 3) continue;

            // Track 1: LCS 连续子串
            int lcs = LongestCommonSubstring(photoNorm, rawNorm);
            // Track 2: 字符集 Jaccard
            var rawChars = new HashSet<char>(rawNorm.ToLowerInvariant());
            int common = 0;
            foreach (var c in photoChars) if (rawChars.Contains(c)) common++;
            double jaccard = photoChars.Count > 0 ? (double)common / photoChars.Count : 0;

            double lcsScore = lcs >= 3 ? (double)lcs / Math.Min(photoNorm.Length, rawNorm.Length) : 0;
            double score = Math.Max(lcsScore, jaccard);
            if (score < 0.01) continue;
            scored.Add((b, Math.Round(score, 4)));
        }

        if (scored.Count == 0) return null;
        scored.Sort((a, b) => b.Score.CompareTo(a.Score));
        var best = scored[0];
        // 无强匹配时回退，由调用方走 ScoreAgainstV3 兜底
        if (best.Score < 0.20) return null;

        // 三态判定
        string matchStatus = best.Score >= 0.50 ? "full" : "partial";
        bool matched = matchStatus is "full" or "partial";

        // 部分匹配时提取 mismatchedTokens
        List<string> mismatched = new();
        if (matchStatus == "partial")
        {
            var photoSigs = ExtractContentSignatures(photoText);
            try
            {
                List<string> bSigs = new();
                try { bSigs = JsonSerializer.Deserialize<List<string>>(best.Block.Tokens) ?? new(); } catch { }
                var bSet = new HashSet<string>(bSigs, StringComparer.OrdinalIgnoreCase);
                foreach (var s in photoSigs.Where(s => s.Length >= 2))
                    if (!bSet.Contains(s, StringComparer.OrdinalIgnoreCase) && !mismatched.Contains(s))
                        mismatched.Add(s);
            }
            catch { }
        }

        var d = GetDrawing(drawingId);
        var candidates = scored.Select(s => new BlockHit(s.Block.BIdx, Math.Round(s.Score, 3),
            BlockImageUrl(drawingId, s.Block.BIdx), s.Block.X, s.Block.Y, s.Block.W, s.Block.H)).ToList();

        return new BlockMatchResult(drawingId, d?.Model, d?.FileName, d?.VirtualPath,
            Math.Round(best.Score, 3), photoText,
            matched ? BlockImageUrl(drawingId, best.Block.BIdx) : null, matched,
            candidates, qualityError, matchStatus,
            mismatched.Count > 0 ? mismatched : null);
    }

    /// <summary>核心：上传照片 → 识别内容 → 与「已选图纸」的小图块比对 → 返回命中的小图（原图格式）。
    /// v4 简化：先用 MatchBySimpleText 文本直接匹配；若未命中则回退到 ScoreAgainstV3 兜底。</summary>
    public BlockMatchResult MatchBlock(int drawingId, IFormFile file)
    {
        // ---- 诊断日志（排查企微内置浏览器 "秒返无法识别"）----
        Console.WriteLine($"[MATCH-REQ] drawingId={drawingId} fileName={file.FileName} contentType={file.ContentType} length={file.Length} md5={PhotoMd5(file)}");

        // 照片结果缓存：同一张照片在同一图纸下秒回（任务42）
        // ⭐ 修复：必须按 (drawingId, MD5) 联合键，否则同一照片比对不同图纸会返回第一次的结果
        var photoKey = $"{drawingId}_{PhotoMd5(file)}";
        if (_photoCache.TryGetValue(photoKey, out var cachedResult))
        {
            // 缓存命中但缺少 diff → 需要重新计算（旧缓存或升级迁移）
            if (cachedResult.Diff == null && cachedResult.Matched)
            {
                // 走新流程：保存照片 → 计算 diff → 更新缓存 → 返回
                var segDirCache = SegDir(drawingId);
                var stampCache = Guid.NewGuid().ToString("N").Substring(0, 12);
                var photoPathCache = Path.Combine(segDirCache, $"match_{stampCache}.jpg");
                try
                {
                    using (var ms = new MemoryStream()) { file.CopyTo(ms); File.WriteAllBytes(photoPathCache, ms.ToArray()); }
                    var diff = ComputeDiffFromPath(drawingId, photoPathCache, cachedResult);
                    if (diff != null)
                    {
                        var updated = cachedResult with { Diff = diff };
                        _photoCache[photoKey] = updated;
                        return updated;
                    }
                }
                catch { }
                finally { try { File.Delete(photoPathCache); } catch { } }
            }
            return cachedResult;
        }

        // ⭐ 先保存照片到磁盘（供给 OCR 和后续 diff 重用，避免 IFormFile 流被消耗）
        var _t0 = DateTime.UtcNow;
        var segDir = SegDir(drawingId);
        var photoMd5 = PhotoMd5(file);
        // ⭐ 用 MD5 命名持久化（H5 兜底需要 PhotoImageUrl 长期可用）
        var photoPath = Path.Combine(segDir, $"match_{photoMd5}.jpg");
        try
        {
            using (var ms = new MemoryStream()) { file.CopyTo(ms); File.WriteAllBytes(photoPath, ms.ToArray()); }
        }
        catch { photoPath = null; }

        // ⭐ 缩小保存的照片到最大边 960px（大图 1008x2560 会导致 RapidOCR 耗时 37s+）
        if (photoPath != null && File.Exists(photoPath))
        {
            try
            {
                var scriptsDir = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
                var resizeScript = scriptsDir != null ? Path.Combine(scriptsDir, "..", "..", "..", "Modules", "Drawings", "resize_photo.py") : null;
                if (resizeScript != null)
                    resizeScript = Path.GetFullPath(resizeScript);
                else
                    resizeScript = "resize_photo.py";
                if (File.Exists(resizeScript))
                {
                    var rpsi = new ProcessStartInfo(_ocr.PythonExe)
                    {
                        RedirectStandardOutput = false, RedirectStandardError = false,
                        UseShellExecute = false, CreateNoWindow = true,
                    };
                    rpsi.ArgumentList.Add(resizeScript);
                    rpsi.ArgumentList.Add(photoPath);
                    using var rp = Process.Start(rpsi);
                    rp?.WaitForExit(30_000);
                }
            }
            catch { }
        }
        Console.WriteLine($"[TIMING] save file: {(DateTime.UtcNow - _t0).TotalMilliseconds:F0}ms");

        try
        {
            _t0 = DateTime.UtcNow;
            EnsureSegmented(drawingId);
            Console.WriteLine($"[TIMING] ensure seg: {(DateTime.UtcNow - _t0).TotalMilliseconds:F0}ms");
        string? photoText;
        bool ocrUnavailable = false;
        try
        {
            // ⭐ 走本地路径 OCR（避免 IFormFile 流被消耗的问题）
            if (photoPath != null && File.Exists(photoPath))
                photoText = _ocr.Recognize(photoPath);
            else
                photoText = _ocr.Recognize(file);
        }
        catch (OcrUnavailableException)
        {
            photoText = "";
            ocrUnavailable = true;
        }
        Console.WriteLine($"[TIMING] OCR recognize: {(DateTime.UtcNow - _t0).TotalMilliseconds:F0}ms");
        Console.WriteLine($"[MATCH-OCR] photoText_len={photoText?.Length ?? 0} preview={((photoText??"").Length > 0 ? (photoText?.Substring(0, Math.Min(80, photoText.Length)) + "...") : "(empty)")}");

        var blocks = GetBlocks(drawingId);
        var photoSigs = ExtractContentSignatures(photoText);

        // ---- 照片质量门控 ----
        bool photoMeaningful = IsPhotoMeaningful(photoSigs, photoText);
        string? qualityError = null;
        if (!photoMeaningful)
        {
            if (ocrUnavailable)
                qualityError = "识别服务暂时不可用（OCR 引擎异常退出），请稍后重试；若持续出现请联系管理员";
            else
                qualityError = "无法识别照片内容——请确保拍摄的部件上文字/编码清晰可见（如型号、序列号、服务热线等），避免模糊、反光或过远拍摄";
        }

        // ---- ① 尝试简化匹配（文本直接重叠率）----
        if (photoMeaningful)
        {
            _t0 = DateTime.UtcNow;
            var simple = MatchBySimpleText(drawingId, photoText ?? "", blocks, qualityError);
            Console.WriteLine($"[TIMING] matching: {(DateTime.UtcNow - _t0).TotalMilliseconds:F0}ms");
            if (simple != null)
            {
                // ⭐ A1：先启动差异标记计算（与 DB 落库并行；diff 已免模型加载，仅 PIL 绘制）
                var _tDiffS = DateTime.UtcNow;
                Task<DiffMarkers?> diffTaskS = simple.Matched
                    ? Task.Run(() => ComputeDiffFromPath(drawingId, photoPath, simple))
                    : Task.FromResult<DiffMarkers>(null!);
                // 结果入库（与 diff 并行）
                var d = GetDrawing(drawingId);
                using (var conn = _db.Open())
                using (var c = conn.CreateCommand())
                {
                    c.CommandText = "INSERT INTO drawing_histories (QueryType,QueryText,DetectedModel,DetectedPart,DrawingId,Matched,CreatedAt) VALUES ($t,$q,$dm,$dp,$did,$m,$ts)";
                    c.Parameters.AddWithValue("$t", "block");
                    var qt = photoText ?? "";
                    c.Parameters.AddWithValue("$q", qt.Length > 2000 ? qt.Substring(0, 2000) : qt);
                    c.Parameters.AddWithValue("$dm", (object?)d?.Model ?? DBNull.Value);
                    c.Parameters.AddWithValue("$dp", simple.Matched && simple.Candidates.Count > 0 ? (object?)simple.Candidates[0].Idx.ToString() : DBNull.Value);
                    c.Parameters.AddWithValue("$did", (object?)drawingId ?? DBNull.Value);
                    c.Parameters.AddWithValue("$m", simple.Matched ? 1 : 0);
                    c.Parameters.AddWithValue("$ts", DateTime.Now.ToString("o"));
                    c.ExecuteNonQuery();
                }
                var photoImageUrlS = (photoPath != null && File.Exists(photoPath)) ? $"api/drawings/match-photo?id={drawingId}&md5={photoMd5}" : null;
                if (simple.Matched)
                {
                    var diffS = diffTaskS.GetAwaiter().GetResult();
                    var cached = simple with { Diff = diffS, PhotoImageUrl = photoImageUrlS };
                    _photoCache[photoKey] = cached;
                    Console.WriteLine($"[TIMING] diff compute (parallel): {(DateTime.UtcNow - _tDiffS).TotalMilliseconds:F0}ms");
                    return cached;
                }
                return simple with { PhotoImageUrl = photoImageUrlS };
            }
        }

        // ---- ② 简化匹配未命中 → ScoreAgainstV3 兜底 ----
        var matchable = blocks.Where(b => !IsNonEngineeringBlock(b, b.BlockType ?? "")).ToList();

        var scored = new List<(BlockRec Block, double Score, bool CodeHit, double GramScore, bool UniqueIdHit, bool ModelFuzzyHit, bool EngraveHit)>();
        foreach (var b in matchable)
        {
            List<string> bSigs = new();
            try { bSigs = JsonSerializer.Deserialize<List<string>>(b.Tokens) ?? new(); } catch { }
            var (score, codeHit, gramScore, uniqueIdHit, modelFuzzyHit, engraveHit) = ScoreAgainstV3(photoSigs, bSigs);
            scored.Add((b, score, codeHit, gramScore, uniqueIdHit, modelFuzzyHit, engraveHit));
        }
        scored.Sort((a, b) => b.Score.CompareTo(a.Score));

        var best = scored.FirstOrDefault();

        bool matched = false;
        if (photoMeaningful && best.Block != null)
        {
            bool hasCodeSignal = best.UniqueIdHit || best.ModelFuzzyHit || best.CodeHit || best.EngraveHit;
            if (hasCodeSignal && best.Score >= 0.20)
                matched = true;
        }

        var drawing = GetDrawing(drawingId);
        var candidates = scored.Select(s => new BlockHit(s.Block.BIdx, Math.Round(s.Score, 3), BlockImageUrl(drawingId, s.Block.BIdx), s.Block.X, s.Block.Y, s.Block.W, s.Block.H)).ToList();

        // Rule 3: 三态分类 + 不匹配token提取
        string matchStatus = "none";
        List<string> mismatched = new();

        if (photoMeaningful && best.Block != null)
        {
            bool hasSig = best.UniqueIdHit || best.ModelFuzzyHit || best.CodeHit || best.EngraveHit;
            if (matched && best.Score >= 0.70)
                matchStatus = "full";
            else if ((matched && best.Score < 0.70) || (!matched && hasSig && best.Score > 0.05))
            {
                matchStatus = "partial";
                // 提取照片中存在但最佳匹配块中没有的 token（不匹配部分）
                try
                {
                    List<string> bSigs = new();
                    try { bSigs = JsonSerializer.Deserialize<List<string>>(best.Block.Tokens) ?? new(); } catch { }
                    var bSet = new HashSet<string>(bSigs, StringComparer.OrdinalIgnoreCase);
                    // 取照片签名中"有价值"的token（排除单字噪声）
                    foreach (var s in photoSigs.Where(s => s.Length >= 2))
                        if (!bSet.Contains(s, StringComparer.OrdinalIgnoreCase) && !mismatched.Contains(s))
                            mismatched.Add(s);
                }
                catch { /* token extraction failure — partial still shown without details */ }
            }
        }
        else if (!photoMeaningful)
        {
            matchStatus = "none";  // 质量门控未通过
        }

        var dispScore = matchStatus == "full" || matchStatus == "partial"
            ? Math.Round(best.Score, 3)
            : (matched && best.Block != null ? Math.Round(best.Score, 3) : 0);

        var result = new BlockMatchResult(
            drawingId, drawing?.Model, drawing?.FileName, drawing?.VirtualPath,
            dispScore,
            photoText,
            (matchStatus == "full" || matchStatus == "partial") && best.Block != null
                ? BlockImageUrl(drawingId, best.Block.BIdx) : null,
            matchStatus == "full" || matchStatus == "partial",  // Matched reflects meaningful match only
            candidates,
            qualityError,
            matchStatus,
            mismatched.Count > 0 ? mismatched : null);

        // ⭐ A1：差异标记计算与 DB 落库并行（diff 已免模型加载，仅 PIL 绘制）
        var _tDiff = DateTime.UtcNow;
        Task<DiffMarkers?> diffTask = result.Matched
            ? Task.Run(() => ComputeDiffFromPath(drawingId, photoPath, result))
            : Task.FromResult<DiffMarkers>(null!);

        // DB 落库（与 diff 并行）
        using (var conn = _db.Open())
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "INSERT INTO drawing_histories (QueryType,QueryText,DetectedModel,DetectedPart,DrawingId,Matched,CreatedAt) VALUES ($t,$q,$dm,$dp,$did,$m,$ts)";
            c.Parameters.AddWithValue("$t", "block");
            var qt = photoText ?? "";
            c.Parameters.AddWithValue("$q", qt.Length > 2000 ? qt.Substring(0, 2000) : qt);
            c.Parameters.AddWithValue("$dm", (object?)drawing?.Model ?? DBNull.Value);
            c.Parameters.AddWithValue("$dp", matched && best.Block != null ? (object?)best.Block.BIdx.ToString() : DBNull.Value);
            c.Parameters.AddWithValue("$did", (object?)drawingId ?? DBNull.Value);
            c.Parameters.AddWithValue("$m", matched ? 1 : 0);
            c.Parameters.AddWithValue("$ts", DateTime.Now.ToString("o"));
            c.ExecuteNonQuery();
        }

        DiffMarkers? diff = result.Matched ? diffTask.GetAwaiter().GetResult() : null;
        var photoImageUrl = (photoPath != null && File.Exists(photoPath))
            ? $"api/drawings/match-photo?id={drawingId}&md5={photoMd5}"
            : null;
        var finalResult = result with { Diff = diff, PhotoImageUrl = photoImageUrl };
        if (result.Matched) _photoCache[photoKey] = finalResult;
        Console.WriteLine($"[TIMING] diff compute (parallel): {(DateTime.UtcNow - _tDiff).TotalMilliseconds:F0}ms");
        return finalResult;
        }
        finally
        {
            // ⭐ 保留照片文件——H5 兜底显示需要 PhotoImageUrl 长期可访问
        }
    }

    /// <summary>照片质量门控：判断 OCR 结果是否包含可识别的工程编码。
    /// 工程照片应至少包含一个「像样的编码」（≥4位数字串、或含字母数字混合的型号码），
    /// 否则判定为垃圾 OCR（噪声/模糊/拍歪了），直接报错不匹配。</summary>
        // ================= 合并单次调用（任务2）：块匹配 + 照片标示 + 快照入库 =================
        /// <summary>
        /// 一次请求完成：① 常驻 worker 合并任务（零 RapidOCR 重载）产出「整图 OCR 文本 + 照片标示图 + 块级差异」；
        /// ② 用返回 photoText 做产品块匹配（复用 MatchBySimpleText / ScoreAgainstV3，不重复 OCR）；
        /// ③ 写 history 主记录并取 Id → 落盘快照 → 返回 {historyId, photoDiffUrl, 匹配结论}。
        /// 旧 /match-block + /diff-photo 串行两次请求被合并为一次，H5 最终呈现 &lt;10s（真实 HTTP 验证为准）。
        /// 失败回退：worker 不可用或 combined 失败时，回退「单独 OCR + MarkDifferencesOnPhoto」等价产出。
        /// </summary>
        public MatchAndPhotoResult MatchAndPhoto(int drawingId, IFormFile file)
        {
            var photoMd5 = PhotoMd5(file);
            // ---- 保存照片到磁盘（与 MatchBlock 一致：供 OCR 与后续快照复用）----
            var segDir = SegDir(drawingId);
            var photoPath = Path.Combine(segDir, $"match_{photoMd5}.jpg");
            try
            {
                using (var ms = new MemoryStream()) { file.CopyTo(ms); File.WriteAllBytes(photoPath, ms.ToArray()); }
                // ⭐ v19.50：另存原图副本供照片图标检测（resize 到 960 后小图标如 11px NFC
                //    的 OTSU 轮廓丢失 → 照片 0 检出 → 误报缺图标黄框）。run_on_photo 图标检测
                //    优先用 _orig；OCR/匹配仍用 resize 后的 match_<md5>.jpg，行为不变。
                try
                {
                    using (var ms2 = new MemoryStream()) { file.CopyTo(ms2); File.WriteAllBytes(Path.Combine(segDir, $"match_{photoMd5}_orig.jpg"), ms2.ToArray()); }
                }
                catch { /* 原图副本保存失败不阻断主流程 */ }
            }
            catch { photoPath = null; }

            // 缩小保存的照片到最大边 960px（与 MatchBlock 一致，避免大图拖慢 OCR）
            if (photoPath != null && File.Exists(photoPath))
            {
                try
                {
                    var scriptsDir = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
                    var resizeScript = scriptsDir != null ? Path.Combine(scriptsDir, "..", "..", "..", "Modules", "Drawings", "resize_photo.py") : null;
                    if (resizeScript != null) resizeScript = Path.GetFullPath(resizeScript);
                    else resizeScript = "resize_photo.py";
                    if (File.Exists(resizeScript))
                    {
                        var rpsi = new ProcessStartInfo(_ocr.PythonExe)
                        {
                            RedirectStandardOutput = false, RedirectStandardError = false,
                            UseShellExecute = false, CreateNoWindow = true,
                        };
                        rpsi.ArgumentList.Add(resizeScript);
                        rpsi.ArgumentList.Add(photoPath);
                        using var rp = Process.Start(rpsi);
                        rp?.WaitForExit(30_000);
                    }
                }
                catch { }
            }

            var segRoot = Path.Combine(_dataDir, "seg");
            var stamp = Guid.NewGuid().ToString("N").Substring(0, 12);
            var outFile = Path.Combine(segDir, $"diff_photo_out_{stamp}.jpg");

            string? photoText = null;
            List<PhotoDiffBlock>? pdBlocks = null;
            string? photoDiffUrl = null;
            bool combinedFailed = false;

            var (payloadJson, failed, _) = _ocr.CombinedDiff(photoPath ?? "", drawingId.ToString(), outFile, _dbPath, segRoot);
            if (failed || string.IsNullOrWhiteSpace(payloadJson))
            {
                combinedFailed = true;
            }
            else
            {
                try
                {
                    using var doc = JsonDocument.Parse(payloadJson);
                    var root = doc.RootElement;
                    var ok = root.TryGetProperty("success", out var sEl) && sEl.GetBoolean();
                    if (ok)
                    {
                        photoText = root.TryGetProperty("photoText", out var pt) ? pt.GetString() : null;
                        pdBlocks = ParsePhotoDiffBlocks(root);
                        photoDiffUrl = $"/api/drawings/diff-photo-image?did={drawingId}&file={Path.GetFileName(outFile)}";
                    }
                    else
                    {
                        combinedFailed = true;
                    }
                }
                catch { combinedFailed = true; }
            }

            // 回退：combined 失败 → 单独 OCR（worker 复用）+ 旧 MarkDifferencesOnPhoto
            if (combinedFailed)
            {
                try
                {
                    if (photoPath != null && File.Exists(photoPath)) photoText = _ocr.Recognize(photoPath);
                    else photoText = _ocr.Recognize(file);
                }
                catch (OcrUnavailableException) { photoText = ""; }
                var pd = MarkDifferencesOnPhoto(drawingId, file);
                if (pd.Success) { photoDiffUrl = pd.MarkedImageUrl; pdBlocks = pd.Blocks; }
            }

            bool ocrUnavailable = combinedFailed && string.IsNullOrEmpty(photoText);
            var match = MatchCoreFromText(drawingId, photoText, photoPath, photoMd5, ocrUnavailable);

            DiffMarkers? diff = null;
            if ((match.MatchStatus is "full" or "partial") && pdBlocks != null)
                diff = BuildDiffFromBlocks(pdBlocks);
            var finalMatch = match with { Diff = diff };

            // ---- 写 history 主记录并取 Id → 落盘快照 ----
            int historyId = 0;
            using (var conn = _db.Open())
            using (var c = conn.CreateCommand())
            {
                c.CommandText = "INSERT INTO drawing_histories (QueryType,QueryText,DetectedModel,DetectedPart,DrawingId,Matched,CreatedAt) VALUES ($t,$q,$dm,$dp,$did,$m,$ts)";
                c.Parameters.AddWithValue("$t", "block");
                var qt = photoText ?? "";
                c.Parameters.AddWithValue("$q", qt.Length > 2000 ? qt.Substring(0, 2000) : qt);
                c.Parameters.AddWithValue("$dm", (object?)match.Model ?? DBNull.Value);
                c.Parameters.AddWithValue("$dp", finalMatch.Matched && finalMatch.Candidates.Count > 0 ? (object?)finalMatch.Candidates[0].Idx.ToString() : DBNull.Value);
                c.Parameters.AddWithValue("$did", (object?)drawingId ?? DBNull.Value);
                c.Parameters.AddWithValue("$m", finalMatch.Matched ? 1 : 0);
                c.Parameters.AddWithValue("$ts", DateTime.Now.ToString("o"));
                c.ExecuteNonQuery();
                using var idc = conn.CreateCommand(); idc.CommandText = "SELECT last_insert_rowid()"; historyId = Convert.ToInt32(idc.ExecuteScalar());
                if ((!string.IsNullOrEmpty(outFile)) && File.Exists(outFile))
                {
                    var snapRel = "seg/" + drawingId + "/" + Path.GetFileName(outFile);
                    using var u = conn.CreateCommand();
                    u.CommandText = "UPDATE drawing_histories SET SnapshotPath=$sp WHERE Id=$id";
                    u.Parameters.AddWithValue("$sp", snapRel);
                    u.Parameters.AddWithValue("$id", historyId);
                    u.ExecuteNonQuery();
                }
            }

            return new MatchAndPhotoResult(
                historyId, photoDiffUrl,
                finalMatch.DrawingId, finalMatch.Model, finalMatch.FileName, finalMatch.VirtualPath,
                finalMatch.Score, finalMatch.PhotoText, finalMatch.BlockImageUrl, finalMatch.Matched,
                finalMatch.Candidates, finalMatch.QualityError, finalMatch.MatchStatus,
                finalMatch.MismatchedTokens, finalMatch.Diff, finalMatch.PhotoImageUrl);
        }

        /// <summary>从 photoText 计算产品块匹配（不重复 OCR、不写库、不算 diff），供 MatchAndPhoto 复用。</summary>
        private BlockMatchResult MatchCoreFromText(int drawingId, string? photoText, string? photoPath, string photoMd5, bool ocrUnavailable)
        {
            var blocks = GetBlocks(drawingId);
            var photoSigs = ExtractContentSignatures(photoText);
            bool photoMeaningful = IsPhotoMeaningful(photoSigs, photoText);
            string? qualityError = null;
            if (!photoMeaningful)
            {
                qualityError = ocrUnavailable
                    ? "识别服务暂时不可用（OCR 引擎异常退出），请稍后重试；若持续出现请联系管理员"
                    : "无法识别照片内容——请确保拍摄的部件上文字/编码清晰可见（如型号、序列号、服务热线等），避免模糊、反光或过远拍摄";
            }

            if (photoMeaningful)
            {
                var simple = MatchBySimpleText(drawingId, photoText ?? "", blocks, qualityError);
                if (simple != null)
                {
                    var photoImageUrlS = (photoPath != null && File.Exists(photoPath)) ? $"api/drawings/match-photo?id={drawingId}&md5={photoMd5}" : null;
                    return simple with { PhotoImageUrl = photoImageUrlS };
                }
            }

            var matchable = blocks.Where(b => !IsNonEngineeringBlock(b, b.BlockType ?? "")).ToList();
            var scored = new List<(BlockRec Block, double Score, bool CodeHit, double GramScore, bool UniqueIdHit, bool ModelFuzzyHit, bool EngraveHit)>();
            foreach (var b in matchable)
            {
                List<string> bSigs = new();
                try { bSigs = JsonSerializer.Deserialize<List<string>>(b.Tokens) ?? new(); } catch { }
                var (score, codeHit, gramScore, uniqueIdHit, modelFuzzyHit, engraveHit) = ScoreAgainstV3(photoSigs, bSigs);
                scored.Add((b, score, codeHit, gramScore, uniqueIdHit, modelFuzzyHit, engraveHit));
            }
            scored.Sort((a, b) => b.Score.CompareTo(a.Score));
            var best = scored.FirstOrDefault();

            bool matched = false;
            if (photoMeaningful && best.Block != null)
            {
                bool hasCodeSignal = best.UniqueIdHit || best.ModelFuzzyHit || best.CodeHit || best.EngraveHit;
                if (hasCodeSignal && best.Score >= 0.20) matched = true;
            }

            var drawing = GetDrawing(drawingId);
            var candidates = scored.Select(s => new BlockHit(s.Block.BIdx, Math.Round(s.Score, 3), BlockImageUrl(drawingId, s.Block.BIdx), s.Block.X, s.Block.Y, s.Block.W, s.Block.H)).ToList();

            string matchStatus = "none";
            List<string> mismatched = new();
            if (photoMeaningful && best.Block != null)
            {
                bool hasSig = best.UniqueIdHit || best.ModelFuzzyHit || best.CodeHit || best.EngraveHit;
                if (matched && best.Score >= 0.70) matchStatus = "full";
                else if ((matched && best.Score < 0.70) || (!matched && hasSig && best.Score > 0.05))
                {
                    matchStatus = "partial";
                    try
                    {
                        List<string> bSigs = new();
                        try { bSigs = JsonSerializer.Deserialize<List<string>>(best.Block.Tokens) ?? new(); } catch { }
                        var bSet = new HashSet<string>(bSigs, StringComparer.OrdinalIgnoreCase);
                        foreach (var s in photoSigs.Where(s => s.Length >= 2))
                            if (!bSet.Contains(s, StringComparer.OrdinalIgnoreCase) && !mismatched.Contains(s)) mismatched.Add(s);
                    }
                    catch { }
                }
            }
            else if (!photoMeaningful) matchStatus = "none";

            var dispScore = matchStatus is "full" or "partial"
                ? Math.Round(best.Score, 3)
                : (matched && best.Block != null ? Math.Round(best.Score, 3) : 0);

            var result = new BlockMatchResult(
                drawingId, drawing?.Model, drawing?.FileName, drawing?.VirtualPath,
                dispScore, photoText,
                (matchStatus is "full" or "partial") && best.Block != null ? BlockImageUrl(drawingId, best.Block.BIdx) : null,
                matchStatus is "full" or "partial",
                candidates, qualityError, matchStatus,
                mismatched.Count > 0 ? mismatched : null);

            var photoImageUrl = (photoPath != null && File.Exists(photoPath))
                ? $"api/drawings/match-photo?id={drawingId}&md5={photoMd5}" : null;
            return result with { PhotoImageUrl = photoImageUrl };
        }

        /// <summary>解析 combined 任务返回的 blocks JSON 为 PhotoDiffBlock 列表（复用 MarkDifferencesOnPhotoCore 解析逻辑）。</summary>
        private static List<PhotoDiffBlock> ParsePhotoDiffBlocks(JsonElement root)
        {
            var blocks = new List<PhotoDiffBlock>();
            if (!root.TryGetProperty("blocks", out var blkArr) || blkArr.ValueKind != JsonValueKind.Array) return blocks;
            foreach (var bEl in blkArr.EnumerateArray())
            {
                int bidx = bEl.TryGetProperty("bidx", out var bi) && bi.ValueKind == JsonValueKind.Number ? bi.GetInt32() : -1;
                int[]? roi = null;
                if (bEl.TryGetProperty("roi", out var roiEl) && roiEl.ValueKind == JsonValueKind.Array && roiEl.GetArrayLength() == 4)
                    roi = new[] { roiEl[0].GetInt32(), roiEl[1].GetInt32(), roiEl[2].GetInt32(), roiEl[3].GetInt32() };
                string conf = bEl.TryGetProperty("conf", out var cf) ? cf.GetString() ?? "low" : "low";
                string status = bEl.TryGetProperty("status", out var st) ? st.GetString() ?? "low_conf" : "low_conf";
                int photoLineCount = bEl.TryGetProperty("photoLineCount", out var plc) && plc.ValueKind == JsonValueKind.Number ? plc.GetInt32() : 0;
                int iconMissing = 0, iconExtra = 0;
                if (bEl.TryGetProperty("icons", out var ic) && ic.ValueKind == JsonValueKind.Object)
                {
                    if (ic.TryGetProperty("missing", out var im) && im.ValueKind == JsonValueKind.Number) iconMissing = im.GetInt32();
                    if (ic.TryGetProperty("extra", out var ie) && ie.ValueKind == JsonValueKind.Number) iconExtra = ie.GetInt32();
                }
                var lines = new List<PhotoDiffLine>();
                if (bEl.TryGetProperty("lines", out var lnArr) && lnArr.ValueKind == JsonValueKind.Array)
                {
                    foreach (var lnEl in lnArr.EnumerateArray())
                    {
                        string lstatus = lnEl.TryGetProperty("status", out var ls) ? ls.GetString() ?? "" : "";
                        string ltext = lnEl.TryGetProperty("text", out var lt) ? lt.GetString() ?? "" : "";
                        int[]? lbbox = null;
                        if (lnEl.TryGetProperty("bbox", out var lb) && lb.ValueKind == JsonValueKind.Array && lb.GetArrayLength() >= 4)
                            lbbox = new[] { lb[0].GetInt32(), lb[1].GetInt32(), lb[2].GetInt32(), lb[3].GetInt32() };
                        if (lbbox != null) lines.Add(new PhotoDiffLine(lstatus, lbbox, ltext));
                    }
                }
                blocks.Add(new PhotoDiffBlock(bidx, roi, conf, status, lines, iconMissing, iconExtra, photoLineCount));
            }
            return blocks;
        }

        /// <summary>从照片级 blocks 聚合出结构化 DiffMarkers（供前端 renderDiffMarkers 复用，零额外 Python 调用）。</summary>
        private static DiffMarkers BuildDiffFromBlocks(List<PhotoDiffBlock> blocks)
        {
            var red = new List<DiffRegion>();
            var yellow = new List<DiffRegion>();
            var green = new List<DiffRegion>();
            var gray = new List<DiffRegion>();
            int iconMissing = 0, iconExtra = 0;
            foreach (var b in blocks)
            {
                if (b.Lines != null) foreach (var l in b.Lines)
                {
                    int x = 0, y = 0, w = 0, h = 0;
                    if (l.BBox != null && l.BBox.Length >= 4) { x = l.BBox[0]; y = l.BBox[1]; w = l.BBox[2]; h = l.BBox[3]; }
                    var r = new DiffRegion(x, y, w, h, l.Text, l.Status);
                    if (l.Status == "red") red.Add(r);
                    else if (l.Status == "yellow") yellow.Add(r);
                    else if (l.Status == "green") green.Add(r);
                    else if (l.Status == "gray") gray.Add(r);
                }
                iconMissing += b.IconMissing;
                iconExtra += b.IconExtra;
            }
            var iconMissingRegions = new List<DiffRegion>();
            for (int i = 0; i < iconMissing; i++) iconMissingRegions.Add(new DiffRegion(0, 0, 0, 0, "", ""));
            var iconExtraRegions = new List<DiffRegion>();
            for (int i = 0; i < iconExtra; i++) iconExtraRegions.Add(new DiffRegion(0, 0, 0, 0, "", ""));
            return new DiffMarkers(red, yellow, gray, green, new(), new(), new(), null,
                new List<DiffRegion>(), iconMissingRegions, iconExtraRegions,
                new List<CharDiff>(), new List<CharDiff>());
        }

        /// <summary>匹配结果响应（合并端点）：在 BlockMatchResult 字段基础上附加 historyId + photoDiffUrl。</summary>
        public record MatchAndPhotoResult(
            int HistoryId, string? PhotoDiffUrl,
            int DrawingId, string? Model, string? FileName, string? VirtualPath,
            double Score, string? PhotoText, string? BlockImageUrl, bool Matched,
            List<BlockHit> Candidates, string? QualityError, string MatchStatus,
            List<string>? MismatchedTokens, DiffMarkers? Diff, string? PhotoImageUrl);

        /// <summary>按 history 行 SnapshotPath 返回标示照片快照（任务3：历史页缩略图，不依赖图纸库）。</summary>
        public IResult ServeHistorySnapshot(int id)
        {
            using var conn = _db.Open();
            using var c = conn.CreateCommand();
            c.CommandText = "SELECT SnapshotPath FROM drawing_histories WHERE Id=$id";
            c.Parameters.AddWithValue("$id", id);
            var snap = c.ExecuteScalar() as string;
            if (string.IsNullOrEmpty(snap)) return Results.NotFound(new { error = "快照不存在" });
            if (!snap.Replace('\\', '/').StartsWith("seg/", StringComparison.OrdinalIgnoreCase))
                return Results.BadRequest(new { error = "非法快照路径" });
            var full = Path.Combine(_dataDir, snap.Replace('/', Path.DirectorySeparatorChar));
            if (!File.Exists(full)) return Results.NotFound(new { error = "快照文件缺失" });
            return Results.File(full, "image/jpeg", Path.GetFileName(full));
        }

    private static bool IsPhotoMeaningful(List<string> sigs, string? rawText)
    {
        if (sigs == null || sigs.Count == 0) return false;
        var t = rawText ?? "";

        // 质量门控（v7.5 放宽）：
        //   原逻辑要求命中「强信号」(词表词/≥2英文词/≥2中文2-gram/型号/6位数字)才放行，
        //   导致「OCR 较弱、只识别出部分打标文字」的真实照片被误杀为「无法识别」。
        //   现改为「只要照片里真有可读文字内容就放行进匹配」：
        //     · 命中激光打标词表（强信号）→ 放行
        //     · ≥4 个字母/汉字（任何可读文本）→ 放行
        //     · ≥6 位连续数字（长编码/电话）→ 放行
        //   最终是否命中由 ScoreAgainstV3 打分决定（需 hasCodeSignal && score≥0.20），
        //   故放宽门控不会降低匹配精度，只是不再把弱 OCR 的真实照片一棍打死。
        if (sigs.Any(s => EngraveVocab.Contains(s))) return true;
        var meaningfulChars = t.Count(c => char.IsLetter(c) || (c >= '\u4e00' && c <= '\u9fff'));
        if (meaningfulChars >= 4) return true;          // 有 4+ 字母/汉字 → 视为有可读打标内容
        if (Regex.IsMatch(t, @"\d{6,}")) return true;   // 或 6+ 位连续数字（长编码/电话）
        return false;
    }

    public IResult ServeBlockImage(int drawingId, int idx)
    {
        var blkName = $"{drawingId}_blk_{idx.ToString("D2")}.png";
        var png = Path.Combine(SegDir(drawingId), "blocks", blkName);
        if (!File.Exists(png)) return Results.NotFound(new { error = "小图未生成" });
        return Results.File(png, "image/png", $"block_{idx}.png");
    }

    /// <summary>服务匹配用的用户产品照片（H5 兜底用）。</summary>
    public IResult ServeMatchPhoto(int drawingId, string md5)
    {
        // 优先用 MD5 命名的（持久化），其次找带 stamp 的
        var segDir = SegDir(drawingId);
        var byMd5 = Path.Combine(segDir, $"match_{md5}.jpg");
        if (File.Exists(byMd5)) return Results.File(byMd5, "image/jpeg", $"match_{md5}.jpg");
        // fallback：扫描 segDir 找最新 match_*.jpg
        try
        {
            var dir = new DirectoryInfo(segDir);
            var latest = dir.GetFiles("match_*.jpg").OrderByDescending(f => f.LastWriteTime).FirstOrDefault();
            if (latest != null) return Results.File(latest.FullName, "image/jpeg", latest.Name);
        }
        catch { }
        return Results.NotFound(new { error = "匹配照片不存在" });
    }

    public IResult ServeFullImage(int drawingId)
    {
        var png = Path.Combine(SegDir(drawingId), $"{drawingId}_full.png");
        if (!File.Exists(png)) return Results.NotFound(new { error = "完整图未生成" });
        return Results.File(png, "image/png", $"drawing_{drawingId}_full.png");
    }

    // ================= 差异标记 v1 =================
    // 用户上传照片后，对匹配命中的图块做 OCR-bbox 级文本差异比较，
    // 排除 CAD 标注（尺寸/引线/标题栏），仅在产品真实文本上标记差异。
    public record DiffRegion(int X, int Y, int W, int H, string Text, string Status);
    public record DiffResult(
        bool Success, string? Error,
        string? MarkedImageUrl,
        List<DiffRegion> RedRegions,
        List<DiffRegion> YellowRegions,
        List<DiffRegion> GrayRegions,
        List<DiffRegion> GreenRegions,
        List<string> PhotoExclusive,
        List<string> BlockExclusive,
        List<string> CadOnly,
        int BlockIdx,
        List<DiffRegion> IconRegions,
        List<DiffRegion> IconMissingRegions,
        List<DiffRegion> IconExtraRegions,
        double DeskewAngle
    );

    // ================= 差异标记 v2（照片直接标示 + 切图块双展示）=================
    // 在整张照片上直接画每块 ROI 外框 + 逐文本行框（照片坐标，可靠，跨块污染已消除）。
    public record PhotoDiffLine(string Status, int[] BBox, string Text);
    public record PhotoDiffBlock(
        int BIdx,
        int[]? Roi,
        string Conf,
        string Status,
        List<PhotoDiffLine> Lines,
        int IconMissing,
        int IconExtra,
        int PhotoLineCount
    );
    public record PhotoDiffResult(
        bool Success,
        string? Error,
        string? MarkedImageUrl,
        double DeskewAngle,
        List<PhotoDiffBlock> Blocks
    );

    /// <summary>
    /// 调用 diff_visualizer.py 计算并保存差异标记图，返回结构化差异数据。
    /// 输入 photo 写入临时文件；输出图保存到 data/seg/{did}/diff_{idx}_{md5}.png。
    /// </summary>
    public DiffResult MarkDifferences(int drawingId, int blockIdx, IFormFile file)
    {
        // 全局互斥：rapidocr-onnxruntime 加载的 ONNX 模型在 Python 进程退出后
        // 仍会持有文件句柄（FileMapping），并发/连续拉起会触发首调之外的
        // "exit=0 但 stdout 为空" 问题。这里用 SemaphoreSlim 串行化所有 mark-diff 请求。
        _diffLock.Wait();
        try
        {
            var r = MarkDifferencesCore(drawingId, blockIdx, file);
            // 释放后等待 600ms 让 ONNX 模型文件锁完全回收（实测 150ms 偶发不够）
            Thread.Sleep(600);
            return r;
        }
        finally
        {
            _diffLock.Release();
        }
    }

    private static readonly SemaphoreSlim _diffLock = new(1, 1);

    private DiffResult MarkDifferencesCore(int drawingId, int blockIdx, IFormFile file)
    {
        var empty = new DiffResult(false, "init", null, new(), new(), new(), new(), new(), new(), new(), blockIdx, new(), new(), new(), 0.0);
        if (file == null || file.Length == 0) return empty with { Error = "未收到照片文件" };
        if (drawingId <= 0 || blockIdx < 0) return empty with { Error = "参数错误" };

        // 1. 落盘照片
        var segDir = SegDir(drawingId);
        if (!Directory.Exists(segDir)) return empty with { Error = "图纸未切割" };
        var stamp = Guid.NewGuid().ToString("N").Substring(0, 12);
        var inFile = Path.Combine(segDir, $"diff_photo_{stamp}.jpg");
        var outFile = Path.Combine(segDir, $"diff_{blockIdx}_{stamp}.jpg");  // ⭐ JPEG 输出
        var blockPng = Path.Combine(segDir, "blocks", $"{drawingId}_blk_{blockIdx:D2}.png");
        try
        {
            using (var ms = new MemoryStream())
            {
                file.CopyTo(ms);
                File.WriteAllBytes(inFile, ms.ToArray());
            }
            if (!File.Exists(blockPng))
                return empty with { Error = $"图块 {blockIdx} 不存在" };

            // 2. 调 diff_visualizer.py（与 OCR 同源干净环境）。
            //    RapidOCR + ONNX 模型在并发 / 模型文件锁偶发场景下首调可能静默崩溃
            //    （exit=1、无 stdout、stderr=空），做最多 2 次重试 + 递增间隔，覆盖抖动。
            var diffScript = _segmentScript.Replace("segment_blocks.py", "diff_visualizer.py");
            if (!File.Exists(diffScript))
                return empty with { Error = "diff_visualizer.py 缺失" };

            // A2：复用持久化 bbox + 图标框，并传块 RawText（避免块图重复 OCR / 图标检测）
            var mKey = $"{drawingId}_{blockIdx}";
            _blockBboxCache.TryGetValue(mKey, out var mCachedBbox);
            if (string.IsNullOrEmpty(mCachedBbox)) { var (pb, _) = LoadBlockBBoxIcons(drawingId, blockIdx); mCachedBbox = pb; }
            var (_, pIcons) = LoadBlockBBoxIcons(drawingId, blockIdx);
            string? mRawText = null;
            try { var mb = GetBlocks(drawingId).FirstOrDefault(b => b.BIdx == blockIdx); mRawText = mb?.RawText; } catch { }

            string jsonText = "", stderr = "";
            int exitCode = -1;
            for (int attempt = 1; attempt <= 3; attempt++)
            {
                var psi = _ocr.CreatePythonPsi(diffScript, inFile, blockPng, outFile);
                psi.ArgumentList.Add(mCachedBbox ?? "");   // 第4参：块 bbox cache
                psi.ArgumentList.Add(mRawText ?? "");       // 第5参：块 RawText override
                psi.ArgumentList.Add("");                   // 第6参：照片 OCR 由 Python 自行完成
                psi.ArgumentList.Add("");                   // 第7参：保留
                psi.ArgumentList.Add(pIcons ?? "");         // 第8参：图标框 override
                using (var p = Process.Start(psi)!)
                {
                    var outTask = p.StandardOutput.ReadToEndAsync();
                    var errTask = p.StandardError.ReadToEndAsync();
                    var exited = p.WaitForExit(60_000);
                    if (!exited) { try { p.Kill(); } catch { } }
                    Task.WaitAll(outTask, errTask);
                    jsonText = outTask.Result ?? "";
                    stderr = errTask.Result ?? "";
                    exitCode = p.ExitCode;
                }
                if (!string.IsNullOrWhiteSpace(jsonText) && exitCode == 0) break;
                // 重试前等更久，让 ONNX 模型锁释放
                if (attempt < 3) Thread.Sleep(800 * attempt);
            }
            Console.WriteLine($"[DIFF-DEBUG] exitCode={exitCode} stdout_len={jsonText.Length} stderr_len={stderr.Length} stdout_head={jsonText[..Math.Min(120, jsonText.Length)]}");
            Console.WriteLine($"[DIFF-STDERR] {(string.IsNullOrEmpty(stderr) ? "(empty)" : stderr[..Math.Min(500, stderr.Length)])}");

            // 3. 解析 stdout JSON（找第一个完整 JSON 对象）
            if (string.IsNullOrWhiteSpace(jsonText))
                return empty with { Error = $"Python 无输出；stderr={stderr[..Math.Min(200, stderr.Length)]}" };
            // 找第一个 { 到最后一个 } 的子串
            int startIdx = jsonText.IndexOf('{');
            int endIdx = jsonText.LastIndexOf('}');
            if (startIdx < 0 || endIdx < 0 || endIdx <= startIdx)
                return empty with { Error = $"stdout 无 JSON 段：{jsonText[..Math.Min(200, jsonText.Length)]}" };
            var jsonSegment = jsonText.Substring(startIdx, endIdx - startIdx + 1);
            using var doc = JsonDocument.Parse(jsonSegment);
            var root = doc.RootElement;
            var ok = root.TryGetProperty("success", out var sEl) && sEl.GetBoolean();
            if (!ok)
            {
                var err = root.TryGetProperty("error", out var eEl) ? eEl.GetString() : "diff_failed";
                return empty with { Error = err ?? "diff_failed" };
            }

            // 4. 解析区域
            List<DiffRegion> ParseRegions(string key, List<string>? texts = null)
            {
                var lst = new List<DiffRegion>();
                if (!root.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array) return lst;
                int i = 0;
                foreach (var el in arr.EnumerateArray())
                {
                    if (el.ValueKind != JsonValueKind.Array || el.GetArrayLength() < 4) { i++; continue; }
                    var x = el[0].GetInt32(); var y = el[1].GetInt32();
                    var w = el[2].GetInt32(); var h = el[3].GetInt32();
                    // 找对应的文字描述
                    string text = "";
                    if (texts != null && i < texts.Count)
                        text = texts[i];
                    else if (key == "redRegions" && i < ParseStringList(root, "blockExclusive").Count)
                        text = ParseStringList(root, "blockExclusive")[i];
                    else if (key == "yellowRegions" && i < ParseStringList(root, "photoExclusive").Count)
                        text = ParseStringList(root, "photoExclusive")[i];
                    else if (key == "grayRegions" && i < ParseStringList(root, "cadOnly").Count)
                        text = ParseStringList(root, "cadOnly")[i];
                    lst.Add(new DiffRegion(x, y, w, h, text, ""));
                    i++;
                }
                return lst;
            }
            static List<string> ParseStringList(JsonElement root, string key)
            {
                var lst = new List<string>();
                if (root.TryGetProperty(key, out var a) && a.ValueKind == JsonValueKind.Array)
                    foreach (var e in a.EnumerateArray())
                        if (e.ValueKind == JsonValueKind.String) lst.Add(e.GetString() ?? "");
                return lst;
            }

            var url = $"/api/drawings/diff-image?did={drawingId}&file={Path.GetFileName(outFile)}";
            // ⭐ 图标差异（位置对比，不比准确性）：到位(紫)/缺图标(红)/多图标(计数)
            var iconRegions = ParseRegions("iconRegions", new List<string>());
            var iconMissing = ParseRegions("iconMissingRegions", new List<string>());
            var iconExtra = ParseRegions("iconExtraRegions", new List<string>());
            // ⭐ 扶正角度（0.0 = 无需扶正）
            double deskewAngle = 0.0;
            if (root.TryGetProperty("deskewAngle", out var da) && da.ValueKind == JsonValueKind.Number)
                deskewAngle = da.GetDouble();
            return new DiffResult(
                true, null, url,
                ParseRegions("redRegions"),
                ParseRegions("yellowRegions"),
                ParseRegions("grayRegions"),
                ParseRegions("greenRegions"),
                ParseStringList(root, "photoExclusive"),
                ParseStringList(root, "blockExclusive"),
                ParseStringList(root, "cadOnly"),
                blockIdx,
                iconRegions,
                iconMissing,
                iconExtra,
                deskewAngle
            );
        }
        catch (Exception ex)
        {
            return empty with { Error = $"EXC {ex.GetType().Name}: {ex.Message}" };
        }
        finally
        {
            try { File.Delete(inFile); } catch { }
        }
    }

    // ================= 差异标记 v2（照片直接标示）=================
    // 在整张照片上直接画每块 ROI 外框 + 逐文本行框（位置由 OCR 文本行归属得到，可靠）；
    // 跨块污染在 Python 端被构造性消除。返回带标示照片 URL + 每块结构化数据。
    // 与 v1 MarkDifferences 共享 _diffLock 串行化（ONNX 文件锁约束），不改动任何现有端点/匹配逻辑。
    public PhotoDiffResult MarkDifferencesOnPhoto(int drawingId, IFormFile file)
    {
        _diffLock.Wait();
        try
        {
            var r = MarkDifferencesOnPhotoCore(drawingId, file);
            Thread.Sleep(600); // 释放后等待 ONNX 模型文件锁回收
            return r;
        }
        finally
        {
            _diffLock.Release();
        }
    }

    private PhotoDiffResult MarkDifferencesOnPhotoCore(int drawingId, IFormFile file)
    {
        var empty = new PhotoDiffResult(false, "init", null, 0.0, new());
        if (file == null || file.Length == 0) return empty with { Error = "未收到照片文件" };
        if (drawingId <= 0) return empty with { Error = "参数错误" };

        var segDir = SegDir(drawingId);
        if (!Directory.Exists(segDir)) return empty with { Error = "图纸未切割" };
        // ⚠️ Python 的 run_on_photo 把传入的 seg_root 当作 base 目录，自行拼接 <did>/blocks/...
        // 因此不能把 per-drawing 目录（SegDir）当作 seg_root 传入，否则会双嵌成 seg/112/112/blocks/... 导致块参考图缺失、
        // LCD 屏显检测被跳过、图标全员判缺失 -> 该块（did112 blk1）误标红。这里传 base 目录。
        var segRoot = Path.Combine(_dataDir, "seg");
        var stamp = Guid.NewGuid().ToString("N").Substring(0, 12);
        var inFile = Path.Combine(segDir, $"diff_photo_in_{stamp}.jpg");
        var outFile = Path.Combine(segDir, $"diff_photo_out_{stamp}.jpg");
        try
        {
            using (var ms = new MemoryStream())
            {
                file.CopyTo(ms);
                File.WriteAllBytes(inFile, ms.ToArray());
            }

            var diffScript = _segmentScript.Replace("segment_blocks.py", "diff_visualizer.py");
            if (!File.Exists(diffScript))
                return empty with { Error = "diff_visualizer.py 缺失" };

            // 复用 diff_visualizer.py photo 模式：photo <photo> <did> <output> [db_path] [seg_root]
            string jsonText = "", stderr = "";
            int exitCode = -1;
            for (int attempt = 1; attempt <= 3; attempt++)
            {
                var psi = _ocr.CreatePythonPsi(diffScript, "photo", inFile, drawingId.ToString(), outFile, _dbPath, segRoot);
                using (var p = Process.Start(psi)!)
                {
                    var outTask = p.StandardOutput.ReadToEndAsync();
                    var errTask = p.StandardError.ReadToEndAsync();
                    var exited = p.WaitForExit(60_000);
                    if (!exited) { try { p.Kill(); } catch { } }
                    Task.WaitAll(outTask, errTask);
                    jsonText = outTask.Result ?? "";
                    stderr = errTask.Result ?? "";
                    exitCode = p.ExitCode;
                }
                if (!string.IsNullOrWhiteSpace(jsonText) && exitCode == 0) break;
                if (attempt < 3) Thread.Sleep(800 * attempt);
            }
            Console.WriteLine($"[DIFF-PHOTO-DEBUG] exitCode={exitCode} stdout_len={jsonText.Length} stderr_len={stderr.Length}");

            if (string.IsNullOrWhiteSpace(jsonText))
                return empty with { Error = $"Python 无输出；stderr={stderr[..Math.Min(200, stderr.Length)]}" };
            int startIdx = jsonText.IndexOf('{');
            int endIdx = jsonText.LastIndexOf('}');
            if (startIdx < 0 || endIdx < 0 || endIdx <= startIdx)
                return empty with { Error = $"stdout 无 JSON 段：{jsonText[..Math.Min(200, jsonText.Length)]}" };
            var jsonSegment = jsonText.Substring(startIdx, endIdx - startIdx + 1);
            using var doc = JsonDocument.Parse(jsonSegment);
            var root = doc.RootElement;
            var ok = root.TryGetProperty("success", out var sEl) && sEl.GetBoolean();
            if (!ok)
            {
                var err = root.TryGetProperty("error", out var eEl) ? eEl.GetString() : "diff_photo_failed";
                return empty with { Error = err ?? "diff_photo_failed" };
            }

            double deskewAngle = 0.0;
            if (root.TryGetProperty("deskewAngle", out var da) && da.ValueKind == JsonValueKind.Number)
                deskewAngle = da.GetDouble();

            var blocks = new List<PhotoDiffBlock>();
            if (root.TryGetProperty("blocks", out var blkArr) && blkArr.ValueKind == JsonValueKind.Array)
            {
                foreach (var bEl in blkArr.EnumerateArray())
                {
                    int bidx = bEl.TryGetProperty("bidx", out var bi) && bi.ValueKind == JsonValueKind.Number ? bi.GetInt32() : -1;
                    int[]? roi = null;
                    if (bEl.TryGetProperty("roi", out var roiEl) && roiEl.ValueKind == JsonValueKind.Array && roiEl.GetArrayLength() == 4)
                    {
                        roi = new[] { roiEl[0].GetInt32(), roiEl[1].GetInt32(), roiEl[2].GetInt32(), roiEl[3].GetInt32() };
                    }
                    string conf = bEl.TryGetProperty("conf", out var cf) ? cf.GetString() ?? "low" : "low";
                    string status = bEl.TryGetProperty("status", out var st) ? st.GetString() ?? "low_conf" : "low_conf";
                    int photoLineCount = bEl.TryGetProperty("photoLineCount", out var plc) && plc.ValueKind == JsonValueKind.Number ? plc.GetInt32() : 0;
                    int iconMissing = 0, iconExtra = 0;
                    if (bEl.TryGetProperty("icons", out var ic) && ic.ValueKind == JsonValueKind.Object)
                    {
                        if (ic.TryGetProperty("missing", out var im) && im.ValueKind == JsonValueKind.Number) iconMissing = im.GetInt32();
                        if (ic.TryGetProperty("extra", out var ie) && ie.ValueKind == JsonValueKind.Number) iconExtra = ie.GetInt32();
                    }
                    var lines = new List<PhotoDiffLine>();
                    if (bEl.TryGetProperty("lines", out var lnArr) && lnArr.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var lnEl in lnArr.EnumerateArray())
                        {
                            string lstatus = lnEl.TryGetProperty("status", out var ls) ? ls.GetString() ?? "" : "";
                            string ltext = lnEl.TryGetProperty("text", out var lt) ? lt.GetString() ?? "" : "";
                            int[]? lbbox = null;
                            if (lnEl.TryGetProperty("bbox", out var lb) && lb.ValueKind == JsonValueKind.Array && lb.GetArrayLength() >= 4)
                                lbbox = new[] { lb[0].GetInt32(), lb[1].GetInt32(), lb[2].GetInt32(), lb[3].GetInt32() };
                            if (lbbox != null) lines.Add(new PhotoDiffLine(lstatus, lbbox, ltext));
                        }
                    }
                    blocks.Add(new PhotoDiffBlock(bidx, roi, conf, status, lines, iconMissing, iconExtra, photoLineCount));
                }
            }

            var url = $"/api/drawings/diff-photo-image?did={drawingId}&file={Path.GetFileName(outFile)}";
            return new PhotoDiffResult(true, null, url, deskewAngle, blocks);
        }
        catch (Exception ex)
        {
            return empty with { Error = $"EXC {ex.GetType().Name}: {ex.Message}" };
        }
        finally
        {
            try { File.Delete(inFile); } catch { }
        }
    }

    /// <summary>服务照片直接标示图（JPEG）</summary>
    public IResult ServePhotoDiffImage(int drawingId, string fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName) || fileName.Contains("..") || fileName.Contains("/") || fileName.Contains("\\"))
            return Results.BadRequest(new { error = "非法文件名" });
        var path = Path.Combine(SegDir(drawingId), fileName);
        if (!File.Exists(path)) return Results.NotFound(new { error = "照片标示图未生成" });
        return Results.File(path, "image/jpeg", fileName);
    }

    /// <summary>服务差异标记图（PNG，v1 兼容端点 /diff-image 使用）</summary>
    public IResult ServeDiffImage(int drawingId, string fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName) || fileName.Contains("..") || fileName.Contains("/") || fileName.Contains("\\"))
            return Results.BadRequest(new { error = "非法文件名" });
        var path = Path.Combine(SegDir(drawingId), fileName);
        if (!File.Exists(path)) return Results.NotFound(new { error = "差异图未生成" });
        return Results.File(path, "image/png", fileName);
    }

    // ================= 差异标记自动计算（嵌入匹配结果一并返回）=================
    private DiffMarkers? ComputeDiffFromPath(int drawingId, string? photoPath, BlockMatchResult matchResult)
    {
        if (string.IsNullOrEmpty(photoPath) || !File.Exists(photoPath)) {
            Console.WriteLine($"[DIFF-DEBUG] photoPath missing: {photoPath}");
            return null;
        }
        try
        {
            // 仅对 full/partial 匹配计算差异
            if (matchResult.MatchStatus is not ("full" or "partial")) {
                Console.WriteLine($"[DIFF-DEBUG] status not full/partial: {matchResult.MatchStatus}");
                return null;
            }
            int blockIdx = matchResult.Candidates.Count > 0 ? matchResult.Candidates[0].Idx : -1;
            if (blockIdx < 0) {
                Console.WriteLine($"[DIFF-DEBUG] blockIdx invalid: {blockIdx}");
                return null;
            }

            var segDir = SegDir(drawingId);
            var blockPng = Path.Combine(segDir, "blocks", $"{drawingId}_blk_{blockIdx:D2}.png");
            if (!File.Exists(blockPng)) {
                Console.WriteLine($"[DIFF-DEBUG] blockPng missing: {blockPng}");
                return null;
            }
            Console.WriteLine($"[DIFF-DEBUG] computing diff: photo={photoPath} block={blockPng}");
            var stamp = Guid.NewGuid().ToString("N").Substring(0, 12);
            var outFile = Path.Combine(segDir, $"diff_{blockIdx}_{stamp}.jpg");  // ⭐ JPEG 输出（体积小，手机加载快）

            // 块 bbox 缓存（A2：优先持久化 DB → 内存兜底 → 实时 OCR）
            var bboxCacheKey = $"{drawingId}_{blockIdx}";
            var (persistedBbox, persistedIcons) = LoadBlockBBoxIcons(drawingId, blockIdx);
            string? cachedBbox = persistedBbox;
            if (string.IsNullOrEmpty(cachedBbox))
                _blockBboxCache.TryGetValue(bboxCacheKey, out cachedBbox);  // 内存兜底

            // ⭐ 取块的入库 RawText（与匹配阶段一致），传给 Python 做文本对比，
            //   避免块图 OCR 与入库文本不一致导致"块中缺失字符"等假差异
            string? blockRawText = null;
            try
            {
                var matchedBlock = GetBlocks(drawingId).FirstOrDefault(b => b.BIdx == blockIdx);
                if (matchedBlock != null && !string.IsNullOrWhiteSpace(matchedBlock.RawText))
                    blockRawText = matchedBlock.RawText;
            }
            catch { }

            // 调用 diff_visualizer.py
            // ⭐ 第4~8参统一占位（空串），保证 Python 端位置稳定、绝不因某参缺失而错位
            var diffScript = _segmentScript.Replace("segment_blocks.py", "diff_visualizer.py");
            var psi = _ocr.CreatePythonPsi(diffScript, photoPath, blockPng, outFile);
            psi.ArgumentList.Add(cachedBbox ?? "");              // 第4参：bbox cache（块文本+bbox，免块 OCR）
            psi.ArgumentList.Add(blockRawText ?? "");            // 第5参：块 RawText override
            psi.ArgumentList.Add(matchResult.PhotoText ?? "");   // 第6参：照片 OCR 文本 override（免照片 OCR）
            psi.ArgumentList.Add("");                            // 第7参：保留
            psi.ArgumentList.Add(persistedIcons ?? "");          // 第8参：图标框 override（DB 持久化，免图标检测）

            string jsonText = "";
            for (int attempt = 1; attempt <= 2; attempt++)
            {
                using var p = Process.Start(psi)!;
                var outTask = p.StandardOutput.ReadToEndAsync();
                var errTask = p.StandardError.ReadToEndAsync();
                var exited = p.WaitForExit(60_000);
                if (!exited) { try { p.Kill(); } catch { } }
                Task.WaitAll(outTask, errTask);
                jsonText = outTask.Result ?? "";
                var err = errTask.Result ?? "";
                // ⭐ 总是记录 stderr（成功时也可能有 [DIFF-DBG] 等调试输出）
                if (!string.IsNullOrWhiteSpace(err))
                    Console.WriteLine($"[DIFF-PY-STDERR] exit={p.ExitCode} stderr={err.Substring(0, Math.Min(800, err.Length))}");
                if (!string.IsNullOrWhiteSpace(jsonText) && p.ExitCode == 0) break;
                if (attempt < 2) Thread.Sleep(400);
            }
            if (string.IsNullOrWhiteSpace(jsonText)) {
                Console.WriteLine($"[DIFF-DEBUG] jsonText empty after {2} attempts, returning null");
                return null;
            }
            int si = jsonText.IndexOf('{'), ei = jsonText.LastIndexOf('}');
            if (si < 0 || ei < 0) return null;
            using var doc = JsonDocument.Parse(jsonText.Substring(si, ei - si + 1));
            var root = doc.RootElement;
            if (!root.TryGetProperty("success", out var ok) || !ok.GetBoolean()) return null;

            // A2：持久化块 bbox + 图标框 到 DB（避免下次重跑块 OCR/图标检测；重启后仍可用）
            // ⭐ 从 Python 返回的 blockLines(块文本+bbox) / iconRegions(图标框) 提取，合并写入
            string? newBbox = (root.TryGetProperty("blockLines", out var blines) && blines.ValueKind == JsonValueKind.Array)
                ? blines.GetRawText() : null;
            string? newIcons = (root.TryGetProperty("iconRegions", out var iRegions) && iRegions.ValueKind == JsonValueKind.Array)
                ? iRegions.GetRawText() : null;
            if (newBbox != null || newIcons != null)
            {
                if (newBbox != null) _blockBboxCache.TryAdd(bboxCacheKey, newBbox);  // 内存 L1 也更新
                var (existingBbox, existingIcons) = LoadBlockBBoxIcons(drawingId, blockIdx);
                SaveBlockBBoxIcons(drawingId, blockIdx, newBbox ?? existingBbox, newIcons ?? existingIcons);
            }

            // 解析为 DiffMarkers
            var url = $"/api/drawings/diff-image?did={drawingId}&file={Path.GetFileName(outFile)}";
            var photoEx = ParseStringList(root, "photoExclusive");
            var blockEx = ParseStringList(root, "blockExclusive");
            var cadOnly = ParseStringList(root, "cadOnly");
            // ⭐ 图标区域独立解析（紫色框=到位；缺图标红框；多图标仅计数）
            var iconRegions = ParseRegions(root, "iconRegions", new List<string>());
            var iconMissing = ParseRegions(root, "iconMissingRegions", new List<string>());
            var iconExtra = ParseRegions(root, "iconExtraRegions", new List<string>());
            return new DiffMarkers(
                ParseRegions(root, "redRegions", blockEx),
                ParseRegions(root, "yellowRegions", photoEx),
                ParseRegions(root, "grayRegions", cadOnly),
                ParseRegions(root, "greenRegions", new List<string>()),
                photoEx, blockEx, cadOnly, url,
                iconRegions,
                iconMissing,
                iconExtra,
                ParseCharDiffList(root, "missingChars"),
                ParseCharDiffList(root, "extraChars")
            );
        }
        catch (Exception ex) {
            Console.WriteLine($"[DIFF-DEBUG] EXC: {ex.GetType().Name}: {ex.Message}");
            return null;  // 静默失败：差异标记是附加功能，不影响匹配结果
        }
    }

    private static List<DiffRegion> ParseRegions(JsonElement root, string key, List<string> texts)
    {
        var lst = new List<DiffRegion>();
        if (!root.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array) return lst;
        int i = 0;
        foreach (var el in arr.EnumerateArray())
        {
            if (el.ValueKind != JsonValueKind.Array || el.GetArrayLength() < 4) { i++; continue; }
            var text = i < texts.Count ? texts[i] : "";
            lst.Add(new DiffRegion(el[0].GetInt32(), el[1].GetInt32(), el[2].GetInt32(), el[3].GetInt32(), text, ""));
            i++;
        }
        return lst;
    }

    private static List<string> ParseStringList(JsonElement root, string key)
    {
        var lst = new List<string>();
        if (root.TryGetProperty(key, out var a) && a.ValueKind == JsonValueKind.Array)
            foreach (var e in a.EnumerateArray())
                if (e.ValueKind == JsonValueKind.String) lst.Add(e.GetString() ?? "");
        return lst;
    }

    private static List<CharDiff> ParseCharDiffList(JsonElement root, string key)
    {
        var lst = new List<CharDiff>();
        if (!root.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array) return lst;
        foreach (var e in arr.EnumerateArray())
        {
            if (e.ValueKind != JsonValueKind.Object) continue;
            var blockText = e.TryGetProperty("blockText", out var bEl) ? bEl.GetString() ?? "" : "";
            var missing = e.TryGetProperty("missing", out var mEl) ? mEl.GetString() ?? "" : "";
            var photoTexts = new List<string>();
            if (e.TryGetProperty("photoTexts", out var pEl) && pEl.ValueKind == JsonValueKind.Array)
                foreach (var pt in pEl.EnumerateArray())
                    if (pt.ValueKind == JsonValueKind.String) photoTexts.Add(pt.GetString() ?? "");
            var positions = new List<int>();
            if (e.TryGetProperty("positions", out var posEl) && posEl.ValueKind == JsonValueKind.Array)
                foreach (var p in posEl.EnumerateArray())
                    if (p.ValueKind == JsonValueKind.Number) positions.Add(p.GetInt32());
            lst.Add(new CharDiff(blockText, photoTexts, missing, positions.Count > 0 ? positions : null));
        }
        return lst;
    }

    // ---------- 历史 ----------
    public record HistoryDto(int Id, string QueryType, string? DetectedModel, string? DetectedPart,
                             bool Matched, string CreatedAt, string? SnapshotPath);

    /// <summary>读取历史记录（最近优先）。</summary>
    public List<HistoryDto> GetHistory(int limit = 50)
    {
        var list = new List<HistoryDto>();
        using var conn = _db.Open();
        using var c = conn.CreateCommand();
        c.CommandText = @"
            SELECT h.Id,h.QueryType,h.DetectedModel,h.DetectedPart,h.Matched,h.CreatedAt,
                   h.SnapshotPath
            FROM drawing_histories h
            ORDER BY h.Id DESC LIMIT $l";
        c.Parameters.AddWithValue("$l", limit > 0 ? limit : 50);
        using var r = c.ExecuteReader();
        while (r.Read())
        {
            list.Add(new HistoryDto(
                r.GetInt32(0), r.GetString(1),
                r.IsDBNull(2) ? null : r.GetString(2),
                r.IsDBNull(3) ? null : r.GetString(3),
                r.GetInt32(4) == 1, r.GetString(5),
                r.IsDBNull(6) ? null : r.GetString(6)
            ));
        }
        return list;
    }

    // [recovered v9 methods] re-added from pre-corruption source with clean text
    public record SyncResult(int Added, int Skipped, List<AddDrawingResult> AddedItems);

    // 扫描 Files:Root 文件夹，把磁盘上存在但 drawings 表尚缺的 PDF 入库（仅 INSERT，懒切割+OCR）。只增不删。
    public SyncResult SyncFolder()
    {
        // 1) 已有 VirtualPath 集合（与 INSERT 同格式 "drawings/{name}"）
        var existing = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var conn = _db.Open())
        using (var c = conn.CreateCommand())
        {
            c.CommandText = "SELECT VirtualPath FROM drawings WHERE VirtualPath IS NOT NULL";
            using var r = c.ExecuteReader();
            while (r.Read()) existing.Add(r.GetString(0));
        }

        // 2) 扫描文件夹（与 ListDrawings / InitLibrary / ResolvePhysical 同一根目录，TopDirectoryOnly）
        var files = _files.ListDrawings();
        var now = DateTime.Now.ToString("o");
        var added = new List<AddDrawingResult>();
        int skipped = 0;

        foreach (var f in files)
        {
            var vp = "drawings/" + f.Name;
            if (existing.Contains(vp)) { skipped++; continue; }   // 已入库则跳过

            var model = ModelParser.ExtractModel(f.Name);          // 与 InitLibrary 一致（允许 null）
            try
            {
                using var conn = _db.Open();
                int newId;
                using (var ins = conn.CreateCommand())
                {
                    ins.CommandText = "INSERT INTO drawings (Model,FileName,VirtualPath,CreatedAt) VALUES ($m,$fn,$vp,$t)";
                    ins.Parameters.AddWithValue("$m", (object?)model ?? DBNull.Value);
                    ins.Parameters.AddWithValue("$fn", f.Name);
                    ins.Parameters.AddWithValue("$vp", vp);
                    ins.Parameters.AddWithValue("$t", now);
                    ins.ExecuteNonQuery();
                }
                using (var idc = conn.CreateCommand()) { idc.CommandText = "SELECT last_insert_rowid()"; newId = Convert.ToInt32(idc.ExecuteScalar()); }
                added.Add(new AddDrawingResult(newId, f.Name, vp, model, null));
                existing.Add(vp); // 防同批重名重复插入
            }
            catch (Exception ex)
            {
                // 单张失败不影响其它；结构化记录，不抛 500
                added.Add(new AddDrawingResult(null, f.Name, vp, model, "入库失败" + ex.Message));
            }
        }
        return new SyncResult(added.Count(x => x.Error is null), skipped, added);
    }

    // 从型号字符串提取关键词串用于模糊匹配。例 "PC-P1HJQ" -> ["PCP","CP1","P1H","1HJ","HJQ","PCP1H", ...]
    // OCR 常把完整型号拆成多段 token（如 PC / CJ / JUG），用子串 Contains 能跨 token 命中。
    private static List<string> ExtractModelSubstrings(string model)
    {
        var subs = new List<string>();
        if (string.IsNullOrWhiteSpace(model)) return subs;
        // 移除常见分隔符后取所有长度的滑动窗口子串
        var clean = model.Replace("-", "").Replace("_", "");
        for (int len = 3; len <= clean.Length; len++)
            for (int i = 0; i <= clean.Length - len; i++)
                subs.Add(clean.Substring(i, len));
        // 也保留原始格式中的分隔段（如 PC-、P1H）
        foreach (var seg in model.Split('-', '_'))
            if (seg.Length >= 2) subs.Add(seg);
        return subs;
    }

    // 裸品牌词（HITACHI / HYXC 等 logo 标识，无型号码）-> 装饰元素，排除。
    private static bool ContainsBrand(string raw)
        => ModelParser.ContainsFuzzy(raw, "HITACHI")
           || ModelParser.ContainsFuzzy(raw, "HYXC");

    // [v9] 照片型号守卫：判断照片 OCR 文本是否包含任何型号/服务热线特征。
    // 防止安全警示(LOW VOLTAGE/禁止强电)、标题栏(模具编号)等非打标区被几何或弱文本误判为 matched。
    // 匹配条件（满足任一即通过）：
    //   1. 包含图纸型号的关键子串（如 P1HVQ / P1HJQ 等）
    //   2. 包含 >=7 位连续数字（服务热线 400-860-1111 类）
    //   3. 包含 "400" + "860"/"111" 组合模式
    //   4. 包含中文 "服务热线" 关键词
    private static bool PhotoContainsModelHint(string? photoText, string? drawModel)
    {
        if (string.IsNullOrWhiteSpace(photoText)) return false;
        var t = photoText.ToUpperInvariant();

        // 条件1：型号子串命中
        if (!string.IsNullOrWhiteSpace(drawModel))
        {
            var subs = ExtractModelSubstrings(drawModel!);
            if (subs.Any(s => t.Contains(s, StringComparison.OrdinalIgnoreCase)))
                return true;
        }

        // 条件2：>=7 位连续数字
        if (Regex.IsMatch(t, @"\d{7,}")) return true;

        // 条件3：服务热线特征组合
        if (t.Contains("400") && (t.Contains("860") || t.Contains("111"))) return true;

        // 条件4：中文"服务热线"
        if (photoText.Contains("服务热线")) return true;

        return false;
    }

}
