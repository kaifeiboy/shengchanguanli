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
                if (!body.TryGetProperty("items", out var itemsEl) || itemsEl.ValueKind != JsonValueKind.Array)
                    return Results.BadRequest(new { error = "缺少 items（复核项数组）", code = "bad_request" });
                var items = new List<(string MarkKey, bool IsActive, bool Confirmed)>();
                foreach (var it in itemsEl.EnumerateArray())
                {
                    var mk = ReadString(it, "markKey") ?? ReadString(it, "id");
                    if (string.IsNullOrWhiteSpace(mk)) continue;
                    var isActive = !it.TryGetProperty("isActive", out var ia) || ia.ValueKind != JsonValueKind.False;
                    var confirmed = it.TryGetProperty("confirmed", out var cf) && cf.ValueKind == JsonValueKind.True;
                    items.Add((mk!, isActive, confirmed));
                }
                s.ReviewProfile(id, reviewedBy, note, items);
                return Results.Ok(new { ok = true, profileId = id, status = "reviewed" });
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
