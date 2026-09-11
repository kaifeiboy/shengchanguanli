using System.Text.Json;
using Microsoft.AspNetCore.Mvc;
using Platform.Core;

namespace Platform.Modules.InventoryTail;

/// <summary>
/// 库存尾数模块（独立模块，Key=inventory）。
/// 端点：materials(新增/批量)/search(模糊查询)/detail/records(出入库)/handlers(经手人下拉)/
///       ocr/correct(透视矫正+白平衡)/ocr/boxes(框选识别)/export(表格导出)。
/// 只挂载本模块端点（/api/inventory/*），不影响平台及其它模块。
/// </summary>
public class InventoryTailModule : IModule
{
    public string Key => "inventory";
    public string Name => "库存尾数";
    public string Icon => "📦";
    public int Order => 40;

    public void RegisterServices(IServiceCollection services)
    {
        services.AddSingleton<InventoryTailService>();
    }

    public void MapEndpoints(WebApplication app)
    {
        var g = app.MapGroup("/api/inventory");

        // 新增材料（单条）
        g.MapPost("/materials", (JsonElement body, InventoryTailService s) =>
        {
            try { return Results.Ok(s.AddMaterial(body)); }
            catch (InventoryTailException ex) { return Results.BadRequest(new { error = ex.Message }); }
        });

        // 批量新增（拍照识别多条，逐条返回成功/失败）
        g.MapPost("/materials/batch", (JsonElement body, InventoryTailService s) =>
        {
            try { return Results.Ok(s.AddMaterialsBatch(body)); }
            catch (InventoryTailException ex) { return Results.BadRequest(new { error = ex.Message }); }
        });

        // 模糊查询（库号/编码/材料信息 LIKE）
        g.MapGet("/search", ([FromQuery] string q, InventoryTailService s) =>
            Results.Ok(s.Search(q ?? "")));

        // 材料详情 + 全部出入库记录
        g.MapGet("/materials/{id:int}", (int id, InventoryTailService s) =>
        {
            var d = s.GetDetail(id);
            return d is null ? Results.NotFound(new { error = "材料不存在" }) : Results.Ok(d);
        });

        // 入库/出库
        g.MapPost("/materials/{id:int}/records", (int id, JsonElement body, InventoryTailService s) =>
        {
            try { return Results.Ok(s.AddRecord(id, body)); }
            catch (InventoryTailException ex) { return Results.BadRequest(new { error = ex.Message }); }
        });

        // 经手人下拉（历史去重，按最近操作排序）
        g.MapGet("/handlers", (InventoryTailService s) => Results.Ok(s.Handlers()));

        // 拍照：透视矫正 + 白平衡（默认自动检测；可传 pts 手动四点）
        g.MapPost("/ocr/correct", (HttpContext ctx, InventoryTailService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data" });
            var file = ctx.Request.Form.Files["file"];
            if (file is null || file.Length == 0)
                return Results.BadRequest(new { error = "未收到图片" });
            var pts = ctx.Request.Form["pts"].ToString();
            try { return Results.Ok(s.OcrCorrect(file, pts)); }
            catch (InventoryTailException ex) { return Results.BadRequest(new { error = ex.Message }); }
        });

        // 拍照：框选区域识别
        g.MapPost("/ocr/boxes", (HttpContext ctx, InventoryTailService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data" });
            var file = ctx.Request.Form.Files["file"];
            if (file is null || file.Length == 0)
                return Results.BadRequest(new { error = "未收到图片" });
            var boxesJson = ctx.Request.Form["boxes"].ToString();
            try { return Results.Ok(s.OcrBoxes(file, boxesJson)); }
            catch (InventoryTailException ex) { return Results.BadRequest(new { error = ex.Message }); }
        });

        // 导出（按查询条件生成 xlsx）
        g.MapGet("/export", ([FromQuery] string q, InventoryTailService s) => s.Export(q ?? ""));
    }
}
