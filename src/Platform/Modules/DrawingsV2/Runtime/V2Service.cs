using System.Security.Cryptography;
using System.Text.Json;
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

        var configured = config["DrawingsV2:PhotoDir"];
        PhotoDir = !string.IsNullOrWhiteSpace(configured)
            ? configured!
            : Path.Combine(Path.GetDirectoryName(_store.DbPath) ?? ".", "drawingsv2_photos");
        try { Directory.CreateDirectory(PhotoDir); } catch { }
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

        var doc = await _py.ParseAsync(phys, ct);
        var key = Path.GetFileNameWithoutExtension(phys);
        var res = MarkBuilder.Build(doc, key);
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
    /// null/空/Unspecified 走页面级配准（向后兼容）。</param>
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

    /// <summary>比对记录详情（含完整 verdicts）。</summary>
    public object? CompareRecord(long id) => _store.GetCompareRecord(id);

    /// <summary>G1 人工复核：取档案复核页数据（基本信息 + 全部 marks 含 is_active/confirmed）。</summary>
    public object? ProfileForReview(long id) => _store.GetProfileForReview(id);

    /// <summary>G1 人工复核：保存复核结果（增量更新 marks 的 is_active/confirmed，档案置 reviewed）。</summary>
    public void ReviewProfile(long id, string? reviewedBy, string? note,
                              IReadOnlyList<(string MarkKey, bool IsActive, bool Confirmed)> items)
        => _store.SaveReview(id, reviewedBy, note, items);

    /// <summary>G2 会话合并：取一次多部位检验会话的聚合结果。</summary>
    public object? Session(string sessionId) => _store.GetSession(sessionId);

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
                double x = list[0].NormBbox![0], y0 = list[0].NormBbox[1];
                double w = list[0].NormBbox[2], h = list[0].NormBbox[3];
                double slot = h / list.Count;
                for (int i = 0; i < list.Count; i++)
                    list[i].NormBbox = new[] { x, y0 + slot * i, w, slot };
            }
        }
    }

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

        // G1：读取档案复核状态（兼容式：未 reviewed 仍比对，仅提示）
        var profileStatus = _store.GetProfileStatus(profileId);

        // 1.5) 视图级配准（方案 B）：解析 view → MarkView?，按视图过滤 + 用视图并集 bbox 重算 NormBbox
        // 重算后的 NormBbox 用于：构造 regions.json（Python verify 按视图级位置裁剪照片）
        // 与 MarkVerifier.Verify 的位置比较（C# 端在 Verify 内部再次构造同样的 viewNormMap）。
        // selectedView 为 null/Unspecified → viewNormMap 为 null，走原页面级流程（向后兼容）。
        MarkView? selectedView = null;
        MarkView? inferredView = null;   // 自动视图识别（Stage 3 自动找图块）
        Dictionary<string, double[]>? viewNormMap = null;
        var filteredMarks = marks;

        // 【自动视图识别】用户没选 view 时，先跑盲检，用 OCR 文本 + QR 解码反向匹配图纸 marks 的 View
        // 推断出唯一 view → 自动走方案B视图级配准；推断不出/冲突 → 降级页面级（向后兼容）
        JsonDocument? obsDoc = null;
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
                var inferred = MarkVerifier.InferViewFromBlind(marks, obsDoc.RootElement);
                if (inferred is { } iv && iv != MarkView.Unspecified)
                {
                    inferredView = iv;
                    view = iv.ToString();   // 统一走下面的视图级配准流程
                }
            }
        }

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
                viewNormMap = MarkVerifier.ApplyAffineRegistration(filteredMarks, viewNormMap, obsDoc.RootElement);
            }
        }

        // 2) 定点区域清单（Group 展开为子项；无坐标的 mark 无法定点，跳过并如实说明）
        // 视图级配准时：用视图级 NormBbox（按 mark.Id 查 viewNormMap），照片即视图坐标系
        var regions = new List<object>();
        var skipped = new List<string>();
        foreach (var m in FlattenForVerify(filteredMarks))
        {
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
            var verdict = MarkVerifier.Verify(marks, verDoc.RootElement, obsDoc?.RootElement, selectedView, viewNormMap);

            // 5) 落库留痕（历史回溯 / 复查）—— counts_json 同条记录存 selected_view 键
            var verdictDtos = verdict.Verdicts.Select(v => new
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
                expectedBbox = v.ExpectedBbox
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
                geometryMethod,
                geometryApplied,
                verifierVersion = V2Store.DecisionVersion,
                codeDetector = verdict.CodeDetector,
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

            // 照片访问 URL：PhotoDir 映射到 /drawingsv2-photos，取 photoPhys 相对 PhotoDir 的路径
            var photoRel = Path.GetRelativePath(PhotoDir, photoPhys).Replace('\\', '/');
            var photoUrl = $"/drawingsv2-photos/{photoRel}";

            return new
            {
                recordId,
                profileId,
                drawingKey,
                sessionId = sessionId ?? "",
                profileStatus,
                reviewHint = profileStatus == "reviewed"
                    ? ""
                    : "该图纸对象尚未人工确认（draft），比对结果仅供参考，建议先在「确认图纸对象」中复核。",
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
                geometryApplied,
                geometryMethod,
                skippedNoBbox = skipped,
                verdicts = verdictDtos,
                // 图纸页面索引（供前端 drawing-preview 接口渲染图纸截图）
                drawingPdfPath = _store.ProfilePdfPath(profileId) ?? ResolvePdf(drawing),
                drawingPageIndex = 0
            };
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
        pageIndex = m.Source.PageIndex,
        spanIds = m.Source.SpanIds,
        imageIds = m.Source.ImageIds,
        evidence = m.Source.Evidence,
        children = m.Children is { Count: > 0 } ? m.Children.Select(c => ToDto(c)).ToList() : null
    };
}
