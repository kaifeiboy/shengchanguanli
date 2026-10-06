using System.Text.Json;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Platform.Core;
using Platform.Modules.DrawingsV2.Decision;
using Platform.Modules.DrawingsV2.Runtime;

namespace Platform.Modules.DrawingsV2;

/// <summary>
/// 打标首件对比 v2 模块。
///
/// <para>【独立性保证 —— 对应「不得影响平台及其它模块」硬规则】
/// 独立 SQLite 库（drawingsv2.db）+ 独立 Python 脚本（Scripts/vpdf_run.py + vpdf 包）
/// + 独立端点前缀（/api/drawingsv2/*）。
/// 复用的只有两个**平台级设施**：<c>IPythonProcessFactory</c> 与 <c>FileAccessService</c>，
/// 均不含业务语义，属「模块依赖平台」而非「模块依赖模块」。
/// 不读写其它模块的表，不改任何既有端点，因此本模块的任何变更都不会影响
/// 维修解绑 / 不良履历 / 库存尾数等模块，也不会影响旧 drawings 模块的运行。</para>
///
/// <para>【并存策略】M3 阶段以 Key="drawingsv2" 与旧 drawings 模块**并存注册**，
/// 旧链路零改动、可并行验证。待 v2 端点覆盖 H5 调用面后（M5）再切换入口，
/// 届时用户可见的 URL 与入口保持不变。</para>
///
/// <para>端点：health、meta、analyze（剖析一份图纸）、profiles（列表/详情）。</para>
/// </summary>
public class DrawingsV2Module : IModule
{
    public string Key => "drawingsv2";
    public string Name => "打标首件对比 v2";
    public string Icon => "🧭";
    public int Order => 15;

    public void RegisterServices(IServiceCollection services)
    {
        services.AddSingleton<V2Store>();
        services.AddSingleton<V2Python>();
        services.AddSingleton<V2Service>();
    }

    public void MapEndpoints(WebApplication app)
    {
        var g = app.MapGroup("/api/drawingsv2");

        // 健康检查：Python 解释器 / 脚本定位 / 库路径 / 感知层自检
        g.MapGet("/health", async (V2Service s) => await Safe(async () => Results.Ok(await s.Health())));

        // 模块概览：库路径、决策层版本、档案与 mark 计数、规则分布
        g.MapGet("/meta", (V2Service s) => Safe(() => Results.Ok(s.Meta())));

        // 剖析一份图纸：name 或虚拟路径（drawings/xxx.pdf）
        g.MapPost("/analyze", async (JsonElement body, V2Service s, CancellationToken ct) =>
            await Safe(async () =>
            {
                var name = ReadString(body, "name") ?? ReadString(body, "path");
                if (string.IsNullOrWhiteSpace(name))
                    return Results.BadRequest(new { error = "缺少 name（图纸文件名或虚拟路径）", code = "bad_request" });
                return Results.Ok(await s.AnalyzeAsync(name!, ct));
            }));

        g.MapGet("/analyze", async ([FromQuery] string? name, V2Service s, CancellationToken ct) =>
            await Safe(async () =>
            {
                if (string.IsNullOrWhiteSpace(name))
                    return Results.BadRequest(new { error = "缺少查询参数 name", code = "bad_request" });
                return Results.Ok(await s.AnalyzeAsync(name!, ct));
            }));

        // 已剖析档案列表
        g.MapGet("/profiles", (V2Service s) => Safe(() => Results.Ok(s.Profiles())));

        // 某图纸 marks 的 View 分布（供前端动态生成视图选择器选项）
        g.MapGet("/views", (string drawing, V2Service s) => Safe(() =>
        {
            var v = s.ViewDistribution(drawing);
            return v is null ? Results.NotFound(new { error = $"找不到 v2 档案：{drawing}", code = "not_found" })
                             : Results.Ok(v);
        }));

        // 档案详情（含完整 mark 树）
        g.MapGet("/profiles/{id:long}", (long id, V2Service s) => Safe(() =>
        {
            var p = s.Profile(id);
            return p is null ? Results.NotFound(new { error = $"档案不存在：{id}", code = "not_found" })
                             : Results.Ok(p);
        }));

        // 首件比对：图纸档案（应打标对象）↔ 现场照片 → 八态判定
        // body: { drawing: "xxx.pdf" | 档案id, photo: "绝对路径|虚拟路径", observe?: true,
        //         view?: "TopCover|BottomCover|Side|Nameplate|Cable|Other"  // M6 视图级配准 }
        g.MapPost("/compare", async (JsonElement body, V2Service s, CancellationToken ct) =>
            await Safe(async () =>
            {
                var drawing = ReadString(body, "drawing") ?? ReadString(body, "name") ?? ReadString(body, "profileId");
                var photo = ReadString(body, "photo") ?? ReadString(body, "image");
                var observe = !body.TryGetProperty("observe", out var ob) || ob.ValueKind != JsonValueKind.False;
                var view = ReadString(body, "view");  // M6 视图级配准：拍摄视图枚举名
                var sessionId = ReadString(body, "sessionId");  // G2 会话合并：不传则新建会话
                if (string.IsNullOrWhiteSpace(drawing) || string.IsNullOrWhiteSpace(photo))
                    return Results.BadRequest(new
                    {
                        error = "缺少 drawing（图纸文件名或档案id）或 photo（照片路径）",
                        code = "bad_request"
                    });
                return Results.Ok(await s.CompareAsync(drawing!, photo!, observe, ct, view, sessionId));
            }));

        // 首件比对（照片上传版）：multipart/form-data，字段沿用旧链路习惯
        //   drawing : 图纸文件名 / 虚拟路径 / v2 档案 id
        //   file    : 手机拍的照片
        //   view    : 拍摄视图（M6 视图级配准）TopCover/BottomCover/Side/Nameplate/Cable/Other
        g.MapPost("/compare/upload", async (HttpContext ctx, V2Service s, CancellationToken ct) =>
            await Safe(async () =>
            {
                if (!ctx.Request.HasFormContentType)
                    return Results.BadRequest(new { error = "需要 multipart/form-data", code = "bad_request" });

                var form = await ctx.Request.ReadFormAsync(ct);
                var drawing = Str(form, "drawing") ?? Str(form, "name") ?? Str(form, "profileId")
                              ?? Str(form, "drawingId");
                var file = form.Files["file"] ?? (form.Files.Count > 0 ? form.Files[0] : null);
                if (string.IsNullOrWhiteSpace(drawing))
                    return Results.BadRequest(new { error = "缺少 drawing（图纸或档案id）", code = "bad_request" });
                if (file is null || file.Length == 0)
                    return Results.BadRequest(new { error = "未收到图片文件（字段名 file）", code = "bad_request" });

                var observe = !(form.TryGetValue("observe", out var obv)
                                && (obv.ToString().Equals("false", StringComparison.OrdinalIgnoreCase) || obv.ToString() == "0"));
                var view = Str(form, "view");  // M6 视图级配准：拍摄视图枚举名
                var sessionId = Str(form, "sessionId");  // G2 会话合并：不传则新建会话

                var temp = Path.Combine(Path.GetTempPath(), $"v2_upload_{Guid.NewGuid():N}{Path.GetExtension(file.FileName)}");
                try
                {
                    await using (var fs = new FileStream(temp, FileMode.Create, FileAccess.Write, FileShare.None))
                        await file.CopyToAsync(fs, ct);
                    return Results.Ok(await s.CompareUploadAsync(temp, file.FileName, drawing!, observe, ct, view, sessionId));
                }
                finally
                {
                    try { File.Delete(temp); } catch { }
                }
            }));

        // P2 · 逻辑图块：读取某档案的块（docs/逻辑图块方案_2026-09-26.md §4）
        //   ?hasMarkingOnly=true → 只返回参与匹配的块（= P3 命中检索候选集，空块被天然隔离）
        //   ?withElements=false  → 不返回块内元素（只取块级摘要，响应更小）
        g.MapGet("/profiles/{id:long}/blocks", (long id, V2Service s,
                 [FromQuery] bool? hasMarkingOnly, [FromQuery] bool? withElements) =>
            Safe(() => Results.Ok(s.LogicalBlocks(id, hasMarkingOnly ?? false, withElements ?? true))));

        // P2 · 逻辑图块：触发提取并落库（整体覆盖同一 profile+页）。
        // 耗时较长（含块级局部 OCR，实测 20~60s/张），故不与 analyze 主链路耦合。
        // body: { "ocr": false } 可只走文本层（判空会退化，仅供调试）
        g.MapPost("/profiles/{id:long}/blocks", async (long id, JsonElement body, V2Service s, CancellationToken ct) =>
            await Safe(async () =>
            {
                var ocrOn = !body.TryGetProperty("ocr", out var oc) || oc.ValueKind != JsonValueKind.False;
                var res = await s.ExtractLogicalBlocksAsync(id, ocrOn, ct);
                return Results.Ok(res);
            }));

        // 逻辑图块复核（2026-09-30 H5 待复核界面）：保存人工块框 override 并按其重提取落库
        // （整体覆盖该档案全部块；写入 data/drawingsv2_blocks_override/{id}.json，含 OCR 约 20~60s）。
        // body: { blocks: [ { bboxPt:[x0,y0,x1,y1] | norm:[x,y,w,h], name?, viewHint?, hasMarking? } ], note?, ocr? }
        g.MapPost("/profiles/{id:long}/blocks/override", async (long id, JsonElement body, V2Service s, CancellationToken ct) =>
            await Safe(async () => Results.Ok(await s.SaveBlocksOverrideAsync(id, body, ct))));

        // P3 · 文本命中选块 + 四色标示（方案 §0.5.1 / §0.5.3）：
        //   照片 OCR 文本 → 检索图块打标内容 → 命中绿 / 缺标红 / 相似·低置信·多出黄 / 图标·QR·d 类灰。
        //   body: { photo: "绝对路径|虚拟路径" }
        //   ⚠ P3 阶段为独立端点，**不改 compare 的 verdicts 口径**（零回归）；命中质量确认后再决定是否切换主判定。
        g.MapPost("/profiles/{id:long}/blocks/match", async (long id, JsonElement body, V2Service s, CancellationToken ct) =>
            await Safe(async () =>
            {
                var photo = ReadString(body, "photo") ?? ReadString(body, "image");
                if (string.IsNullOrWhiteSpace(photo))
                    return Results.BadRequest(new { error = "缺少 photo（照片绝对路径或平台虚拟路径）", code = "bad_request" });
                return Results.Ok(await s.MatchBlocksAsync(id, photo!, ct));
            }));

        // M7（库管理）导入图纸：multipart 上传（单/批量，同名 file），逐张落盘→剖析登记→入队抽取
        g.MapPost("/profiles/import", async (HttpContext ctx, V2Service s, CancellationToken ct) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data 上传图纸", code = "bad_request" });
            var files = ctx.Request.Form.Files;
            var model = ctx.Request.Form["model"].ToString();
            var results = new List<object>();
            foreach (var f in files)
            {
                if (f is null || f.Length == 0) { results.Add(new { profileId = (long?)null, fileName = f?.FileName, drawingKey = (string?)null, error = "空文件" }); continue; }
                using var ms = f.OpenReadStream();
                results.Add(await s.ImportOneAsync(ms, f.FileName,
                    string.IsNullOrWhiteSpace(model) ? null : model, ct));
            }
            return Results.Ok(new { count = results.Count(r => r is V2Service.ImportResult ir && ir.Error is null), results });
        });

        // M7（库管理）扫描图纸根目录，自动登记并抽取库缺的 PDF
        g.MapPost("/profiles/sync", async (V2Service s, CancellationToken ct) =>
            await Safe(async () => Results.Ok(await s.SyncFolderAsync(ct))));

        // M7（库管理）查询档案逻辑块抽取状态
        g.MapGet("/profiles/{id:long}/blocks/status", (long id, V2Service s) => Safe(() =>
        {
            var st = s.GetBlockStatus(id);
            return st is null ? Results.NotFound(new { error = $"档案不存在：{id}", code = "not_found" })
                                : Results.Ok(st);
        }));

        // M7（库管理）档案原图下载（替代 V1 detail/virtualPath 链路，供 H5 详情页「下载原图」）
        g.MapGet("/profiles/{id:long}/pdf", (long id, V2Service s) => Safe(() =>
        {
            var p = s.ProfilePdf(id);
            return p is null
                ? Results.NotFound(new { error = $"档案不存在或 PDF 缺失：{id}", code = "not_found" })
                : Results.File(p.Value.PdfPath, "application/pdf", p.Value.DrawingKey + ".pdf");
        }));

        // 逻辑图块复核：渲染档案图纸某页 PNG（供 H5 复核弹窗叠加块框，避免把文件系统路径暴露给前端）。
        g.MapGet("/profiles/{id:long}/preview", async (long id, HttpContext ctx, V2Service s, CancellationToken ct) =>
        {
            var page = int.TryParse(ctx.Request.Query["page"], out var p) ? p : 0;
            try
            {
                var png = await s.RenderProfilePageAsync(id, page, ct);
                return png is null
                    ? Results.NotFound(new { error = $"找不到图纸：{id}", code = "not_found" })
                    : Results.File(png, "image/png");
            }
            catch (V2Exception ex) { return Results.BadRequest(new { error = ex.Message, code = "render_failed" }); }
        });


        // G1 人工复核：取档案复核页数据（基本信息 + 全部 marks 含 is_active/confirmed）
        g.MapGet("/profiles/{id:long}/review", (long id, V2Service s) => Safe(() =>
        {
            var p = s.ProfileForReview(id);
            return p is null ? Results.NotFound(new { error = $"档案不存在：{id}", code = "not_found" })
                             : Results.Ok(p);
        }));

        // G1 人工复核：保存复核结果（人工确认图纸对象后，档案置 reviewed）
        g.MapPost("/profiles/{id:long}/review", async (long id, JsonElement body, V2Service s) =>
            await Safe(async () =>
            {
                var reviewedBy = ReadString(body, "reviewedBy") ?? ReadString(body, "reviewer");
                var note = ReadString(body, "note") ?? ReadString(body, "reviewNote");
                // 逻辑图块复核口径（2026-09-30）：items 可缺省 = 不改 marks，仅置 reviewed
                // （复核内容以逻辑图块为准，marks 属旧引擎遗留）。
                var items = new List<(string MarkKey, bool IsActive, bool Confirmed)>();
                if (body.TryGetProperty("items", out var itemsEl) && itemsEl.ValueKind == JsonValueKind.Array)
                {
                    foreach (var it in itemsEl.EnumerateArray())
                    {
                        var mk = ReadString(it, "markKey") ?? ReadString(it, "id");
                        if (string.IsNullOrWhiteSpace(mk)) continue;
                        var isActive = !it.TryGetProperty("isActive", out var ia) || ia.ValueKind != JsonValueKind.False;
                        var confirmed = it.TryGetProperty("confirmed", out var cf) && cf.ValueKind == JsonValueKind.True;
                        items.Add((mk!, isActive, confirmed));
                    }
                }
                s.ReviewProfile(id, reviewedBy, note, items);
                return Results.Ok(new { ok = true, profileId = id, status = "reviewed" });
            }));

        // 【P1b 设计§3】人工复核·增删改 mark
        g.MapPost("/profiles/{id:long}/marks", async (long id, JsonElement body, V2Service s) =>
            await Safe(async () =>
            {
                var type = ReadString(body, "type") ?? ReadString(body, "markType");
                if (string.IsNullOrWhiteSpace(type) || type is not ("Text" or "Icon" or "Qr" or "Group"))
                    return Results.BadRequest(new { error = "type 必须属于 Text/Icon/Qr/Group", code = "bad_request" });
                var view = ReadString(body, "view") ?? "Unspecified";
                var text = ReadString(body, "text");
                var required = !body.TryGetProperty("required", out var rq) || rq.ValueKind != JsonValueKind.False;
                var condition = ReadString(body, "condition") ?? ReadString(body, "conditionText");
                var (nx, ny, nw, nh) = ParseNorm(body);
                if (nx is null || ny is null || nw is null || nh is null)
                    return Results.BadRequest(new { error = "缺少有效的 norm 坐标 (x,y,w,h)", code = "bad_request" });
                var newId = s.AddMark(id, type, view, text, required, condition, nx, ny, nw, nh);
                return Results.Ok(new { ok = true, profileId = id, markId = newId });
            }));
        g.MapPut("/profiles/{id:long}/marks/{markId:long}", async (long id, long markId, JsonElement body, V2Service s) =>
            await Safe(async () =>
            {
                var type = ReadString(body, "type") ?? ReadString(body, "markType");
                if (string.IsNullOrWhiteSpace(type) || type is not ("Text" or "Icon" or "Qr" or "Group"))
                    return Results.BadRequest(new { error = "type 必须属于 Text/Icon/Qr/Group", code = "bad_request" });
                var view = ReadString(body, "view") ?? "Unspecified";
                var text = ReadString(body, "text");
                var required = !body.TryGetProperty("required", out var rq) || rq.ValueKind != JsonValueKind.False;
                var condition = ReadString(body, "condition") ?? ReadString(body, "conditionText");
                var (nx, ny, nw, nh) = ParseNorm(body);
                if (nx is null || ny is null || nw is null || nh is null)
                    return Results.BadRequest(new { error = "缺少有效的 norm 坐标 (x,y,w,h)", code = "bad_request" });
                var ok = s.UpdateMark(id, markId, type, view, text, required, condition, nx, ny, nw, nh);
                return ok ? Results.Ok(new { ok = true, markId })
                         : Results.NotFound(new { error = $"未找到 mark：{id}/{markId}", code = "not_found" });
            }));
        g.MapDelete("/profiles/{id:long}/marks/{markId:long}", async (long id, long markId, V2Service s) =>
            await Safe(async () =>
            {
                var ok = s.DeleteMark(id, markId);
                return ok ? Results.Ok(new { ok = true, markId })
                         : Results.NotFound(new { error = $"未找到 mark：{id}/{markId}", code = "not_found" });
            }));

        // 【B 项·capture】为 QR mark 录入/清除预期解码内容（供 VerifyQr 内容身份比对）
        g.MapPost("/profiles/{id:long}/marks/{markId:long}/expected-text",
            async (long id, long markId, JsonElement body, V2Service s) =>
            await Safe(async () =>
            {
                var text = ReadString(body, "text") ?? ReadString(body, "expectedText");
                var ok = s.SetMarkExpectedText(id, markId, text);
                return ok
                    ? Results.Ok(new { ok = true, profileId = id, markId, text })
                    : Results.NotFound(new { error = $"未找到 QR mark：{id}/{markId}", code = "not_found" });
            }));

        // G2 会话合并：取一次多部位检验会话的聚合结果
        g.MapGet("/sessions/{id}", (string id, V2Service s) => Safe(() =>
        {
            var sess = s.Session(id);
            return sess is null ? Results.NotFound(new { error = $"会话不存在：{id}", code = "not_found" })
                                 : Results.Ok(sess);
        }));

        // 比对记录列表（历史回溯）
        g.MapGet("/records", ([FromQuery] int? limit, V2Service s) =>
            Safe(() => Results.Ok(s.CompareRecords(limit ?? 50))));

        // 比对记录详情（含完整 verdicts）
        g.MapGet("/records/{id:long}", (long id, V2Service s) => Safe(() =>
        {
            var r = s.CompareRecord(id);
            return r is null ? Results.NotFound(new { error = $"记录不存在：{id}", code = "not_found" })
                             : Results.Ok(r);
        }));

        // ② 判定级人工确认：取某条比对记录的人工判定
        g.MapGet("/records/{id:long}/verdicts", (long id, V2Service s) => Safe(() =>
            Results.Ok(s.VerdictReviews(id))));

        // ② 判定级人工确认：保存人工判定（HumanPresent 实物有标 / HumanMissing 实物缺标 / HumanWrongPart 拍错部位）
        // 会话聚合时人工判定优先于系统判定；系统原判定随记录留痕，不会被覆盖。
        g.MapPost("/records/{id:long}/verdicts", (long id, JsonElement body, V2Service s) =>
            Safe(() =>
            {
                var by = ReadString(body, "by") ?? ReadString(body, "reviewedBy");
                if (!body.TryGetProperty("items", out var itemsEl) || itemsEl.ValueKind != JsonValueKind.Array)
                    return Results.BadRequest(new { error = "缺少 items（人工判定数组）", code = "bad_request" });
                var items = new List<(string MarkKey, string HumanState, string? Note)>();
                foreach (var it in itemsEl.EnumerateArray())
                {
                    var mk = ReadString(it, "markKey");
                    var st = ReadString(it, "humanState") ?? ReadString(it, "state");
                    var note = ReadString(it, "note");
                    if (string.IsNullOrWhiteSpace(mk) || string.IsNullOrWhiteSpace(st)) continue;
                    if (st is not ("HumanPresent" or "HumanMissing" or "HumanWrongPart"))
                        return Results.BadRequest(new { error = $"非法 humanState：{st}", code = "bad_request" });
                    items.Add((mk!, st!, note));
                }
                if (items.Count == 0)
                    return Results.BadRequest(new { error = "items 为空", code = "bad_request" });
                var n = s.SaveVerdictReviews(id, items, by);
                return Results.Ok(new { ok = true, recordId = id, saved = n });
            }));

        // 图纸页面预览：渲染 PDF 指定页为 PNG（供 H5 可视化图纸回显）
        g.MapGet("/drawing-preview", async (HttpContext ctx, V2Service s, CancellationToken ct) =>
        {
            var pdf = ctx.Request.Query["pdf"].ToString();
            var page = int.TryParse(ctx.Request.Query["page"], out var p) ? p : 0;
            if (string.IsNullOrWhiteSpace(pdf))
                return Results.BadRequest(new { error = "缺少 pdf 参数", code = "missing_pdf" });

            try
            {
                var png = await s.RenderDrawingPageAsync(pdf, page, ct);
                return png is null
                    ? Results.NotFound(new { error = $"找不到图纸：{pdf}", code = "not_found" })
                    : Results.File(png, "image/png");
            }
            catch (V2Exception ex)
            {
                return Results.BadRequest(new { error = ex.Message, code = "render_failed" });
            }
        });

        // 比对记录「已标示照片」：缩略图（mode=thumb）或原图（mode=full）。
        // 四色结论由 Scripts/render_marked.py 从记录响应渲染，首次访问落盘缓存，之后直接回读。
        g.MapGet("/records/{id:long}/photo", async (long id, HttpContext ctx, V2Service s, CancellationToken ct) =>
        {
            var mode = ctx.Request.Query["mode"].ToString();
            try
            {
                var r = await s.GetRecordMarkedPhoto(id, mode, ct);
                return r is null
                    ? Results.NotFound(new { error = $"记录不存在或照片缺失：{id}", code = "not_found" })
                    : Results.File(r.Value.Bytes, r.Value.ContentType);
            }
            catch (V2Exception ex)
            {
                return Results.BadRequest(new { error = ex.Message, code = "render_failed" });
            }
        });
    }

    private static string? ReadString(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return null;
        return v.ValueKind switch
        {
            JsonValueKind.String => v.GetString(),
            JsonValueKind.Number => v.GetRawText(),
            _ => null
        };
    }

    /// <summary>从 multipart 表单取值（兼容多种字段名的客户端写法）。</summary>
    private static string? Str(IFormCollection form, string name)
        => form.TryGetValue(name, out var v) && !string.IsNullOrWhiteSpace(v.ToString()) ? v.ToString() : null;

    /// <summary>
    /// 统一错误出口：业务异常转 400 且带机器可读 code，客户端永远看不到堆栈。
    /// </summary>
    private static (double?, double?, double?, double?) ParseNorm(JsonElement body)
    {
        double? G(string n) => body.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;
        double? x = G("normX") ?? G("x");
        double? y = G("normY") ?? G("y");
        double? w = G("normW") ?? G("w");
        double? h = G("normH") ?? G("h");
        if (x is null && body.TryGetProperty("norm", out var nm) && nm.ValueKind == JsonValueKind.Object)
        {
            x = nm.TryGetProperty("x", out var a) && a.ValueKind == JsonValueKind.Number ? a.GetDouble() : null;
            y = nm.TryGetProperty("y", out var b) && b.ValueKind == JsonValueKind.Number ? b.GetDouble() : null;
            w = nm.TryGetProperty("w", out var c) && c.ValueKind == JsonValueKind.Number ? c.GetDouble() : null;
            h = nm.TryGetProperty("h", out var d) && d.ValueKind == JsonValueKind.Number ? d.GetDouble() : null;
        }
        return (x, y, w, h);
    }

    private static async Task<IResult> Safe(Func<Task<IResult>> act)
    {
        try { return await act(); }
        catch (V2Exception e) { return Results.BadRequest(new { error = e.Message, code = "business" }); }
        catch (Exception e) { return Results.BadRequest(new { error = "服务异常：" + e.Message, code = "internal" }); }
    }

    private static IResult Safe(Func<IResult> act)
    {
        try { return act(); }
        catch (V2Exception e) { return Results.BadRequest(new { error = e.Message, code = "business" }); }
        catch (Exception e) { return Results.BadRequest(new { error = "服务异常：" + e.Message, code = "internal" }); }
    }
}
