using System.Text.Json;
using Microsoft.AspNetCore.Mvc;
using Platform.Core;

namespace Platform.Modules.InvTail;

/// <summary>
/// 库存尾数模块（v2 全新重建，Key=invtail）。
/// <para>独立性保证：独立 SQLite 库（invtail.db）+ 独立 Python 脚本（inv_ocr.py / inv_export.py）
/// + 自带 Python 进程工厂，只挂载 /api/invtail/* 端点，不读写平台共享库、不引用其它业务模块，
/// 因此本模块的任何变更都不会影响图纸比对 / 维修解绑 / 不良履历 / 包装检查等模块。</para>
/// <para>端点总览：
/// meta、handlers、materials(查/增/改/删/批量)、materials/{id}/move(出入库)、records/{id}/delete、
/// ocr/prepare(透视矫正+白平衡)、ocr/rewarp(手动四角微调)、ocr/image(取处理后图)、ocr/recognize(只识别框选)、export(明细流水 xlsx)。</para>
/// </summary>
public class InvTailModule : IModule
{
    public string Key => "invtail";
    public string Name => "库存尾数";
    public string Icon => "📦";
    public int Order => 40;

    public void RegisterServices(IServiceCollection services)
    {
        services.AddSingleton<InvTailStore>();
        services.AddSingleton<InvTailPython>();
        services.AddSingleton<InvTailService>();
    }

    public void MapEndpoints(WebApplication app)
    {
        var g = app.MapGroup("/api/invtail");

        // ---------- 概览 / 经手人 ----------

        // 模块概览：材料数、流水数、总库存、经手人（最后操作的人排最前）
        g.MapGet("/meta", (InvTailService s) => Safe(() => Results.Ok(s.Meta())));

        // 经手人下拉（最后操作的人排最前，支持前端手动输入新名字）
        g.MapGet("/handlers", (InvTailService s) => Safe(() => Results.Ok(s.Handlers())));

        // 经手人管理：删除（只移除下拉候选，历史流水保留）/ 改名（syncRecords=true 时同步历史流水）
        g.MapPost("/handlers/delete", (JsonElement body, InvTailService s) =>
            Safe(() => Results.Ok(s.DeleteHandler(Str(body, "name")))));

        g.MapPost("/handlers/rename", (JsonElement body, InvTailService s) =>
            Safe(() => Results.Ok(s.RenameHandler(Str(body, "oldName"), Str(body, "newName"), Bool(body, "syncRecords")))));

        // ---------- 材料主档 ----------

        // 模糊查询：关键词同时匹配 库号 / 编码 / 材料信息；q 为空=全部（按最近操作倒序）
        g.MapGet("/materials", ([FromQuery] string? q, [FromQuery] int? page, [FromQuery] int? size, InvTailService s) =>
            Safe(() => Results.Ok(s.Search(q, page ?? 1, size ?? 20))));

        // 材料详情 + 出入库流水（含逐笔结存）
        g.MapGet("/materials/{id:long}", (long id, InvTailService s) => Safe(() => Results.Ok(s.Detail(id))));

        // 新增材料（数量记为首笔入库流水；库号/编码任一重复即拦截并回传已存在记录）
        g.MapPost("/materials", (JsonElement body, InvTailService s) =>
            Safe(() => Results.Ok(s.CreateMaterial(ReadCreate(body)))));

        // 批量新增（拍照识别一次框选多条）：逐条独立处理，返回成功与失败明细
        g.MapPost("/materials/batch", (JsonElement body, InvTailService s) => Safe(() =>
        {
            var list = new List<InvCreateRequest>();
            if (body.TryGetProperty("items", out var arr) && arr.ValueKind == JsonValueKind.Array)
                foreach (var it in arr.EnumerateArray()) list.Add(ReadCreate(it));
            return Results.Ok(s.CreateMaterialsBatch(list, Str(body, "handler")));
        }));

        // 修改材料主档（库号/编码/材料信息）
        g.MapPost("/materials/{id:long}/update", (long id, JsonElement body, InvTailService s) =>
            Safe(() => Results.Ok(s.UpdateMaterial(id, ReadCreate(body)))));

        // 删除材料（连带流水，误建档纠正用）
        g.MapPost("/materials/{id:long}/delete", (long id, InvTailService s) =>
            Safe(() => Results.Ok(s.DeleteMaterial(id))));

        // ---------- 出入库 ----------

        // 出入库记账：direction=in|out，记录数量 + 当前时间 + 经手人；出库超量拦截
        g.MapPost("/materials/{id:long}/move", (long id, JsonElement body, InvTailService s) => Safe(() =>
            Results.Ok(s.Move(id, new InvMoveRequest
            {
                Direction = Str(body, "direction"),
                Qty = Num(body, "qty"),
                Handler = Str(body, "handler"),
                Note = Str(body, "note")
            }))));

        // 删除一笔流水（误录纠正）
        g.MapPost("/records/{id:long}/delete", (long id, InvTailService s) =>
            Safe(() => Results.Ok(s.DeleteRecord(id))));

        // 修改某笔流水（已在条目中生效）的经手人：只改经手人，不动方向与数量，库存不变
        g.MapPost("/records/{id:long}/handler", (long id, JsonElement body, InvTailService s) =>
            Safe(() => Results.Ok(s.UpdateRecordHandler(id, Str(body, "handler")))));

        // ---------- 拍照识别 ----------

        // 第一步：上传照片 → 自动检测文档四角做透视畸变矫正 + 白平衡 → 返回处理后图供框选
        g.MapPost("/ocr/prepare", (HttpContext ctx, InvTailService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data 上传照片" });
            var form = ctx.Request.Form;
            var file = form.Files["image"] ?? (form.Files.Count > 0 ? form.Files[0] : null);
            if (file is null) return Results.BadRequest(new { error = "未收到照片（字段名 image）" });
            var wb = form["wb"].ToString();
            var autoWarp = form["autoWarp"].ToString();
            var doAuto = autoWarp != "0" && !string.Equals(autoWarp, "false", StringComparison.OrdinalIgnoreCase);
            return Safe(() => Results.Ok(s.OcrPrepare(file, wb, doAuto)));
        });

        // 手动微调四角 / 切换白平衡后重新矫正（复用会话原图，不用重传，省手机流量）
        g.MapPost("/ocr/rewarp", (JsonElement body, InvTailService s) =>
            Safe(() => Results.Ok(s.OcrRewarp(body))));

        // 取会话处理后的图片（矫正+白平衡结果），H5 画布加载后在其上框选
        g.MapGet("/ocr/image/{sid}", (HttpContext ctx, string sid, InvTailService s) =>
        {
            try { return s.OcrImage(ctx, sid); }
            catch (InvTailException e) { return Results.BadRequest(new { error = e.Message }); }
        });

        // 第二步：只识别框选区域（rois 多个 → 按多条信息分别返回）
        g.MapPost("/ocr/recognize", (JsonElement body, InvTailService s) =>
            Safe(() => Results.Ok(s.OcrRecognize(body))));

        // ---------- 导出 ----------

        // 导出出入库明细流水 xlsx（一行一笔：库号/编码/材料信息/方向/数量/结存/时间/经手人）
        g.MapGet("/export", ([FromQuery] string? q, [FromQuery] string? handler, [FromQuery] string? direction,
                             [FromQuery] string? dateFrom, [FromQuery] string? dateTo, InvTailService s) =>
            Safe(() => s.Export(q, handler, direction, dateFrom, dateTo)));
    }

    // ---------- 请求解析 ----------

    private static InvCreateRequest ReadCreate(JsonElement body) => new()
    {
        BinNo = Str(body, "binNo"),
        Code = Str(body, "code"),
        MaterialInfo = Str(body, "materialInfo"),
        Qty = Num(body, "qty"),
        Handler = Str(body, "handler"),
        Note = Str(body, "note")
    };

    private static string? Str(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return null;
        return v.ValueKind switch
        {
            JsonValueKind.String => v.GetString(),
            JsonValueKind.Number => v.GetRawText(),
            _ => null
        };
    }

    /// <summary>数量解析：兼容前端传数字或字符串（H5 input 常给字符串）。</summary>
    private static double Num(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return 0;
        if (v.ValueKind == JsonValueKind.Number) return v.TryGetDouble(out var d) ? d : 0;
        if (v.ValueKind == JsonValueKind.String)
            return double.TryParse((v.GetString() ?? "").Trim(), out var d2) ? d2 : 0;
        return 0;
    }

    /// <summary>布尔解析：兼容 true/false、字符串、数字（H5 fetch 偶尔把 bool 序列化成字符串）。</summary>
    private static bool Bool(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return false;
        return v.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.String => bool.TryParse((v.GetString() ?? "").Trim(), out var b) && b,
            JsonValueKind.Number => v.TryGetDouble(out var d) && d != 0,
            _ => false
        };
    }

    /// <summary>
    /// 统一错误出口：业务异常转 400 且带机器可读 code，客户端永远看不到堆栈或内部细节。
    /// duplicate 携带 existing（前端可一键跳转该材料做出入库）；shortage 携带当前库存。
    /// </summary>
    private static IResult Safe(Func<IResult> act)
    {
        try { return act(); }
        catch (InvDuplicateException e)
        {
            return Results.BadRequest(new
            {
                error = e.Message,
                code = "duplicate",
                field = e.Field,
                existing = new
                {
                    id = e.Existing.Id,
                    binNo = e.Existing.BinNo,
                    code = e.Existing.Code,
                    materialInfo = e.Existing.MaterialInfo,
                    stock = InvNum.Fmt(e.Existing.Stock)
                }
            });
        }
        catch (InvStockShortageException e)
        {
            return Results.BadRequest(new
            {
                error = e.Message,
                code = "shortage",
                stock = InvNum.Fmt(e.Stock),
                requested = InvNum.Fmt(e.Requested)
            });
        }
        catch (InvTailException e)
        {
            return Results.BadRequest(new { error = e.Message, code = "business" });
        }
        catch (Exception e)
        {
            return Results.BadRequest(new { error = "服务异常：" + e.Message, code = "internal" });
        }
    }
}
