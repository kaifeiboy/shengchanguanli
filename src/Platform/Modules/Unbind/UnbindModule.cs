using System.Text.Json;
using Platform.Core;

namespace Platform.Modules.Unbind;

/// <summary>
/// 维修解绑（解绑管理）模块。
/// 手机 H5 通过本模块访问目标内网系统的解绑功能；
/// 认证信息由本地 Node.js 中转服务透明透传，手机端无需二次登录。
/// </summary>
public class UnbindModule : IModule
{
    public string Key => "unbind";
    public string Name => "维修解绑";
    public string Icon => "🔧";
    public int Order => 20;

    public void RegisterServices(IServiceCollection services)
    {
        services.AddSingleton<UnbindService>();
        services.AddHttpClient<UnbindService>();
        services.AddSingleton<UsageLogService>();
        services.AddSingleton<QrDecodeService>();
    }

    public void MapEndpoints(WebApplication app)
    {
        var g = app.MapGroup("/api/unbind");

        // 查询解绑记录
        g.MapPost("/search", (JsonElement body, UnbindService s) => s.SearchAsync(body));

        // 外箱解绑
        g.MapPost("/outer", (JsonElement body, UnbindService s) => s.OuterUnbindAsync(body));

        // 维修解绑
        g.MapPost("/repair", (JsonElement body, UnbindService s) => s.RepairUnbindAsync(body));

        // 解绑账号：录入/状态/手动刷新（加密保存 + 自动续期，由 Node 中转实现）
        g.MapPost("/auth-setup", (JsonElement body, UnbindService s) => s.AuthSetupAsync(body));
        g.MapGet("/auth-status", (UnbindService s) => s.AuthStatusAsync());
        g.MapPost("/auth-refresh", (UnbindService s) => s.AuthRefreshAsync());

        // 使用记录（服务端持久化，避免手机浏览器 localStorage 被静默清空）
        g.MapGet("/usage-log", (UsageLogService s) => s.ListAsync());
        g.MapPost("/usage-log", async (HttpContext ctx, UsageLogService s) =>
        {
            var raw = await UsageLogService.ReadRawBodyAsync(ctx.Request);
            await s.AppendAsync(raw);
        });
        g.MapDelete("/usage-log", (UsageLogService s) => s.ClearAsync());

        // 二维码服务端强解码兜底：H5 纯 JS 库全失败时调本机 Python(zxing-cpp) 最后兜底
        g.MapPost("/qr-decode", async (HttpContext ctx, QrDecodeService s) => await s.DecodeAsync(ctx));
    }
}
