using System.Text.Json;
using Microsoft.AspNetCore.Mvc;
using Platform.Core;

namespace Platform.Modules.DefectHistory;

/// <summary>
/// 不良履历查询模块（v2 底层重构）：单工作簿单 Sheet 数据源。
/// 端点：meta/query/add/batch/delete/image/export/template + analysis/analysis/export/analysis/status。
/// 每月 2 号由 DefectAnalysisWorker 自动生成上月全型号分析报告。
/// 只挂载本模块端点（/api/defecthistory/*），不影响其它模块。
/// </summary>
public class DefectHistoryModule : IModule
{
    public string Key => "defecthistory";
    public string Name => "不良履历查询";
    public string Icon => "📋";
    public int Order => 30;

    public void RegisterServices(IServiceCollection services)
    {
        services.AddSingleton<DefectHistoryService>();
        services.AddHostedService<DefectAnalysisWorker>();
    }

    public void MapEndpoints(WebApplication app)
    {
        var g = app.MapGroup("/api/defecthistory");

        // 数据源概览 + 月份清单（基于第一列日期统计）
        g.MapGet("/meta", (DefectHistoryService s) =>
            Safe(() => Results.Ok(s.Meta())));

        // 按型号模糊查询（可选月份过滤，返回平铺行列表）；model 可空=全部查询
        g.MapGet("/query", ([FromQuery] string? model, [FromQuery] string? month, DefectHistoryService s) =>
        {
            return Safe(() => Results.Ok(s.Query(model ?? "", month)));
        });

        // 单条录入（multipart：表单字段 + files["image"] 可多张；发生工程/处理方式手动录入）
        g.MapPost("/add", (HttpContext ctx, DefectHistoryService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data 提交" });
            var form = ctx.Request.Form;
            return Safe(() => Results.Ok(s.Add(form, form.Files)));
        });

        // 批量导入（?preview=true 仅预览；上传含 SMT 工作表自动舍弃）
        g.MapPost("/batch", (HttpContext ctx, DefectHistoryService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data 上传文件" });
            var file = ctx.Request.Form.Files["file"];
            var preview = string.Equals(ctx.Request.Query["preview"], "true", StringComparison.OrdinalIgnoreCase);
            if (file is null) return Results.BadRequest(new { error = "未收到上传文件（字段名 file）" });
            return Safe(() => Results.Ok(s.Batch(file, preview)));
        });

        // 删除一条（按数据行索引；file/sheet 兼容保留）
        g.MapPost("/delete", (JsonElement body, DefectHistoryService s) =>
        {
            var file = TryGetString(body, "file");
            var sheet = TryGetString(body, "sheet");
            var row = TryGetInt(body, "row");
            if (row < 0)
                return Results.BadRequest(new { error = "row 参数不合法" });
            return Safe(() => Results.Ok(s.Delete(file, sheet, row)));
        });

        // 批量删除（一次写入删多行，body: {rows:[3,5,7]}；内部降序+图位移，杜绝误删）
        g.MapPost("/batch-delete", (JsonElement body, DefectHistoryService s) =>
        {
            try { return Results.Ok(s.BatchDelete(body)); }
            catch (DefectHistoryException ex) { return Results.BadRequest(new { error = ex.Message }); }
        });

        // 不良图片（某行第 i 张；thumb=true|1 返回缩略图；成功=private max-age=3600，失败=no-store）
        g.MapGet("/image", (HttpContext ctx, [FromQuery] string? file, [FromQuery] string? sheet,
            [FromQuery] int row, [FromQuery] int i, [FromQuery] string? thumb, DefectHistoryService s) =>
        {
            var isThumb = thumb == "1" || string.Equals(thumb, "true", StringComparison.OrdinalIgnoreCase);
            return s.Image(ctx, file, sheet, row, i, isThumb);
        });

        // 导出（full=true|1 全量；否则按 model[&month] 查询结果；表头+框线+图片锚定）
        g.MapGet("/export", ([FromQuery] string? model, [FromQuery] string? month, [FromQuery] string? full, DefectHistoryService s) =>
            s.Export(model, month, full == "1" || string.Equals(full, "true", StringComparison.OrdinalIgnoreCase)));

        // 上传录入模板下载
        g.MapGet("/template", (DefectHistoryService s) =>
            s.Template());

        // 分析：按月×线别聚合 IPQC/QA 次数（JSON，供 H5 绘图；model 可选=全型号）
        g.MapGet("/analysis", ([FromQuery] string month, [FromQuery] string? model, DefectHistoryService s) =>
        {
            if (string.IsNullOrWhiteSpace(month)) return Results.BadRequest(new { error = "缺少 month（YYYY-MM）" });
            return Safe(() => Results.Ok(s.Analysis(month, model)));
        });

        // 分析导出：xlsx（原生堆叠柱状图 + 数据表表头框线）
        g.MapGet("/analysis/export", ([FromQuery] string month, [FromQuery] string? model, DefectHistoryService s) =>
            s.AnalysisExport(month, model));

        // 最新自动分析状态（每月 2 号生成上月全型号报告）
        g.MapGet("/analysis/status", (DefectHistoryService s) =>
            Safe(() => Results.Ok(s.AnalysisStatus())));
    }

    private static string? TryGetString(JsonElement el, string name)
    {
        if (el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String)
            return v.GetString();
        return null;
    }

    private static int TryGetInt(JsonElement el, string name)
    {
        if (el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var i))
            return i;
        return -1;
    }

    private static IResult Safe(Func<IResult> act)
    {
        try { return act(); }
        catch (DefectHistoryException e) { return Results.BadRequest(new { error = e.Message }); }
        catch (Exception e) { return Results.BadRequest(new { error = "服务异常：" + e.Message }); }
    }
}
