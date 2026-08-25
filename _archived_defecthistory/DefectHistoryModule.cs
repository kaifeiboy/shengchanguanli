using System.Text.Json;
using Microsoft.AspNetCore.Mvc;
using Platform.Core;

namespace Platform.Modules.DefectHistory;

/// <summary>
/// 不良履历查询模块：按型号跨 E:\生产不良履历 全部月份 .xls 查询 + H5 单条/批量录入写回数据源。
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
    }

    public void MapEndpoints(WebApplication app)
    {
        var g = app.MapGroup("/api/defecthistory");

        // 月份清单（下拉用）
        g.MapGet("/meta", (DefectHistoryService s) =>
            Safe(() => Results.Ok(s.Meta())));

        // 按型号模糊查询（可选月份过滤）
        g.MapGet("/query", ([FromQuery] string model, [FromQuery] string? month, DefectHistoryService s) =>
        {
            if (string.IsNullOrWhiteSpace(model)) return Results.BadRequest(new { error = "请输入型号" });
            return Safe(() => Results.Ok(s.Query(model.Trim(), month)));
        });

        // 单条录入（缺月份自动建表）
        g.MapPost("/add", (JsonElement body, DefectHistoryService s) =>
        {
            AddRequest req;
            try { req = body.Deserialize<AddRequest>() ?? new AddRequest(); }
            catch { return Results.BadRequest(new { error = "请求体格式错误" }); }
            return Safe(() => Results.Ok(s.Add(req)));
        });

        // 批量导入（?preview=true 仅预览）
        g.MapPost("/batch", (HttpContext ctx, DefectHistoryService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data 上传文件" });
            var file = ctx.Request.Form.Files["file"];
            var preview = string.Equals(ctx.Request.Query["preview"], "true", StringComparison.OrdinalIgnoreCase);
            if (file is null) return Results.BadRequest(new { error = "未收到上传文件（字段名 file）" });
            return Safe(() => Results.Ok(s.Batch(file, preview)));
        });

        // 删除一条（按 文件+sheet+行索引，写前自动备份）
        g.MapPost("/delete", (JsonElement body, DefectHistoryService s) =>
        {
            var file = TryGetString(body, "file");
            var sheet = TryGetString(body, "sheet");
            var row = TryGetInt(body, "row");
            if (string.IsNullOrWhiteSpace(file) || string.IsNullOrWhiteSpace(sheet) || row < 0)
                return Results.BadRequest(new { error = "file/sheet/row 参数不合法" });
            return Safe(() => Results.Ok(s.Delete(file!, sheet!, row)));
        });
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
