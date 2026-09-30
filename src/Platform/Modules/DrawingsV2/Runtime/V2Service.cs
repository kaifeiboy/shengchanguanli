using System.Collections.Concurrent;
using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using Platform.Infrastructure;
using Platform.Modules.DrawingsV2.Decision;

namespace Platform.Modules.DrawingsV2.Runtime;

/// <summary>
/// v2 服务层：编排「定位图纸 → 调感知层 → 构造应打标对象 → 落库」，不碰 HTTP 类型。
///
/// <para>【复用的平台设施】<see cref="FileAccessService"/>：延续虚拟路径 + 白名单 +
/// 路径穿越防护，使 v2 与旧模块**读同一份原始图纸目录**（用户约束 2：原始图纸与文件夹继续保留调用），
/// 且无需自己实现任何路径安全逻辑。</para>
///
/// <para>【职责边界】本层只做编排，判定逻辑全在 <see cref="MarkBuilder"/>，
/// 进程管理全在 <see cref="V2Python"/>，数据全在 <see cref="V2Store"/>。</para>
/// </summary>
public sealed class V2Service
{
    private readonly V2Python _py;
    private readonly V2Store _store;
    private readonly FileAccessService _files;
    private readonly ILogger<V2Service> _logger;
    private readonly bool _requireReviewedProfiles;
    private readonly bool _visionFallbackEnabled;
    // M1（#35）照片指纹缓存：完全相同的输入直接复用历史结论。默认开启，
    // 可用 DrawingsV2:PhotoCache=false 关闭（规则调试 / 怀疑缓存掩盖问题时用）。
    private readonly bool _photoCache;
    // M2（#35）对象级确认记忆：把「上次人工结论」随本次结果一起返回（只读回显，不改判定）。
    private readonly bool _objectMemory;

    // A（#37）图纸侧清单净化（排除屏显/页脚/二维码格式说明污染），默认开。
    private readonly bool _excludePollution;
    // B（#37）部位覆盖判据（照片未拍到该部位 → 灰而非红），默认开，仅配准可信时生效。
    private readonly bool _partCoverageGate;

    // M7（库管理）图纸根目录 + 逻辑块异步抽取队列（序列化 Python 子进程，避免并发 OOM）
    private readonly string _filesRoot;
    private readonly SemaphoreSlim _extractSem = new(1, 1);
    private readonly ConcurrentDictionary<long, byte> _extractQueued = new();

    // ⚠️ M3（#36）严格 Gate 自动放行 —— 已于 2026-09-27 整体删除。
    // 原因：新匹配范式（照片 OCR 文本命中图块打标内容）下判定不依赖 confidence 标定，
    // 真值集作废 → Gate 无标定依据。字段/方法/响应字段一并移除（已查 H5 未消费这些字段）。

    /// <summary>
    /// 照片存储目录（内容寻址）。默认在 v2 库同级的 `drawingsv2_photos/`，
    /// 与旧模块的历史照片目录物理隔离 —— 试点期互不干扰，回滚只需删目录。
    /// </summary>
    public string PhotoDir { get; }

    public V2Service(V2Python py, V2Store store, FileAccessService files,
                     IConfiguration config, ILogger<V2Service> logger)
    {
        _py = py;
        _store = store;
        _files = files;
        _logger = logger;
        _requireReviewedProfiles = !bool.TryParse(config["DrawingsV2:RequireReviewedProfiles"], out var requireReviewed)
                                   || requireReviewed;
        _photoCache = !bool.TryParse(config["DrawingsV2:PhotoCache"], out var photoCache) || photoCache;
        _objectMemory = !bool.TryParse(config["DrawingsV2:ObjectMemory"], out var objectMemory) || objectMemory;


        // A（#37）：图纸侧清单净化默认开（配置 DrawingsV2:ExcludePollution=false 关闭）。
        _excludePollution = !bool.TryParse(config["DrawingsV2:ExcludePollution"], out var excludePollution) || excludePollution;
        // B（#37）：部位覆盖判据默认开（配置 DrawingsV2:PartCoverageGate=false 关闭）。
        _partCoverageGate = !bool.TryParse(config["DrawingsV2:PartCoverageGate"], out var partCoverage) || partCoverage;

        // 视觉兜底（方案 §1.8）默认开启 —— 关闭就完全失去转曲内容的召回，等于没做 P0-1。
        // 代价是单页多约 33s（2026-09-22 实测），可用 DrawingsV2:VisionFallback=false 关掉。
        var vf = config["DrawingsV2:VisionFallback"];
        _visionFallbackEnabled = !string.Equals(vf, "false", StringComparison.OrdinalIgnoreCase)
                                 && !string.Equals(vf, "0", StringComparison.OrdinalIgnoreCase);

        var configured = config["DrawingsV2:PhotoDir"];
        PhotoDir = !string.IsNullOrWhiteSpace(configured)
            ? configured!
            : Path.Combine(Path.GetDirectoryName(_store.DbPath) ?? ".", "drawingsv2_photos");
        try { Directory.CreateDirectory(PhotoDir); } catch { }

        // M7（库管理）图纸根目录：新增图纸落盘到 Files:Root/drawings/，与 ResolvePdf 解析约定一致。
        _filesRoot = config["Files:Root"] ?? @"E:\生产打标效果图";
        try { if (!Directory.Exists(_filesRoot)) Directory.CreateDirectory(_filesRoot); } catch { }
    }

    private static string Sha256Of(string path)
    {
        using var fs = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(fs)).ToLowerInvariant();
    }

    /// <summary>健康检查：Python 解释器 / 脚本定位 / 感知层自检。</summary>
    public async Task<object> Health()
    {
        var scriptExists = File.Exists(_py.ScriptPath);
        string? selfCheck = null;
        string? err = null;
        if (scriptExists)
        {
            try { selfCheck = (await _py.SelfCheckAsync()).Trim(); }
            catch (Exception e) { err = e.Message; }
        }
        return new
        {
            ok = scriptExists && err is null,
            pythonExe = _py.PythonExe,
            pythonExeExists = File.Exists(_py.PythonExe),
            scriptPath = _py.ScriptPath,
            scriptExists,
            dbPath = _store.DbPath,
            decisionVersion = V2Store.DecisionVersion,
            selfCheck,
            error = err
        };
    }

    /// <summary>模块概览：库内档案数 / mark 数 / 规则分布。</summary>
    public object Meta()
    {
        var c = _store.Counts();
        return new
        {
            dbPath = _store.DbPath,
            decisionVersion = V2Store.DecisionVersion,
            counts = c,
            rules = _store.RuleCounts()
        };
    }

    /// <summary>
    /// 剖析一份图纸：感知层解析 → 决策层构造 → 落库。
    /// </summary>
    /// <param name="nameOrPath">
    /// 图纸文件名（如 "02-PC-P1HEQ2-效果图.pdf"）或虚拟路径（"drawings/xxx.pdf"）；
    /// 也接受绝对路径（便于离线验证台直接喂文件）。
    /// </param>
    public async Task<object> AnalyzeAsync(string nameOrPath, CancellationToken ct = default)
    {
        var phys = ResolvePdf(nameOrPath)
                   ?? throw new V2Exception($"找不到图纸：{nameOrPath}");

        var key = Path.GetFileNameWithoutExtension(phys);

        // ---- 视觉兜底（方案 §1.8）：补回转曲（outline）内容 ----
        // 与解析复用同一份 vpdf JSON —— 实测重复 parse 一份 17208 条路径的图纸要 3.0s，属于纯浪费。
        // 只跑第 0 页：当前面向单页效果图；多页图纸其余页暂不补（需要时打开页循环，耗时线性增长）。
        string? rawJsonPath = _visionFallbackEnabled
            ? Path.Combine(Path.GetTempPath(), $"v2-{Guid.NewGuid():N}.vpdf.json")
            : null;
        var doc = await _py.ParseAsync(phys, ct, rawJsonPath);

        VisionFallbackResult? fb = null;
        List<VisionText>? vision = null;
        string? fbError = null;
        if (rawJsonPath is not null)
        {
            try
            {
                var fbJson = await _py.FallbackAsync(phys, File.Exists(rawJsonPath) ? rawJsonPath : null, 0, ct);
                fb = VectorPdfParser.ParseFallback(fbJson);
                var pageIdx = fb.Page!.Index;
                vision = (fb.Page!.Texts ?? new List<VisionText>())
                    .Where(t => !string.IsNullOrWhiteSpace(t.Text))
                    .Select(t => { t.PageIndex = pageIdx; return t; })
                    .ToList();
            }
            catch (Exception e)
            {
                // 兜底失败不阻断主流程：矢量层结论仍然成立，只是少了转曲内容的补充
                fbError = e.Message;
                _logger.LogWarning(e, "视觉兜底失败（不阻断主流程）：{Key}", key);
            }
            finally
            {
                try { File.Delete(rawJsonPath); } catch { }
            }
        }

        var res = MarkBuilder.Build(doc, key, vision, excludePollution: _excludePollution);
        res.VisionRegions = fb?.Page?.Regions?.Count ?? 0;
        res.VisionTexts = vision?.Count ?? 0;
        if (fbError is not null) res.Warnings.Add("视觉兜底失败：" + fbError);
        var profileId = _store.SaveProfile(doc, res, phys, "vpdf-py-1.1.0");

        _logger.LogInformation("v2 剖析完成：{Key} marks={Marks} rules={Rules}",
            key, res.Marks.Count, JsonSerializer.Serialize(res.RuleHits));

        return new
        {
            profileId,
            drawingKey = key,
            pdfPath = phys,
            sha256 = doc.Source?.Sha256,
            schema = doc.Schema,
            pages = doc.Pages?.Count ?? 0,
            visionFallbackRequired = res.VisionFallbackRequired,
            visionFallback = new
            {
                enabled = _visionFallbackEnabled,
                regions = res.VisionRegions,
                texts = res.VisionTexts,
                totalMs = fb?.Diagnostics?.TotalMs ?? 0,
                ocrCalls = fb?.Diagnostics?.Ocr?.OcrCalls ?? 0,
                rejectedBySpanOverlap = fb?.Diagnostics?.Geometry?.RejectedBySpanOverlap ?? 0,
                droppedAsSpanDuplicate = fb?.Diagnostics?.Ocr?.DroppedAsSpanDuplicate ?? 0,
                error = fbError
            },
            declaredItems = res.DeclaredItems,
            scopes = res.Scopes,
            warnings = res.Warnings,
            ruleHits = res.RuleHits,
            markCount = res.Marks.Count,
            marks = res.Marks.Select(m => ToDto(m)).ToList()
        };
    }

    /// <summary>已剖析档案列表。</summary>
    public IReadOnlyList<object> Profiles() => _store.ListProfiles();

    // ─────────────────────────────────────────────────────────────
    // M7（库管理）：V2 增加图纸 —— 落盘 → 剖析登记 → 异步抽取逻辑块
    // 取代 V1 的 add-batch / sync。V2 比对依赖 OCR 抽出的逻辑块，
    // 故「登记」后必须主动触发抽取，不能只靠首次比对懒触发。
    // ─────────────────────────────────────────────────────────────

    public record ImportResult(long? ProfileId, string FileName, string? DrawingKey, string? Error);

    /// <summary>导入单张图纸：落盘 → AnalyzeAsync 登记（status=draft）→ 入队抽取逻辑块。</summary>
    public async Task<ImportResult> ImportOneAsync(Stream stream, string fileName, string? model, CancellationToken ct)
    {
        try
        {
            var staged = StageDrawing(stream, fileName);
            await AnalyzeAsync(staged.drawingKey, ct);
            var found = _store.FindLatestProfile(staged.drawingKey)
                        ?? throw new V2Exception($"剖析后未找到档案：{staged.drawingKey}");
            _store.SetBlockStatus(found.Id, "pending", null);
            EnqueueBlockExtraction(found.Id);
            return new ImportResult(found.Id, staged.safeName, staged.drawingKey, null);
        }
        catch (Exception e)
        {
            return new ImportResult(null, fileName, null, e.Message);
        }
    }

    /// <summary>扫描图纸根目录，对库缺的 PDF 自动登记 + 入队抽取。只增不删。</summary>
    public async Task<object> SyncFolderAsync(CancellationToken ct)
    {
        var existing = _store.AllDrawingKeys();
        var added = new List<ImportResult>();
        int skipped = 0;
        foreach (var f in _files.ListDrawings())
        {
            var key = Path.GetFileNameWithoutExtension(f.Name);
            if (existing.Contains(key)) { skipped++; continue; }
            try
            {
                await AnalyzeAsync(key, ct);
                var found = _store.FindLatestProfile(key)
                            ?? throw new V2Exception($"剖析后未找到档案：{key}");
                _store.SetBlockStatus(found.Id, "pending", null);
                EnqueueBlockExtraction(found.Id);
                added.Add(new ImportResult(found.Id, f.Name, key, null));
                existing.Add(key);
            }
            catch (Exception e)
            {
                added.Add(new ImportResult(null, f.Name, key, e.Message));
            }
        }
        return new { added = added.Count(x => x.Error is null), skipped, items = added };
    }

    /// <summary>查询档案逻辑块抽取状态。</summary>
    public object? GetBlockStatus(long profileId) => _store.GetBlockStatus(profileId);

    /// <summary>把上传流落盘到 Files:Root/drawings/，安全文件名 + 同名去重。</summary>
    private (string safeName, string drawingKey, string physicalPath) StageDrawing(Stream stream, string fileName)
    {
        // PDF 魔数校验（同一流先读魔数再复位，复用给落盘）
        var head = new byte[5];
        int n = stream.Read(head, 0, 5);
        if (n < 4 || System.Text.Encoding.ASCII.GetString(head)[..4] != "%PDF")
            throw new V2Exception("仅支持 PDF 图纸");
        stream.Position = 0;

        // 与 V1 AddDrawing / FileAccessService 一致：PDF 直接落盘到 Files:Root（虚拟前缀 "drawings" 即根目录，
        // 不是子文件夹），虚拟路径 "drawings/{name}.pdf" 由 ResolvePhysical 解析回 Files:Root/{name}.pdf。
        try { if (!Directory.Exists(_filesRoot)) Directory.CreateDirectory(_filesRoot); }
        catch (Exception ex) { throw new V2Exception("无法访问图纸目录：" + ex.Message); }

        var safe = SanitizeFileName(fileName);
        var target = Path.Combine(_filesRoot, safe);
        int k = 1;
        while (File.Exists(target)) { target = Path.Combine(_filesRoot, InsertBeforeExt(safe, $"_{k}")); k++; }
        var safeName = Path.GetFileName(target);
        using (var outStream = File.Create(target))
            stream.CopyTo(outStream);
        return (safeName, Path.GetFileNameWithoutExtension(safeName), target);
    }

    /// <summary>入队逻辑块抽取：同一 profile 去重；串行化 Python 子进程（sem=1）避免并发 OOM。</summary>
    private void EnqueueBlockExtraction(long profileId)
    {
        if (!_extractQueued.TryAdd(profileId, 0)) return;   // 已在队列/抽取中
        _ = Task.Run(async () =>
        {
            try
            {
                _store.SetBlockStatus(profileId, "extracting", null);
                await _extractSem.WaitAsync();
                try { await ExtractLogicalBlocksAsync(profileId, true, CancellationToken.None); }
                finally { _extractSem.Release(); }
                _store.SetBlockStatus(profileId, "ready", null);
            }
            catch (Exception e)
            {
                _store.SetBlockStatus(profileId, "failed", e.Message);
                _logger.LogError(e, "逻辑块抽取失败 profile={Pid}", profileId);
            }
            finally { _extractQueued.TryRemove(profileId, out _); }
        });
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

    /// <summary>档案详情（含完整 mark 树）。</summary>
    public object? Profile(long id) => _store.GetProfile(id);

    /// <summary>
    /// 首件比对：图纸档案（应打标对象） ↔ 现场照片（定点观测 + 全图盲观测）→ 八态判定。
    ///
    /// <para>编排顺序：定位档案（必要时先剖析）→ 构建定点区域 → observe 盲感知（质量门槛 /
    /// 码兜底 / Extra 检出）→ observe-verify 定点感知 → MarkVerifier 判定。
    /// 判定逻辑全在 <see cref="MarkVerifier"/>，本层不写任何比对规则。</para>
    /// </summary>
    /// <param name="drawing">图纸文件名 / 虚拟路径 / 档案数字 id</param>
    /// <param name="photo">照片路径：绝对路径或平台虚拟路径</param>
    /// <param name="withObserve">是否同时跑全图盲感知（默认 true；关掉可省约一半耗时）</param>
    /// <param name="view">拍摄视图（M6 视图级配准）：MarkView 枚举名（TopCover/BottomCover/Side/Nameplate/Cable/Other），
    /// null/空时仅在强证据唯一指向某视图时自动选择，否则返回候选要求 H5 选择。</param>
    public async Task<object> CompareAsync(string drawing, string photo, bool withObserve,
                                           CancellationToken ct = default, string? view = null,
                                           string? sessionId = null)
    {
        if (string.IsNullOrWhiteSpace(drawing)) throw new V2Exception("缺少 drawing");
        if (string.IsNullOrWhiteSpace(photo)) throw new V2Exception("缺少 photo");
        var photoPhys = ResolvePhoto(photo) ?? throw new V2Exception($"找不到照片：{photo}");
        return await CompareCoreAsync(drawing, photoPhys, Path.GetFileName(photoPhys), withObserve, ct,
                                      photoSha: null, view: view, sessionId: sessionId);
    }

    /// <summary>
    /// 手机拍照上传比对：上传的照片先**内容寻址**落盘（sha256 去重），再走同一条比对链路。
    /// 照片存 v2 自己的目录（默认在 drawingsv2.db 同级的 `drawingsv2_photos/`），
    /// 不写任何其它模块的目录，也不改旧链路 —— 保证 v2 试点期与旧方案完全隔离、可随时回滚。
    /// </summary>
    /// <param name="view">拍摄视图（M6 视图级配准），见 <see cref="CompareAsync"/>。</param>
    public async Task<object> CompareUploadAsync(string tempPath, string? originalName, string drawing,
                                                 bool withObserve, CancellationToken ct = default,
                                                 string? view = null, string? sessionId = null)
    {
        if (string.IsNullOrWhiteSpace(drawing)) throw new V2Exception("缺少 drawing");
        if (string.IsNullOrWhiteSpace(tempPath) || !File.Exists(tempPath))
            throw new V2Exception($"上传临时文件不存在：{tempPath}");

        var sha = Sha256Of(tempPath);
        var ext = Path.GetExtension(originalName ?? tempPath);
        if (string.IsNullOrWhiteSpace(ext)) ext = ".jpg";
        var dir = Path.Combine(PhotoDir, DateTime.Now.ToString("yyyyMM"));
        Directory.CreateDirectory(dir);
        var stored = Path.Combine(dir, sha[..2] + "_" + sha + ext);
        if (!File.Exists(stored))
            File.Copy(tempPath, stored, overwrite: true);

        return await CompareCoreAsync(drawing, stored, originalName, withObserve, ct, sha, view, sessionId);
    }

    /// <summary>比对记录列表（历史回溯）。</summary>
    public IReadOnlyList<object> CompareRecords(int limit) => _store.ListCompareRecords(limit);

    // ---------------- 逻辑图块（P2 · 方案 §4） ----------------

    /// <summary>
    /// P2 · 逻辑图块提取并落库：调 <c>Scripts/vpdf/logical_blocks.py</c> 产出
    /// <c>logical-blocks/1</c>，解析后【整体覆盖】写入 <c>v2_logical_blocks</c> +
    /// <c>v2_block_elements</c>。
    ///
    /// <para>【为什么不随 analyze 自动跑】本步含块级局部 OCR（含判空块的 2×2 象限补扫），
    /// 单张实测 20~60s，挂在剖析主链路上会把 analyze 拖慢数倍。故做成独立端点按需触发，
    /// P3 之前不参与任何既有判定，对 compare 主链路零影响。</para>
    ///
    /// <para>【幂等】同一 profile 同一页重复调用 = 覆盖，不会累加 rows（依赖 Store 的先删后写）。</para>
    /// </summary>
    /// <param name="ocrOn">false 时只走文本层：判空会退化为「仅文本层判空」，
    /// 可能误杀转曲/栅格化部位，仅供调试与快速预览。</param>
    public async Task<object> ExtractLogicalBlocksAsync(long profileId, bool ocrOn = true,
        CancellationToken ct = default)
    {
        var loc = _store.GetProfileLocation(profileId)
                  ?? throw new V2Exception($"档案不存在：{profileId}");
        // ?? throw 已把 (…)? 解包为具名元组本体，此处**不能**再写 .Value
        var drawingKey = loc.drawingKey;
        var pdfPath = loc.pdfPath;
        if (string.IsNullOrWhiteSpace(pdfPath) || !File.Exists(pdfPath))
            throw new V2Exception($"档案 {profileId} 的图纸不存在：{pdfPath}");

        var sw = System.Diagnostics.Stopwatch.StartNew();
        // 人工块框 override（2026-09-30，66/72 密排图纸分块标定）：
        // data/drawingsv2_blocks_override/{profileId}.json 存在即走人工分块，
        // Python 侧跳过 v8.4 部位分离；文件不存在回退自动分块，链路不变。
        var overridePath = Path.Combine(
            Path.GetDirectoryName(_store.DbPath) ?? ".", "drawingsv2_blocks_override",
            $"{profileId}.json");
        var json = await _py.LogicalBlocksAsync(pdfPath, ocrOn, ct, overridePath);

        JsonDocument doc;
        try { doc = JsonDocument.Parse(json); }
        catch (JsonException e) { throw new V2Exception($"logical-blocks 输出非法 JSON：{e.Message}"); }

        using (doc)
        {
            var root = doc.RootElement;
            var schema = root.TryGetProperty("schema", out var s) ? s.GetString() : "(缺失)";
            if (schema != "logical-blocks/1")
                throw new V2Exception($"意外契约版本：{schema}（期望 logical-blocks/1）");

            var algo = root.TryGetProperty("algo_version", out var av) ? av.GetString() ?? "" : "";
            int pageIndex = root.TryGetProperty("page_index", out var pi) && pi.TryGetInt32(out var piv)
                ? piv : 0;

            var rows = new List<V2Store.LogicalBlockRow>();
            if (root.TryGetProperty("blocks", out var blocks) &&
                blocks.ValueKind == JsonValueKind.Array)
            {
                foreach (var b in blocks.EnumerateArray())
                {
                    int bi = b.TryGetProperty("block_index", out var bie) && bie.TryGetInt32(out var biv)
                        ? biv : rows.Count;

                    var bboxPt = ReadDoubleArray(b, "bbox_pt");
                    var bboxNorm = ReadNorm(b);
                    string fpSeed = bboxPt is { Length: 4 }
                        ? string.Join(",", Array.ConvertAll(bboxPt, v => v.ToString("0.00")))
                        : "?";

                    var els = new List<V2Store.LogicalElementRow>();
                    if (b.TryGetProperty("elements", out var el) &&
                        el.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var e in el.EnumerateArray())
                        {
                            double? conf = null;
                            if (e.TryGetProperty("ocr_conf", out var cf) &&
                                cf.ValueKind == JsonValueKind.Number) conf = cf.GetDouble();
                            els.Add(new V2Store.LogicalElementRow
                            {
                                Kind = Str(e, "kind") ?? "",
                                Text = Str(e, "text"),
                                OcrConf = conf,
                                Norm = ReadNorm(e),
                                BboxJson = ArrJson(e, "bbox_norm"),
                                Participate = Int(e, "participate"),
                                Source = Str(e, "source") ?? ""
                            });
                        }
                    }

                    rows.Add(new V2Store.LogicalBlockRow
                    {
                        BlockIndex = bi,
                        // 稳定键：含 drawing_key + 位置 + 算法版本 + 几何 → 顶点漂移即换键，
                        // 避免历史结论绑在会漂移的序号上（既有 markKey 漂移教训）。
                        BlockKey = Sha256Hex24($"{drawingKey}|{pageIndex}|{bi}|{algo}|{fpSeed}"),
                        Name = Str(b, "name") ?? "",
                        NameFp = Str(b, "name_fp") ?? "",
                        Norm = bboxNorm,
                        BboxJson = ArrJson(b, "bbox_pt"),
                        ViewHint = Str(b, "view_hint") ?? "Unspecified",
                        AreaPct = Dbl(b, "area_pct"),
                        HasMarking = Int(b, "has_marking", 1),
                        EmptyReason = Str(b, "empty_reason"),
                        LowConf = Int(b, "low_conf"),
                        AlgoVersion = algo,
                        GeomFp = $"{fpSeed}#e{els.Count}",
                        NElements = Int(b, "n_elements", els.Count),
                        NParticipate = Int(b, "n_participate"),
                        NImage = Int(b, "n_image"),
                        Elements = els
                    });
                }
            }

            var (nB, nE, nHas) = _store.ReplaceLogicalBlocks(profileId, pageIndex, rows);
            sw.Stop();

            return new
            {
                profileId,
                drawingKey,
                pageIndex,
                algoVersion = algo,
                nBlocks = nB,
                nElements = nE,
                nHasMarking = nHas,
                nEmpty = nB - nHas,
                ocrOn,
                elapsedMs = Math.Round(sw.Elapsed.TotalMilliseconds, 1),
                sourceIntact = root.TryGetProperty("source_intact", out var si) && si.ValueKind == JsonValueKind.True
            };
        }
    }

    /// <summary>
    /// P2 · 读取某档案的逻辑图块（默认含元素）。
    /// <paramref name="hasMarkingOnly"/> 为 true 时只返回参与匹配的块 —— 即 P3 命中检索的候选集。
    /// </summary>
    public object LogicalBlocks(long profileId, bool hasMarkingOnly = false, bool withElements = true)
    {
        var rows = _store.ListLogicalBlocks(profileId, hasMarkingOnly, withElements);
        int nHas = 0;
        foreach (var r in rows) if (r.HasMarking) nHas++;
        return new
        {
            profileId,
            nBlocks = rows.Count,
            nHasMarking = nHas,
            nEmpty = rows.Count - nHas,
            hasMarkingOnly,
            blocks = rows
        };
    }

    /// <summary>
    /// P3 · 文本命中选块 + 四色标示（方案 §0.5.1 / §0.5.3 / §0.5.3.1，2026-09-27 用户最终口径）：
    /// 照片 OCR 文本 → 去图块打标内容里检索 → 命中绿 / 文本差异红（块有照片无·照片多出·相似·包含非全等）/ 图标·QR 任一方缺黄 / d 类灰。
    ///
    /// <para>【零回归】P3-b 已并 compare：结果经 <c>blockMatch</c> 附加字段下传，既有 <c>verdicts</c> 口径一字不改。</para>
    ///
    /// <para>【判定不依赖坐标】坐标（元素 Norm / 照片文本 Norm / PhotoNorm）只作为 P4 红框落点的输入带出，不参与四色结论。</para>
    /// </summary>
    public async Task<object> MatchBlocksAsync(long profileId, string photo, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(photo)) throw new V2Exception("缺少 photo");
        var photoPhys = ResolvePhoto(photo) ?? throw new V2Exception($"找不到照片：{photo}");

        var blocks = _store.ListLogicalBlocks(profileId, hasMarkingOnly: false, withElements: true);
        if (blocks.Count == 0)
            throw new V2Exception($"档案 {profileId} 还没有逻辑图块，请先 POST /api/drawingsv2/profiles/{profileId}/blocks 提取");

        var sw = System.Diagnostics.Stopwatch.StartNew();
        var obsJson = await _py.ObserveAsync(photoPhys, ct);
        JsonElement obsRoot;
        using (var doc = JsonDocument.Parse(obsJson)) obsRoot = doc.RootElement.Clone();

        var photoTexts = BlockTextMatcher.ExtractPhotoTexts(obsRoot);
        var photoCodes = BlockTextMatcher.ExtractPhotoCodes(obsRoot);   // §0.5.3.2 规则 3：QR/图标存在性
        var quality = BlockTextMatcher.ExtractQuality(obsRoot);
        var r = BlockTextMatcher.Match(blocks, photoTexts, quality, photoCodes);
        sw.Stop();

        return new
        {
            profileId,
            photo = photoPhys,
            photoSha256 = Sha256Of(photoPhys),
            nPhotoTexts = photoTexts.Count,
            nPhotoCodes = photoCodes.Count,
            quality = new { usable = quality.Usable, reasons = quality.Reasons, ocrTexts = quality.OcrTexts },
            match = new
            {
                r.Enabled,
                r.Reason,
                r.Degraded,
                r.DegradeReason,
                r.NBlocks,
                r.NCandidates,
                top = r.Top,
                candidates = r.Candidates,
                extra = r.Extra,
                photoTexts = r.PhotoTexts
            },
            elapsedMs = Math.Round(sw.Elapsed.TotalMilliseconds, 1)
        };
    }

    // ---- JS ON 读取小工具（避免缺字段抛异常，缺值一律取默认）----
    private static string? Str(JsonElement e, string name)
        => e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static double[]? ReadDoubleArray(JsonElement e, string name)
    {
        if (!e.TryGetProperty(name, out var v) || v.ValueKind != JsonValueKind.Array) return null;
        var n = v.GetArrayLength();
        var r = new double[n];
        for (int i = 0; i < n; i++) r[i] = v[i].GetDouble();
        return r;
    }

    /// <summary>读 <c>bbox_norm</c>（契约固定 [x,y,w,h] 归一化），缺失/不足 4 位返回 null。</summary>
    private static double[]? ReadNorm(JsonElement e)
    {
        var a = ReadDoubleArray(e, "bbox_norm");
        return a is { Length: >= 4 } ? new[] { a[0], a[1], a[2], a[3] } : null;
    }

    private static string? ArrJson(JsonElement e, string name)
    {
        if (!e.TryGetProperty(name, out var v) || v.ValueKind != JsonValueKind.Array) return null;
        return v.GetRawText();
    }

    private static int Int(JsonElement e, string name, int dflt = 0)
        => e.TryGetProperty(name, out var v) && v.TryGetInt32(out var i) ? i : dflt;

    private static double Dbl(JsonElement e, string name, double dflt = 0.0)
        => e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : dflt;

    private static string Sha256Hex24(string s)
    {
        var bytes = System.Text.Encoding.UTF8.GetBytes(s);
        var hash = SHA256.HashData(bytes);
        return Convert.ToHexString(hash)[..24].ToLowerInvariant();
    }

    /// <summary>比对记录详情（含完整 verdicts）。</summary>
    public object? CompareRecord(long id) => _store.GetCompareRecord(id);

    /// <summary>
    /// 取某条比对记录的「已标示照片」（四色框叠加在原始照片上）。
    /// 首次访问时调用 Python 渲染并落盘缓存到 data/drawingsv2_records/{id}/，之后直接回读缓存（缩略图/原图）。
    /// mode=thumb 输出缩略图（最长边 360px），mode=full 输出原尺寸。
    /// 渲染规则与 H5 drawPhotoAnnotations 新引擎一致（单一事实源：Scripts/render_marked.py）。
    /// </summary>
    public async Task<(byte[] Bytes, string ContentType)?> GetRecordMarkedPhoto(long id, string mode, CancellationToken ct = default)
    {
        var media = _store.GetRecordMedia(id);
        if (media is null) return null;
        var (photoPath, responseJson) = media.Value;
        if (string.IsNullOrWhiteSpace(photoPath) || !File.Exists(photoPath)) return null;

        var root = Path.Combine(Path.GetDirectoryName(_store.DbPath) ?? ".", "drawingsv2_records");
        var dir = Path.Combine(root, id.ToString());
        Directory.CreateDirectory(dir);

        var isThumb = !string.Equals(mode, "full", StringComparison.OrdinalIgnoreCase);
        var outFile = Path.Combine(dir, isThumb ? "thumb.jpg" : "full.jpg");
        var responseFile = Path.Combine(dir, "response.json");

        // 缓存失效判定：输出不存在，或响应 JSON 较缓存有变化（规则/结论更新 → 重渲染）。
        var stale = !File.Exists(outFile) || !File.Exists(responseFile) ||
                    !string.Equals(await File.ReadAllTextAsync(responseFile, ct), responseJson ?? "", StringComparison.Ordinal);
        if (stale)
        {
            await File.WriteAllTextAsync(responseFile, responseJson ?? "{}", ct);
            var maxSide = isThumb ? 360 : 0;
            await _py.RenderMarkedAsync(photoPath, responseFile, outFile, maxSide, ct);
        }

        var bytes = await File.ReadAllBytesAsync(outFile, ct);
        return (bytes, "image/jpeg");
    }

    /// <summary>G1 人工复核：取档案复核页数据（基本信息 + 全部 marks 含 is_active/confirmed）。</summary>
    public object? ProfileForReview(long id) => _store.GetProfileForReview(id);

    /// <summary>
    /// M7（库管理）档案原图：返回 (drawingKey, pdfPath) 供端点 Results.File 下载。
    /// 替代 V1 detail 的 virtualPath 下载链路（选图纸入口切 V2 后 H5 详情页不再依赖 V1）。
    /// </summary>
    public (string DrawingKey, string PdfPath)? ProfilePdf(long id)
    {
        var loc = _store.GetProfileLocation(id);
        if (loc is null) return null;
        var key = loc.Value.drawingKey;
        var pdf = loc.Value.pdfPath;
        if (string.IsNullOrWhiteSpace(pdf) || !File.Exists(pdf)) return null;
        return (key, pdf);
    }


    /// <summary>G1 人工复核：保存复核结果（增量更新 marks 的 is_active/confirmed，档案置 reviewed）。</summary>
    public void ReviewProfile(long id, string? reviewedBy, string? note,
                              IReadOnlyList<(string MarkKey, bool IsActive, bool Confirmed)> items)
        => _store.SaveReview(id, reviewedBy, note, items);

    /// <summary>G2 会话合并：取一次多部位检验会话的聚合结果。</summary>
    public object? Session(string sessionId) => _store.GetSession(sessionId);

    /// <summary>② 保存判定级人工确认（实物有标 / 实物缺标 / 拍错部位）。</summary>
    public int SaveVerdictReviews(long recordId,
        IEnumerable<(string MarkKey, string HumanState, string? Note)> items, string? by)
        => _store.SaveVerdictReviews(recordId, items, by);

    /// <summary>【B 项·capture】为 QR mark 录入/清除预期解码内容（供 VerifyQr 做内容身份比对）。</summary>
    public bool SetMarkExpectedText(long profileId, long markId, string? text)
        => _store.SetMarkExpectedText(profileId, markId, text);

    public long AddMark(long profileId, string markType, string view, string? text, bool required, string? conditionText, double? nx, double? ny, double? nw, double? nh)
        => _store.AddMark(profileId, markType, view, text, required, conditionText, nx, ny, nw, nh);
    public bool UpdateMark(long profileId, long markId, string markType, string view, string? text, bool required, string? conditionText, double? nx, double? ny, double? nw, double? nh)
        => _store.UpdateMark(profileId, markId, markType, view, text, required, conditionText, nx, ny, nw, nh);
    public bool DeleteMark(long profileId, long markId)
        => _store.DeleteMark(profileId, markId);



    /// <summary>② 取某条记录的人工确认结果（前端回显/审计）。</summary>
    public object VerdictReviews(long recordId)
        => new
        {
            recordId,
            items = _store.ListVerdictReviews(recordId)
                .OrderBy(kv => kv.Key, StringComparer.Ordinal)
                .Select(kv => new
                {
                    markKey = kv.Key,
                    systemState = kv.Value.SystemState,
                    humanState = kv.Value.HumanState,
                    by = kv.Value.By,
                    note = kv.Value.Note,
                    at = kv.Value.At
                }).ToList()
        };

    /// <summary>取某图纸 marks 的 View 分布（供前端动态生成视图选择器选项）。</summary>
    public List<object>? ViewDistribution(string drawing) => _store.ViewDistribution(drawing);

    /// <summary>
    /// 渲染图纸页面为 PNG 字节（供 H5 可视化图纸回显）。
    /// nameOrPath 支持：物理绝对路径 / 虚拟路径（drawings/xxx.pdf）/ 纯文件名。
    /// </summary>
    public async Task<byte[]?> RenderDrawingPageAsync(string nameOrPath, int pageIndex, CancellationToken ct)
    {
        var phys = ResolvePdf(nameOrPath);
        if (phys is null) return null;
        return await _py.RenderDrawingPageAsync(phys, pageIndex, ct);
    }

    /// <summary>S2（2026-09-14）：组子项坐标塌陷修复。同组内 ≥2 个子项 norm_bbox 完全重合时，
    /// 在其共有 bbox 内垂直均分，给每个子项分配可区分坐标。marks 为每次比对新加载，直接修改无副作用。</summary>
    /// <summary>把长度为 size 的框起点限制在 [0, 1-size] 内，避免平移后越界。</summary>
    private static double Clamp01(double v, double size)
        => v < 0 ? 0 : (v + size > 1 ? Math.Max(0, 1 - size) : v);

    private static void DistributeCollapsedGroupChildren(IReadOnlyList<DrawingMark> marks)
    {
        foreach (var g in marks.Where(m => m.Type == MarkType.Group))
        {
            var kids = (g.Children ?? new List<DrawingMark>())
                .Where(k => k.NormBbox is { Length: 4 } && k.NormBbox[2] > 0 && k.NormBbox[3] > 0)
                .ToList();
            if (kids.Count < 2) continue;
            // 仅对完全重合（相同 norm_bbox）的子集做垂直均分
            foreach (var grp in kids
                .GroupBy(k => string.Join(",", k.NormBbox!.Select(x => x.ToString("F4"))))
                .Where(gr => gr.Count() > 1))
            {
                var list = grp.ToList();
                var bbox = list[0].NormBbox!;
                double x = bbox[0], y0 = bbox[1];
                double w = bbox[2], h = bbox[3];
                double slot = h / list.Count;
                for (int i = 0; i < list.Count; i++)
                    list[i].NormBbox = new[] { x, y0 + slot * i, w, slot };
            }
        }
    }

    /// <summary>
    /// M2（#35）人工三态 → 与系统判定是否「观感一致」。
    /// 两者枚举空间不同（HumanPresent/Missing/WrongPart vs 八态），直接字符串相等恒为 false。
    /// 映射沿用既有会话聚合语义（V2Store.SessionAgg.Rollup）：HumanPresent→Matched、
    /// HumanMissing→Missing、HumanWrongPart→「没拍到或不可比」，此处不引入新规则。
    /// </summary>
    private static bool MemoryAgrees(string human, string sys) => V2Store.MemoryAgrees(human, sys);


    private async Task<object> CompareCoreAsync(string drawing, string photoPhys, string? photoName,
                                                bool withObserve, CancellationToken ct,
                                                string? photoSha = null, string? view = null,
                                                string? sessionId = null)
    {
        // 1) 定位档案（数字 id 直取；否则按 key，找不到就先剖析入库）
        List<DrawingMark> marks;
        long profileId;
        string drawingKey;
        if (long.TryParse(drawing, out var pid))
        {
            var loaded = _store.LoadMarks(pid) ?? throw new V2Exception($"档案不存在：{pid}");
            marks = loaded;
            profileId = pid;
            drawingKey = _store.ProfileKey(pid) ?? $"profile#{pid}";
        }
        else
        {
            var phys0 = ResolvePdf(drawing);
            var key = phys0 is not null ? Path.GetFileNameWithoutExtension(phys0) : drawing;
            var found = _store.FindLatestProfile(key);
            if (found is null)
            {
                await AnalyzeAsync(drawing, ct);   // 剖析并落库
                found = _store.FindLatestProfile(key) ?? throw new V2Exception($"剖析后仍未找到档案：{key}");
            }
            marks = _store.LoadMarks(found.Value.Id) ?? throw new V2Exception($"档案 mark 读取失败：{found.Value.Id}");
            profileId = found.Value.Id;
            drawingKey = found.Value.DrawingKey;
        }

        // 生产闸门：未人工确认的图纸对象不得用于比对。
        var profileStatus = _store.GetProfileStatus(profileId);
        if (_requireReviewedProfiles && !string.Equals(profileStatus, "reviewed", StringComparison.OrdinalIgnoreCase))
            throw new V2Exception("该图纸对象尚未人工确认，请先完成图纸复核再进行生产比对。");

        if (!string.IsNullOrWhiteSpace(view)
            && (!Enum.TryParse<MarkView>(view, true, out var requestedView)
                || requestedView == MarkView.Unspecified))
            throw new V2Exception($"无效的拍摄部位：{view}");

        // M1（#35）照片指纹缓存：完全相同的输入（档案版本 + 判定版本 + 照片内容 + 部位）
        // 直接复用历史结论，不再重复消耗 ~6.8s 的 Python 感知链路；同时保证「同一输入同一结论」。
        // 缓存查询在 Python 调用之前，命中即返回；任何异常或不一致都回落到正常计算。
        var photoShaValue = photoSha ?? Sha256Of(photoPhys);
        var pdfShaFull = _store.ProfilePdfSha(profileId);
        var pdfSha16 = pdfShaFull.Length >= 16 ? pdfShaFull.Substring(0, 16) : pdfShaFull;
        if (_photoCache && !string.IsNullOrWhiteSpace(photoShaValue))
        {
            try
            {
                var hit = _store.FindCachedCompare(profileId, photoShaValue, pdfSha16,
                                                   V2Store.DecisionVersion, view);
                if (hit is not null)
                {
                    var countsCache = new Dictionary<string, int>();
                    if (JsonNode.Parse(hit.CountsJson) is JsonObject cj)
                        foreach (var kv in cj)
                            if (kv.Value is JsonValue jv && jv.TryGetValue<int>(out var ci))
                                countsCache[kv.Key] = ci;
                    var qualityCache = new List<string>();
                    if (JsonNode.Parse(hit.QualityJson) is JsonArray qa)
                        foreach (var q in qa)
                        {
                            var qs = q is JsonValue jq && jq.TryGetValue<string>(out var s) ? s : "";
                            if (!string.IsNullOrEmpty(qs)) qualityCache.Add(qs);
                        }
                    JsonObject psjCache;
                    try
                    {
                        psjCache = JsonNode.Parse(string.IsNullOrWhiteSpace(hit.ParamsSnapshotJson)
                            ? "{}" : hit.ParamsSnapshotJson) as JsonObject ?? new JsonObject();
                    }
                    catch { psjCache = new JsonObject(); }
                    psjCache["fromCache"] = true;
                    psjCache["cacheHitRecordId"] = hit.Id;

                    var hitSid = string.IsNullOrWhiteSpace(sessionId) ? Guid.NewGuid().ToString("N") : sessionId;
                    var newId = _store.SaveCompare(
                        profileId, drawingKey, photoPhys, photoShaValue, photoName,
                        hit.Usable, hit.CodeDetector, hit.CodeDegraded, 0.0,
                        countsCache, qualityCache, hit.VerdictsJson,
                        selectedView: view, sessionId: hitSid,
                        marksSnapshotJson: hit.MarksSnapshotJson,
                        paramsSnapshotJson: psjCache.ToJsonString(),
                        responseJson: hit.ResponseJson);

                    if (JsonNode.Parse(hit.ResponseJson) is JsonObject joCache)
                    {
                        joCache["recordId"] = newId;
                        joCache["fromCache"] = true;
                        joCache["cacheHitRecordId"] = hit.Id;
                        joCache["verifyMs"] = 0;

                        // M2 兼容（#35）：人工记忆是随时间累积的，不能被整份缓存冻结
                        // ——否则刚确认过的对象，下次比对仍查不到记忆。此处用当前记忆覆盖缓存里的快照。
                        try
                        {
                            var memItems = new List<object>();
                            var verKey2 = V2Store.DecisionVersion + ":" + pdfSha16;
                            var mem2 = _store.ListMarkMemory(profileId, verKey2);
                            if (JsonNode.Parse(hit.VerdictsJson) is JsonArray varr)
                                foreach (var vv in varr)
                                {
                                    var mk = vv?["markKey"]?.GetValue<string>() ?? "";
                                    var st = vv?["state"]?.GetValue<string>() ?? "";
                                    if (mk.Length == 0 || mk.StartsWith("extra#", StringComparison.Ordinal)) continue;
                                    if (!mem2.TryGetValue(mk, out var mm2)) continue;
                                    memItems.Add(new
                                    {
                                        markKey = mk,
                                        systemState = st,
                                        humanState = mm2.Human,
                                        agreed = MemoryAgrees(mm2.Human, st),
                                        by = mm2.By,
                                        at = mm2.At
                                    });
                                }
                            joCache["memory"] = JsonSerializer.SerializeToNode(memItems);
                        }
                        catch { }


                    }
                }
            }
            catch
            {
                // 缓存复用失败不得影响正常比对：静默回落
            }
        }

        // 1.5) 视图级配准（方案 B）：解析 view → MarkView?，按视图过滤 + 用视图并集 bbox 重算 NormBbox
        // 重算后的 NormBbox 用于：构造 regions.json（Python verify 按视图级位置裁剪照片）
        // 与 MarkVerifier.Verify 的位置比较（C# 端在 Verify 内部再次构造同样的 viewNormMap）。
        // selectedView 为 null/Unspecified → viewNormMap 为 null，走原页面级流程（向后兼容）。
        MarkView? selectedView = null;
        MarkView? inferredView = null;   // 自动视图识别（Stage 3 自动找图块）
        var viewCandidates = new List<object>();   // P2 BlockMatcher：Top-N 候选视图（供 H5 多候选选择，B3）
        Dictionary<string, double[]>? viewNormMap = null;
        var filteredMarks = marks;

        // 【自动视图识别】用户没选 view 时，先跑盲检，用 OCR 文本 + QR 解码反向匹配图纸 marks 的 View
        // 推断出唯一 view → 自动走方案B视图级配准；推断不出/冲突 → 降级页面级（向后兼容）
        JsonDocument? obsDoc = null;
        object? blockMatchDto = null;    // ★ 2026-09-28：逻辑图块文本命中，计算上移至 view 门禁前（型号驱动、不依赖部位）
        bool geometryApplied = false;     // 照片是否做了透视矫正（决定 H5 标注坐标是否可直接画在原图上）
        string geometryMethod = "";
        if (withObserve)
        {
            var obsJson = await _py.ObserveAsync(photoPhys, ct);
            obsDoc = JsonDocument.Parse(obsJson);
            if (obsDoc.RootElement.TryGetProperty("geometry", out var gmEl)
                && gmEl.ValueKind == JsonValueKind.Object)
            {
                if (gmEl.TryGetProperty("applied", out var ap)) geometryApplied = ap.ValueKind == JsonValueKind.True;
                if (gmEl.TryGetProperty("method", out var me) && me.ValueKind == JsonValueKind.String)
                    geometryMethod = me.GetString() ?? "";
            }

            if (string.IsNullOrWhiteSpace(view))
            {
                // 【P2 BlockMatcher】特征级找图块：多特征加权打分取 Top-N；
                // Chosen 明显领先才自动选（保持旧"唯一匹配才选"语义 + 覆盖更多旧 null 情形），
                // 否则 Chosen=null，中止判定并要求 H5 让用户确认部位。
                var bm = BlockMatcher.InferView(marks, obsDoc.RootElement);
                if (bm.Chosen is { } cv && cv != MarkView.Unspecified)
                {
                    inferredView = cv;
                    view = cv.ToString();   // 统一走下面的视图级配准流程
                }
                viewCandidates = bm.Candidates
                    .Select(c => (object)new { view = c.View.ToString(), score = c.Score, reason = c.Reason })
                    .ToList();
            }

            // ★ 2026-09-28：逻辑图块文本命中计算上移（型号驱动、不依赖部位 view）。
            // view 缺失时仍返回 blockMatch，使 H5「补选部位」交互彻底移除。失败静默回落，不影响主判定。
            try
            {
                var lb = _store.ListLogicalBlocks(profileId, hasMarkingOnly: false, withElements: true);
                // ★ 2026-09-28 方案A（用户拍板）：未建档图纸（blocks=0）首次比对时自动补提取（实测 30~60s/图），
                //   失败静默回落 → 由下方 blocksMissing 结构给 H5「一键建档并比对」入口（方案C 兜底）。
                if (lb.Count == 0)
                {
                    try { await ExtractLogicalBlocksAsync(profileId, ocrOn: true, ct); }
                    catch { /* 提取失败不阻断主判定 */ }
                    lb = _store.ListLogicalBlocks(profileId, hasMarkingOnly: false, withElements: true);
                }
                if (lb.Count > 0)
                {
                    var pTexts = BlockTextMatcher.ExtractPhotoTexts(obsDoc.RootElement);
                    var pCodes = BlockTextMatcher.ExtractPhotoCodes(obsDoc.RootElement);
                    var pQual = BlockTextMatcher.ExtractQuality(obsDoc.RootElement);
                    var mr = BlockTextMatcher.Match(lb, pTexts, pQual, pCodes);
                    blockMatchDto = new
                    {
                        mr.Enabled,
                        mr.Reason,
                        mr.Degraded,
                        mr.DegradeReason,
                        mr.NBlocks,
                        mr.NCandidates,
                        top = mr.Top,
                        candidates = mr.Candidates,
                        extra = mr.Extra,
                        photoTexts = mr.PhotoTexts,
                        nPhotoCodes = mr.NPhotoCodes
                    };
                }
                else
                {
                    // ★ 2026-09-28 方案C：自动提取仍未产出块 → 明确告知未建档，H5 给「一键建档并比对」入口
                    blockMatchDto = new
                    {
                        Enabled = false,
                        Reason = "该图纸尚未建立逻辑图块档案（自动提取未成功），请点击「一键建档并比对」重试",
                        BlocksMissing = true,
                        NBlocks = 0,
                        NCandidates = 0,
                        Top = (object?)null,
                        Candidates = Array.Empty<object>(),
                        Extra = Array.Empty<object>(),
                        PhotoTexts = Array.Empty<string>(),
                        NPhotoCodes = 0
                    };
                }
            }
            catch { /* 并入失败不得影响主判定 */ }
        }

        if (string.IsNullOrWhiteSpace(view))
        {
            return new
            {
                recordId = (long?)null,
                profileId,
                drawingKey,
                profileStatus,
                requiresViewSelection = true,
                result = "NeedsReview",
                reason = withObserve
                    ? "照片证据不足，未产出比对结论。请拍清晰、正对部件的照片后重试。"   // ★ 2026-09-28 去部位化文案（原文案「请选择拍摄部位」已过时）
                    : "未启用全图观测，无法进行对象判定。",
                viewCandidates,
                photo = photoPhys,
                // ★ 2026-09-28：补 photoUrl，使 H5 照片回显可加载（此前只返绝对路径 photoPhys，浏览器无法加载 → 无照片显示）
                photoUrl = $"/drawingsv2-photos/{Path.GetRelativePath(PhotoDir, photoPhys).Replace('\\', '/')}",
                photoSha256 = photoSha ?? Sha256Of(photoPhys),
                geometryApplied,
                geometryMethod,
                blockMatch = blockMatchDto,   // ★ 2026-09-28：view 缺失时仍返回型号驱动的逻辑图块结论
                // ★ 2026-09-28：补齐图纸图像源，使 H5 图纸画布在型号驱动模式下可高亮命中块整体区域（drawingBbox 缺省走 blockMatch.top.blockNorm）
                drawingPdfPath = _store.ProfilePdfPath(profileId) ?? ResolvePdf(drawingKey),
                drawingPageIndex = 0,
                verdicts = Array.Empty<object>()
            };
        }

        // P0：文本锚点配准变换（null = 未校正）。仅在视图级配准分支内计算。
        MarkVerifier.AnchorTransform? anchor = null;
        bool affineApplied = false;

        if (!string.IsNullOrWhiteSpace(view) &&
            Enum.TryParse<MarkView>(view, true, out var mv) && mv != MarkView.Unspecified)
        {
            selectedView = mv;
            filteredMarks = MarkVerifier.FilterByView(marks, mv);
            // S2：组子项坐标塌陷修复——同组多个子项 norm_bbox 完全重合时，在其共有 bbox 内
            // 垂直均分，使每个子项获得可区分的视图坐标（照片标示/位置比对不再重叠）。
            DistributeCollapsedGroupChildren(filteredMarks);
            // M6 修正·视图词范围：用 ViewBboxOf 替代 marks 的 UnionBbox，
            // 以视图标签文字为锚点排除散乱 marks，使 viewBbox 更贴合照片实际产品面
            var viewBbox = MarkVerifier.ViewBboxOf(filteredMarks);
            // viewBbox 为 null（视图无声明）→ viewNormMap 置为空字典，
            // regions 构造为空，MarkVerifier.Verify 走「该图纸无 X 视图声明」分支返回空 verdict
            viewNormMap = new Dictionary<string, double[]>();
            if (viewBbox is { Length: >= 4 } && viewBbox[2] > 0 && viewBbox[3] > 0)
            {
                foreach (var m in MarkVerifier.EnumerateNonGroup(filteredMarks))
                    if (m.NormBbox is { Length: >= 4 })
                        viewNormMap[m.Id] = MarkVerifier.RebaseToView(m.NormBbox, viewBbox);
            }

            // 【Stage 4 真正几何配准】基于对象的仿射配准：用盲检文本/QR 位置与 marks 位置
            // 建立点对，计算仿射变换补偿旋转与缩放。配准后的 viewNormMap 同时用于
            // 构造 regions（裁剪位置）和 MarkVerifier.Verify（判定位置），保证一致。
            if (viewNormMap.Count > 0 && obsDoc is not null)
            {
                viewNormMap = MarkVerifier.ApplyAffineRegistration(filteredMarks, viewNormMap, obsDoc.RootElement, out affineApplied);
            }

            // 【P0 文本锚点配准（2026-09-15）】仿射配准在平面图纸（no-quad）下生产 0/60 生效，
            // 定点框实为图纸坐标直映照片 → 取景差异变错位。此处用「盲检 OCR 命中的 mark」作锚点
            // 估计平移/尺度并搬正 viewNormMap：regions（裁剪）与 Verify（判定）共用同一份坐标，
            // 不会出现两处变换不一致。0 锚点时 EstimateAnchorTransform 返回 null → 完全不校正。
            anchor = MarkVerifier.EstimateAnchorTransform(
                FlattenForVerify(filteredMarks).ToList(), viewNormMap, obsDoc?.RootElement);
            if (anchor is { Applied: true })
            {
                var corrected = new Dictionary<string, double[]>();
                foreach (var kv in viewNormMap) corrected[kv.Key] = anchor.Apply(kv.Value);
                viewNormMap = corrected;
            }
        }

        // B（#37）：配准可信 = 仿射生效 或 锚点生效 或 指定了视图（照片即视图坐标系）。
        // 仅此时部位覆盖判据（subject 求交）才可靠。
        bool registrationTrusted = affineApplied || anchor?.Applied == true
                                    || (selectedView is { } sv && sv != MarkView.Unspecified);

        // 2) 定点区域清单（Group 展开为子项；无坐标的 mark 无法定点，跳过并如实说明）
        // 视图级配准时：用视图级 NormBbox（按 mark.Id 查 viewNormMap），照片即视图坐标系
        var regions = new List<object>();
        var skipped = new List<string>();
        foreach (var m in FlattenForVerify(filteredMarks))
        {
            // 非必标 mark：决策层已对其返回 NotApplicable（MarkVerifier.cs:720），
            // 不依赖定点观测结果，故直接跳过 region 构建，避免无谓的昂贵解码（QR/图标多尺度）。
            if (!m.Required) continue;

            var nb = viewNormMap is not null && viewNormMap.TryGetValue(m.Id, out var vb)
                     ? vb : m.NormBbox;
            if (nb is { Length: >= 4 })
                regions.Add(new
                {
                    id = m.Id,
                    kind = m.Type switch
                    {
                        MarkType.Qr => "qr",
                        MarkType.Icon => "icon",
                        _ => "text"
                    },
                    norm_bbox = nb,
                    label = m.Text
                });
            else
                skipped.Add(m.Id);
        }

        var regionsFile = Path.Combine(Path.GetTempPath(), $"v2_regions_{Guid.NewGuid():N}.json");
        try
        {
            await File.WriteAllTextAsync(regionsFile,
                JsonSerializer.Serialize(regions), ct);

            // 3) 定点感知（盲检已在上面提前跑完，复用 obsDoc）
            using var verDoc = JsonDocument.Parse(await _py.VerifyAsync(photoPhys, regionsFile, ct));

            // 4) 八态判定 —— 下传 selectedView + 已配准的 viewNormMap（含仿射变换），
            //    MarkVerifier 直接复用，保证 regions 裁剪位置与判定位置一致
            // 【文档 §5 / §8】照片有效部位区域（产品主体）：只在主体内建立一对一关系
            double[]? photoSubject = null;
            if (obsDoc is not null
                && obsDoc.RootElement.TryGetProperty("subject", out var sj)
                && sj.ValueKind == JsonValueKind.Object
                && sj.TryGetProperty("norm_bbox", out var snb)
                && snb.ValueKind == JsonValueKind.Array && snb.GetArrayLength() >= 4)
            {
                photoSubject = new[]
                {
                    snb[0].GetDouble(), snb[1].GetDouble(), snb[2].GetDouble(), snb[3].GetDouble()
                };
            }
            var verdict = MarkVerifier.Verify(marks, verDoc.RootElement, obsDoc?.RootElement, selectedView, viewNormMap, anchor, photoSubject, registrationTrusted: registrationTrusted, partCoverageGate: _partCoverageGate);

            // 【坐标对齐 #27】首次定点大面积落空时，若多项「全图精确命中」的偏移高度一致，
            // 说明是整体平移错位而非随机误差 —— 按一致偏移重裁一次定点区。
            // 重裁命中才升级为绿，未命中保持原判，故不会产生假绿（只可能变好）。
            object? alignInfo = null;
            var al = MarkVerifier.EstimateConsistentOffset(verdict);
            if (al is not null && viewNormMap is { Count: > 0 })
            {
                var regions2 = new List<object>();
                foreach (var m in FlattenForVerify(filteredMarks))
                {
                    if (!al.MarkIds.Contains(m.Id)) continue;
                    if (!viewNormMap.TryGetValue(m.Id, out var nb) || nb is not { Length: >= 4 }) continue;
                    regions2.Add(new
                    {
                        id = m.Id,
                        kind = m.Type switch
                        {
                            MarkType.Qr => "qr",
                            MarkType.Icon => "icon",
                            _ => "text"
                        },
                        norm_bbox = new[] { Clamp01(nb[0] + al.Dx, nb[2]), Clamp01(nb[1] + al.Dy, nb[3]), nb[2], nb[3] },
                        label = m.Text
                    });
                }
                int upgraded = 0;
                if (regions2.Count > 0)
                {
                    var rf2 = Path.Combine(Path.GetTempPath(), $"v2_regions_{Guid.NewGuid():N}.json");
                    try
                    {
                        await File.WriteAllTextAsync(rf2, JsonSerializer.Serialize(regions2), ct);
                        using var verDoc2 = JsonDocument.Parse(await _py.VerifyAsync(photoPhys, rf2, ct));
                        upgraded = MarkVerifier.ApplyRealignedRegions(
                            marks, verdict, verDoc2.RootElement, obsDoc?.RootElement, al);
                    }
                    finally
                    {
                        try { File.Delete(rf2); } catch { }
                    }
                }
                alignInfo = new
                {
                    dx = Math.Round(al.Dx, 4),
                    dy = Math.Round(al.Dy, 4),
                    support = al.Support,
                    recropped = regions2.Count,
                    upgraded
                };
            }

            // M2（#35）对象级确认记忆：本次涉及的对象，历史上人工确认过什么。
            // 只做「回显与对齐提示」——不改写 verdicts_json、不新增 state、不参与判定。
            // M3（#36）在此一并取稳定性计数，供严格 Gate 判定与前端展示。
            var memoryItems = new List<object>();
            {
                try
                {
                    var verKey = V2Store.DecisionVersion + ":" + pdfSha16;
                    var mem = _objectMemory ? _store.ListMarkMemory(profileId, verKey)
                                            : new Dictionary<string, (string Human, string? By, string At)>(StringComparer.Ordinal);
                    foreach (var v in verdict.Verdicts)
                    {
                        if (v.MarkKey.StartsWith("extra#", StringComparison.Ordinal)) continue;  // 临时对象无持久身份
                        if (!_objectMemory) continue;
                        if (!mem.TryGetValue(v.MarkKey, out var mm)) continue;
                        memoryItems.Add(new
                        {
                            markKey = v.MarkKey,
                            systemState = v.State,
                            humanState = mm.Human,
                            agreed = MemoryAgrees(mm.Human, v.State),
                            by = mm.By,
                            at = mm.At
                        });
                    }
                }
                catch { }
            }

            // 5) 落库留痕（历史回溯 / 复查）—— counts_json 同条记录存 selected_view 键
            var verdictDtos = verdict.Verdicts.Select(v =>
            {
                return new
                {
                    markKey = v.MarkKey,
                    type = v.Type,
                    view = v.View,
                    text = v.Text,
                    state = v.State,
                    offset = v.Offset,
                    observedData = v.ObservedData,
                    evidence = v.Evidence,
                    photoBbox = v.PhotoBbox,
                    drawingBbox = v.DrawingBbox,
                    expectedBbox = v.ExpectedBbox,
                    // M3 置信度：Gate 已删（2026-09-27），此处仅作诊断输出，不参与任何判定。
                    confidence = v.Confidence,
                    blindFallback = v.BlindFallbackHit
                };
            }).ToList();

            // G2：会话合并——未指定 sessionId 时自动新建，保证首拍即建会话、响应带回 sessionId
            if (string.IsNullOrWhiteSpace(sessionId)) sessionId = Guid.NewGuid().ToString("N");

            // G3：比对可追溯快照（实际比对对象集 + 比对参数），落库供 SQL 级还原
            var marksSnapshotJson = JsonSerializer.Serialize(
                FlattenForVerify(filteredMarks).Select(m => new
                {
                    markKey = m.Id,
                    type = m.Type.ToString(),
                    view = m.View.ToString(),
                    text = m.Text,
                    required = m.Required,
                    normBbox = m.NormBbox,
                    confidence = m.Confidence
                }).ToList());
            var paramsSnapshotJson = JsonSerializer.Serialize(new
            {
                selectedView = selectedView?.ToString(),
                inferredView = inferredView?.ToString(),
                viewCandidates = viewCandidates,
                geometryMethod,
                geometryApplied,
                // M3：result 级放行判据落库，供缓存命中路径重算（缺少它们就无法在缓存路径判放行）
                photoUsable = verdict.PhotoUsable,
                geometryTrusted = verdict.GeometryTrusted,
                alignment = alignInfo,
                verifierVersion = V2Store.DecisionVersion,
                codeDetector = verdict.CodeDetector,
                anchor = anchor is null ? null : new
                {
                    applied = anchor.Applied,
                    anchors = anchor.AnchorCount,
                    dx = Math.Round(anchor.Dx, 4),
                    dy = Math.Round(anchor.Dy, 4),
                    scale = Math.Round(anchor.Scale, 4),
                    residual = Math.Round(anchor.Residual, 4),
                    needsReview = anchor.Suppressed
                },
                photoName = photoName,
                photoSha256 = photoSha ?? Sha256Of(photoPhys)
            });

            var recordId = _store.SaveCompare(
                profileId, drawingKey, photoPhys, photoSha ?? Sha256Of(photoPhys), photoName,
                verdict.PhotoUsable, verdict.CodeDetector, verdict.CodeDegraded, verdict.VerifyMs,
                verdict.Counts, verdict.QualityReasons, JsonSerializer.Serialize(verdictDtos),
                selectedView: selectedView?.ToString(),
                sessionId: sessionId,
                marksSnapshotJson: marksSnapshotJson,
                paramsSnapshotJson: paramsSnapshotJson);

            // R2（#34）：照片盲检码全集（照片侧编号 P? 的稳定来源）。
            // 只作展示与编号依据，**不参与任何判定**（QR 不解码原则：位置才是判据）。
            var blindCodesDto = new List<object>();
            if (obsDoc is not null
                && obsDoc.RootElement.TryGetProperty("codes", out var codesEl)
                && codesEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var cEl in codesEl.EnumerateArray())
                {
                    if (cEl.ValueKind != JsonValueKind.Object) continue;
                    string s(string k) => cEl.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String
                        ? v.GetString() ?? "" : "";
                    blindCodesDto.Add(new
                    {
                        id = s("id"),
                        type = s("type"),
                        data = cEl.TryGetProperty("data", out var dv) && dv.ValueKind == JsonValueKind.String
                            ? dv.GetString() : null,
                        normBbox = cEl.TryGetProperty("norm_bbox", out var nv) && nv.ValueKind == JsonValueKind.Array
                            ? nv.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.Number)
                                 .Select(x => Math.Round(x.GetDouble(), 6)).ToArray()
                            : null,
                        detector = s("detector")
                    });
                }
            }

            // 照片访问 URL：PhotoDir 映射到 /drawingsv2-photos，取 photoPhys 相对 PhotoDir 的路径
            var photoRel = Path.GetRelativePath(PhotoDir, photoPhys).Replace('\\', '/');
            var photoUrl = $"/drawingsv2-photos/{photoRel}";

            // P3-b（2026-09-27 → 2026-09-28 解耦）：逻辑图块文本命中结果并入 compare 响应（独立字段 blockMatch，绝不改写 verdicts）。
            // 计算已在 view 门禁前完成（BuildBlockMatch 内联于 withObserve 分支，复用已加载的 obsDoc，避免二次 Observe），此处直接引用。

            object response = new
            {
                recordId = -1L,   // 占位：SaveCompare 取得真实 id 后回填进 response_json
                // M1 缓存的响应结构版本（#36）：结构变更 +1 即可让旧缓存自动失效
                schemaVersion = V2Store.ResponseSchemaVersion,
                // 代码指纹（#36）：重新编译即变化，让旧缓存自动失效，避免掩盖改动与修复
                code = V2Store.CodeFingerprint,
                profileId,
                drawingKey,
                sessionId = sessionId ?? "",
                profileStatus,
                requiresViewSelection = false,
                reviewHint = "",
                photo = photoPhys,
                photoUrl,
                photoSha256 = photoSha ?? Sha256Of(photoPhys),
                photoUsable = verdict.PhotoUsable,
                qualityReasons = verdict.QualityReasons,
                codeDetector = verdict.CodeDetector,
                codeDegraded = verdict.CodeDegraded,
                verifyMs = verdict.VerifyMs,
                counts = verdict.Counts,
                selectedView = selectedView?.ToString() ?? "",
                inferredView = inferredView?.ToString() ?? "",
                viewCandidates = viewCandidates,
                geometryApplied,
                geometryMethod,
                alignment = alignInfo,
                anchor = anchor is null ? null : new
                {
                    applied = anchor.Applied,
                    anchors = anchor.AnchorCount,
                    dx = Math.Round(anchor.Dx, 4),
                    dy = Math.Round(anchor.Dy, 4),
                    scale = Math.Round(anchor.Scale, 4),
                    residual = Math.Round(anchor.Residual, 4),
                    needsReview = anchor.Suppressed
                },
                skippedNoBbox = skipped,
                verdicts = verdictDtos,
                // P3-b（2026-09-27）：逻辑图块文本命中（四色）结果，独立字段，前端按需渲染照片四色框；不改 verdicts。
                blockMatch = blockMatchDto,
                // R2（#34）：照片盲检码全集（含 normBbox/data/detector），供前端稳定编号 P1…Pn
                blindCodes = blindCodesDto,
                // M2（#35）：对象级确认记忆（上次人工结论），供前端显示「上次确认为 X」
                memory = memoryItems,
                // 图纸页面索引（供前端 drawing-preview 接口渲染图纸截图）
                drawingPdfPath = _store.ProfilePdfPath(profileId) ?? ResolvePdf(drawing),
                drawingPageIndex = 0
            };

            // M1（#35）：把完整响应落盘作为「下一次相同输入的复用源」，并把 recordId 校正为真实值。
            try
            {
                var rj = JsonSerializer.Serialize(response);
                if (JsonNode.Parse(rj) is JsonObject joOut)
                {
                    joOut["recordId"] = recordId;
                    rj = joOut.ToJsonString();
                    _store.UpdateCompareResponse(recordId, rj);
                }
                return JsonNode.Parse(rj)!;
            }
            catch
            {
                return response;   // 序列化异常不得影响本次比对结果
            }
        }
        finally
        {
            try { File.Delete(regionsFile); } catch { }
        }
    }

    /// <summary>拍平参与定点验证的 mark（Group 展开子项，组合体本身不做定点）。</summary>
    private static IEnumerable<DrawingMark> FlattenForVerify(IEnumerable<DrawingMark> marks)
    {
        foreach (var m in marks)
        {
            if (m.Type == MarkType.Group)
            {
                foreach (var kid in FlattenForVerify(m.Children ?? new List<DrawingMark>()))
                    yield return kid;
            }
            else
            {
                yield return m;
            }
        }
    }

    /// <summary>照片路径解析：绝对路径直接采信；否则走平台文件层（虚拟路径 / 文件名）。</summary>
    private string? ResolvePhoto(string nameOrPath)
    {
        if (string.IsNullOrWhiteSpace(nameOrPath)) return null;
        if (Path.IsPathRooted(nameOrPath) && File.Exists(nameOrPath)) return nameOrPath;

        foreach (var vpath in new[]
        {
            nameOrPath.Replace('\\', '/'),
            "photos/" + nameOrPath,
            "drawings/" + nameOrPath
        })
        {
            var phys = _files.ResolvePhysical(vpath);
            if (phys is not null && File.Exists(phys)) return phys;
        }
        return null;
    }

    /// <summary>
    /// 把入参解析为物理 PDF 路径。
    /// 绝对路径直接采信；否则按「虚拟路径 → 文件名」两级兜底走平台文件层（带穿越防护）。
    /// </summary>
    private string? ResolvePdf(string nameOrPath)
    {
        if (string.IsNullOrWhiteSpace(nameOrPath)) return null;

        if (Path.IsPathRooted(nameOrPath) && File.Exists(nameOrPath))
            return nameOrPath;

        var vpath = nameOrPath.Contains('/') || nameOrPath.Contains('\\')
            ? nameOrPath.Replace('\\', '/')
            : "drawings/" + nameOrPath;
        if (!vpath.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase)) vpath += ".pdf";

        var phys = _files.ResolvePhysical(vpath);
        return phys is not null && File.Exists(phys) ? phys : null;
    }

    /// <summary>mark → 传输对象（子项递归）。</summary>
    internal static object ToDto(DrawingMark m) => new
    {
        id = m.Id,
        type = m.Type.ToString(),
        view = m.View.ToString(),
        text = m.Text,
        required = m.Required,
        condition = m.Condition,
        normBbox = m.NormBbox,
        bbox = m.Bbox,
        direction = m.Direction,
        confidence = m.Confidence,
        ruleId = m.Source.RuleId,
        // 来源通道（pdf_vector / vision_fallback / manual）。
        // 2026-09-22 补：视觉兜底产出的 mark 若不在 API 暴露来源，下游无法区分
        // 「矢量层确证」与「OCR 补回」，可追溯性要求落空 —— 故显式输出。
        // 纯新增字段，不改既有字段名，对既有消费方向后兼容。
        sourceKind = m.Source.Kind,
        pageIndex = m.Source.PageIndex,
        spanIds = m.Source.SpanIds,
        imageIds = m.Source.ImageIds,
        evidence = m.Source.Evidence,
        children = m.Children is { Count: > 0 } ? m.Children.Select(c => ToDto(c)).ToList() : null
    };
}
