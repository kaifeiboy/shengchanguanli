using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Platform.Core;
using Platform.Infrastructure;
using System.Threading.Tasks;

namespace Platform.Modules.Drawings;

/// <summary>
/// 打标首件对比模块（平台第一个业务模块）。
/// 实现 IModule，注册即生效，平台导航自动出现，平台核心零改动。
/// 路由前缀：/api/drawings/... 与 /api/files/...（文件下载，复用了本地文件访问层）。
///
/// 交互模型（索引式）：
///  1) GET  /api/drawings            全部型号索引
///  2) GET  /api/drawings/search?q= 模糊搜索型号
///  3) GET  /api/drawings/detail/{id} 图纸详情 + 部位片段
///  4) POST /api/drawings/recognize-part  上传效果图照片 -> 自动识别该图纸的部位
/// </summary>
public class DrawingModule : IModule
{
    public string Key => "drawings";
    public string Name => "打标首件对比";
    public string Icon => "🏷️";
    public int Order => 10;

    public void RegisterServices(IServiceCollection services)
    {
        // P8：OcrService 已上移到平台层注册（Program.cs）。
        // 业务模块不再代持被其它模块依赖的共享服务，避免模块替换引发平台启动崩溃。
        services.AddSingleton<DrawingService>();
    }

    public void MapEndpoints(WebApplication app)
    {
        var g = app.MapGroup("/api/drawings");

        // 初始化 / 重建图纸库（仅文件名提取型号，快）
        g.MapPost("/init", (DrawingService s) => Results.Ok(new { inserted = s.InitLibrary() }));

        // 全部型号索引（首屏列表）
        g.MapGet("/", (DrawingService s) => Results.Ok(s.ListAll()));

        // 模糊搜索型号
        g.MapGet("/search", ([FromQuery] string q, DrawingService s) => Results.Ok(s.Search(q)));

        // 图纸详情 + 部位片段
        g.MapGet("/detail/{id:int}", (int id, DrawingService s) =>
        {
            var d = s.GetDetail(id);
            return d is null ? Results.NotFound() : Results.Ok(d);
        });

        // 部位自动识别：上传效果图照片 -> 与所选图纸内容匹配 -> 识别部位
        // 直接读取 HttpContext.Form，避免 minimal API 因 IFormFile 自动注入 anti-forgy 校验。
        g.MapPost("/recognize-part", (HttpContext ctx, DrawingService s) =>
        {
            if (!ctx.Request.Form.TryGetValue("drawingId", out var idVal) ||
                !int.TryParse(idVal.ToString(), out var drawingId))
                return Results.BadRequest(new { error = "缺少 drawingId" });

            var file = ctx.Request.Form.Files["file"];
            if (file is null || file.Length == 0)
                return Results.BadRequest(new { error = "未收到图片文件" });

            return Results.Ok(s.RecognizePart(drawingId, file));
        });

        // 历史记录
        g.MapGet("/history", ([FromQuery] int limit, DrawingService s) => Results.Ok(s.GetHistory(limit > 0 ? limit : 50)));

        // 历史记录标示照片快照（任务3：按 history 行 SnapshotPath 返回，不依赖图纸库 /api/files）
        g.MapGet("/history-snapshot/{id:int}", (int id, DrawingService s) => s.ServeHistorySnapshot(id));

        // 部位裁剪图（图形匹配信号用，返回 PNG 图片）
        g.MapGet("/part-image", (HttpContext ctx, DrawingService s) =>
        {
            var q = ctx.Request.Query;
            if (!q.TryGetValue("id", out var idVal) || !int.TryParse(idVal, out var did))
                return Results.BadRequest(new { error = "缺少 id" });
            if (!q.TryGetValue("part", out var partNameRaw) || string.IsNullOrEmpty(partNameRaw))
                return Results.BadRequest(new { error = "缺少 part" });
            return s.ServePartImage(did, partNameRaw!);
        });

        // 新版（M2）：照片 → 与「已选图纸」切割出的小图块比内容 → 返回命中小图
        g.MapPost("/match-block", (HttpContext ctx, DrawingService s) =>
        {
            if (!ctx.Request.Form.TryGetValue("drawingId", out var idVal) ||
                !int.TryParse(idVal.ToString(), out var drawingId))
                return Results.BadRequest(new { error = "缺少 drawingId" });

            var file = ctx.Request.Form.Files["file"];
            if (file is null || file.Length == 0)
                return Results.BadRequest(new { error = "未收到图片文件" });

            return Results.Ok(s.MatchBlock(drawingId, file));
        });

        // ⭐ 合并端点（任务2）：一次请求完成「块匹配 + 照片标示 + 快照入库」，返回 {historyId, photoDiffUrl, 匹配结论}。
        //    H5 最终呈现不再串行两次请求；旧 /match-block + /diff-photo 保留为回滚通道。
        g.MapPost("/match-and-photo", (HttpContext ctx, DrawingService s) =>
        {
            if (!ctx.Request.Form.TryGetValue("drawingId", out var idVal) ||
                !int.TryParse(idVal.ToString(), out var drawingId))
                return Results.BadRequest(new { error = "缺少 drawingId" });

            var file = ctx.Request.Form.Files["file"];
            if (file is null || file.Length == 0)
                return Results.BadRequest(new { error = "未收到图片文件" });

            return Results.Ok(s.MatchAndPhoto(drawingId, file));
        });

        // 命中小图块原图（原图格式呈现）
        g.MapGet("/block-image", (HttpContext ctx, DrawingService s) =>
        {
            var q = ctx.Request.Query;
            if (!q.TryGetValue("id", out var idVal) || !int.TryParse(idVal, out var did))
                return Results.BadRequest(new { error = "缺少 id" });
            if (!q.TryGetValue("idx", out var idxVal) || !int.TryParse(idxVal, out var idx))
                return Results.BadRequest(new { error = "缺少 idx" });
            return s.ServeBlockImage(did, idx);
        });

        // 匹配用的用户产品照片（H5 兜底显示，不依赖 diff）
        g.MapGet("/match-photo", (HttpContext ctx, DrawingService s) =>
        {
            var q = ctx.Request.Query;
            if (!q.TryGetValue("id", out var idVal) || !int.TryParse(idVal, out var did))
                return Results.BadRequest(new { error = "缺少 id" });
            var md5 = q.TryGetValue("md5", out var mVal) ? mVal.ToString() : "";
            return s.ServeMatchPhoto(did, md5);
        });

        // 完整图纸原图（"查看完整图纸"功能，不破坏原 PDF）
        g.MapGet("/full-image", (HttpContext ctx, DrawingService s) =>
        {
            var q = ctx.Request.Query;
            if (!q.TryGetValue("id", out var idVal) || !int.TryParse(idVal, out var did))
                return Results.BadRequest(new { error = "缺少 id" });
            return s.ServeFullImage(did);
        });

        // 差异标记 v1：照片 vs 命中图块 → OCR-bbox 文本级 diff + 标记图
        g.MapPost("/mark-diff", (HttpContext ctx, DrawingService s) =>
        {
            if (!ctx.Request.Form.TryGetValue("drawingId", out var didVal) || !int.TryParse(didVal, out var did))
                return Results.BadRequest(new { error = "缺少 drawingId" });
            if (!ctx.Request.Form.TryGetValue("blockIdx", out var idxVal) || !int.TryParse(idxVal, out var idx))
                return Results.BadRequest(new { error = "缺少 blockIdx" });
            var file = ctx.Request.Form.Files["file"];
            if (file is null || file.Length == 0)
                return Results.BadRequest(new { error = "未收到图片文件" });
            return Results.Ok(s.MarkDifferences(did, idx, file));
        });

        // 差异标记图服务
        g.MapGet("/diff-image", (HttpContext ctx, DrawingService s) =>
        {
            var q = ctx.Request.Query;
            if (!q.TryGetValue("did", out var didVal) || !int.TryParse(didVal, out var did))
                return Results.BadRequest(new { error = "缺少 did" });
            if (!q.TryGetValue("file", out var fnVal))
                return Results.BadRequest(new { error = "缺少 file" });
            return s.ServeDiffImage(did, fnVal.ToString());
        });

        // 差异标记 v2：照片直接标示（不改动 mark-diff / diff-image 等现有端点）
        g.MapPost("/diff-photo", (HttpContext ctx, DrawingService s) =>
        {
            if (!ctx.Request.Form.TryGetValue("drawingId", out var didVal) || !int.TryParse(didVal, out var did))
                return Results.BadRequest(new { error = "缺少 drawingId" });
            var file = ctx.Request.Form.Files["file"];
            if (file is null || file.Length == 0)
                return Results.BadRequest(new { error = "未收到图片文件" });
            return Results.Ok(s.MarkDifferencesOnPhoto(did, file));
        });

        // 照片直接标示图服务（JPEG，v2）
        g.MapGet("/diff-photo-image", (HttpContext ctx, DrawingService s) =>
        {
            var q = ctx.Request.Query;
            if (!q.TryGetValue("did", out var didVal) || !int.TryParse(didVal, out var did))
                return Results.BadRequest(new { error = "缺少 did" });
            if (!q.TryGetValue("file", out var fnVal))
                return Results.BadRequest(new { error = "缺少 file" });
            return s.ServePhotoDiffImage(did, fnVal.ToString());
        });

        // 重切单张 / 全量（任务42/43）：查询参数 ?drawingId=112&force=true
        // 指定 drawingId -> 同步切该张；省略 -> 后台批量切全部（避免 HTTP 超时）。
        g.MapPost("/segment", (HttpContext ctx, DrawingService s) =>
        {
            int? id = null; bool force = false;
            var q = ctx.Request.Query;
            if (q.TryGetValue("drawingId", out var idVal) && int.TryParse(idVal, out var pid))
                id = pid;
            if (q.TryGetValue("force", out var fVal) &&
                (fVal.ToString().Equals("true", StringComparison.OrdinalIgnoreCase)
                 || fVal.ToString() == "1"))
                force = true;

            if (id.HasValue)
            {
                s.EnsureSegmented(id.Value, force);
                return Results.Ok(new { done = true, drawingId = id.Value, force });
            }
            // 全量：后台跑，立即返回，避免 21 张切割阻塞请求
            _ = Task.Run(() => { try { s.SegmentAll(force); } catch { } });
            return Results.Ok(new { started = true, message = "批量切割已在后台启动", force });
        });

        // 方案 B 兜底：源 PDF 缺失时，对已有切块重过滤（只留打标区）。
        g.MapPost("/refilter", (HttpContext ctx, DrawingService s) =>
        {
            int? id = null; bool force = false;
            var q = ctx.Request.Query;
            if (q.TryGetValue("drawingId", out var idVal) && int.TryParse(idVal, out var pid))
                id = pid;
            if (q.TryGetValue("force", out var fVal) &&
                (fVal.ToString().Equals("true", StringComparison.OrdinalIgnoreCase)
                 || fVal.ToString() == "1"))
                force = true;

            if (id.HasValue)
            {
                s.RefilterDrawing(id.Value);
                return Results.Ok(new { done = true, drawingId = id.Value, force });
            }
            // 全量：后台跑，立即返回，避免 21 张重过滤阻塞请求
            _ = Task.Run(() => { try { s.RefilterAll(force); } catch { } });
            return Results.Ok(new { started = true, message = "批量重过滤已在后台启动", force });
        });

        // 预热：把所有图纸块载入内存缓存 + 预热 OCR worker + Python 编译检查
        g.MapPost("/warmup", (DrawingService s) =>
        {
            s.WarmupCache();
            return Results.Ok(new { warmed = true });
        });

        // 健康检查：API 端点 + Python 脚本编译检查
        g.MapGet("/health", (DrawingService s) =>
        {
            var py = s.PythonHealthCheck();
            return Results.Ok(new { status = "ok", python = new { ok = py.Ok, message = py.Message } });
        });

        // 新增：单张添加图纸（与固有图纸同处理：仅入库，懒切割）
        g.MapPost("/add", (HttpContext ctx, DrawingService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data 上传图纸" });

            var file = ctx.Request.Form.Files["file"];
            var model = ctx.Request.Form["model"].ToString();
            var r = s.AddDrawing(file, string.IsNullOrWhiteSpace(model) ? null : model);
            return r.Error is null ? Results.Ok(r) : Results.BadRequest(new { error = r.Error });
        });
        // 新增：批量添加图纸（多文件，都是工程图纸 PDF）
        g.MapPost("/add-batch", (HttpContext ctx, DrawingService s) =>
        {
            if (!ctx.Request.HasFormContentType)
                return Results.BadRequest(new { error = "请使用 multipart/form-data 上传图纸" });

            var files = ctx.Request.Form.Files;
            var model = ctx.Request.Form["model"].ToString();
            var results = s.AddDrawingsBatch(files, string.IsNullOrWhiteSpace(model) ? null : model);
            return Results.Ok(new { count = results.Count, results });
        });

        // 新增：同步图纸存放文件夹（把直接丢进文件夹的 PDF 等同于新增图纸）
        g.MapPost("/sync", (DrawingService s) =>
        {
            var r = s.SyncFolder();
            return Results.Ok(new
            {
                added = r.Added,
                skipped = r.Skipped,
                items = r.AddedItems.Select(x => new
                {
                    id = x.Id,
                    fileName = x.FileName,
                    virtualPath = x.VirtualPath,
                    model = x.Model,
                    error = x.Error
                })
            });
        });

        // P7：/api/files 已上移到平台层（Program.cs），此处不再映射，避免重复路由。
    }
}
