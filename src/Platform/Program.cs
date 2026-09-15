using System.IO;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Platform.Core;
using Platform.Infrastructure;
using Platform.Modules.Drawings;
using Platform.Modules.DrawingsV2;
using Platform.Modules.Unbind;
using Platform.Modules.DefectHistory;
using Platform.Modules.InvTail;

var builder = WebApplication.CreateBuilder(args);

// ---- Platform configuration ----
// 端口在下方 ConfigureKestrel 显式写死（5000 http + 5443 https）。
// 注意：本机会话可能注入环境变量 SERVER__PORT / ASPNETCORE_URLS（双下划线=配置节），
// 会污染端口，因此这里不读配置、且用 ConfigureKestrel 显式 Listen 覆盖之。

// ---- Core / shared services (platform base) ----
builder.Services.AddSingleton<NetworkResolver>();
builder.Services.AddSingleton<FileAccessService>();
builder.Services.AddSingleton<DbContext>();
// 平台级 Python 进程基础设施：任何模块都应依赖它，而不是横向借用其它模块的 OcrService。
builder.Services.AddSingleton<IPythonProcessFactory, PythonProcessFactory>();

// P8：OcrService 注册从 Drawings 模块上移到平台层。
// 原因：Unbind / DefectHistory 曾通过 DI 依赖它，而它注册在业务模块内 ——
// 一旦 Drawings 模块被替换或未注册，这两个模块的 DI 解析会失败、平台启动崩溃。
// 上移后，模块替换不再影响其它模块。（OcrService 自身仍属历史实现，已封存。）
builder.Services.AddSingleton<OcrService>();

// ---- Module registry: register all modules here ----
var registry = new ModuleRegistry();
registry.Register(new DrawingModule());
// v2（M3）：以 Key="drawingsv2" 与旧 drawings 模块**并存**注册。
// 只新增 /api/drawingsv2/* 端点，不改动、不删除旧链路，可并行验证、随时可回退。
// 待 v2 端点覆盖 H5 调用面后（M5）再切换入口，届时用户可见的 URL 与入口不变。
registry.Register(new DrawingsV2Module());
registry.Register(new UnbindModule());
registry.Register(new DefectHistoryModule());
registry.Register(new InvTailModule());
registry.RegisterServices(builder.Services);

// 维修解绑模块配置：中转服务地址
builder.Services.Configure<UnbindConfig>(builder.Configuration.GetSection("Unbind"));

// ---- Dev tooling ----
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();

// 端口绑定（必须在 Build() 之前）：
// - http://0.0.0.0:5000 保留给所有现有模块（拍照比对/图纸库/维修解绑等）
// - https://0.0.0.0:5443 新增，用于内网实时扫码（http 下浏览器禁用摄像头）
// 使用 ConfigureKestrel 显式 Listen 后，Kestrel 会忽略 app.Urls 与 ASPNETCORE_URLS
// 环境变量（如被钉死的 55718），两端口统一在此声明。
// 证书为自签，安卓访问 https 时点“继续访问”即可。
var certPath = @"E:\workaaa\shengchanguanli\certs\localhost.pfx";
var certPass = "chg2026!";
builder.WebHost.ConfigureKestrel(opts =>
{
    opts.ListenAnyIP(5000);
    opts.ListenAnyIP(5443, lo => lo.UseHttps(certPath, certPass));
});

var app = builder.Build();

// 端口已在上方 ConfigureKestrel 显式 Listen（5000 http + 5443 https），
// 此处无需再设置 app.Urls（显式 Listen 优先级高于 app.Urls / 环境变量）。

// Ensure SQLite schema exists
app.Services.GetRequiredService<DbContext>().Initialize();

// Map every module's endpoints
registry.MapEndpoints(app);

// Serve the mobile H5 (same-origin -> works on LAN and tunnel automatically)
app.UseDefaultFiles();
app.UseStaticFiles(new Microsoft.AspNetCore.Builder.StaticFileOptions
{
    // ⭐ H5 是单文件 SPA，手机浏览器（尤其微信内置）会顽固缓存 index.html，导致后端修复后前端仍显示旧逻辑
    // 这里强制所有 wwwroot 静态资源走 no-cache，确保 H5 每次都拿最新
    OnPrepareResponse = ctx =>
    {
        ctx.Context.Response.Headers["Cache-Control"] = "no-cache, no-store, must-revalidate";
        ctx.Context.Response.Headers["Pragma"] = "no-cache";
        ctx.Context.Response.Headers["Expires"] = "0";
    }
});

// v2 照片静态服务：把 PhotoDir 映射到 /drawingsv2-photos，供 H5 回显已上传照片
{
    var photoDir = builder.Configuration["DrawingsV2:PhotoDir"];
    if (string.IsNullOrWhiteSpace(photoDir))
    {
        var appDb = builder.Configuration["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
        photoDir = Path.Combine(Path.GetDirectoryName(appDb) ?? ".", "drawingsv2_photos");
    }
    if (Directory.Exists(photoDir))
    {
        app.UseStaticFiles(new Microsoft.AspNetCore.Builder.StaticFileOptions
        {
            FileProvider = new Microsoft.Extensions.FileProviders.PhysicalFileProvider(photoDir),
            RequestPath = "/drawingsv2-photos"
        });
    }
}

if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

// ---- Platform-level endpoints ----
app.MapGet("/api/modules", () => registry.ListMeta());

// P7：文件下载端点上移到平台层。
// 该端点使用平台 FileAccessService、且为平台级命名（/api/files），此前却由 Drawings
// 模块代持。上移后平台独立拥有它，Drawings 模块被替换也不会影响文件下载能力。
// 已核实调用方：H5 图纸详情页与历史记录（均属 drawings 页面），URL 与行为完全不变。
app.MapGet("/api/files/{*vpath}", (string vpath, FileAccessService fs) =>
{
    var phys = fs.ResolvePhysical(vpath);
    if (phys == null || !File.Exists(phys)) return Results.NotFound();
    return Results.File(phys, "application/pdf", Path.GetFileName(phys));
});

app.MapGet("/api/network/info", (NetworkResolver n) =>
{
    var ext = builder.Configuration["External:BaseUrl"];
    if (string.IsNullOrWhiteSpace(ext))
    {
        var f = @"E:\workaaa\shengchanguanli\data\tunnel_url.txt";
        if (File.Exists(f)) ext = File.ReadAllText(f).Trim();
    }
    return Results.Ok(n.GetInfo("5000", ext));
});

// L1 引擎锁定：启动即后台预热 OCR worker（拉起常驻进程 + 引擎 warm-up），
// 使首拍匹配跳过冷启动窗口，从根上消除「忽好忽坏」。后台执行，不阻塞 Kestrel 启动。
_ = Task.Run(() =>
{
    try { app.Services.GetRequiredService<DrawingService>().WarmupCache(); }
    catch { }
});

app.Run();
