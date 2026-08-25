using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.Extensions.Options;

namespace Platform.Modules.Unbind;

/// <summary>
/// 维修解绑业务服务。
/// 所有请求只做透明转发：H5 -> .NET -> Node.js 中转服务 -> 目标系统，
/// 不在后端做字段假定，方便后续根据实际接口调整中转层配置。
/// </summary>
public class UnbindService
{
    private readonly HttpClient _http;
    private readonly UnbindConfig _cfg;
    private readonly ILogger<UnbindService> _log;

    public UnbindService(HttpClient http, IOptions<UnbindConfig> cfg, ILogger<UnbindService> log)
    {
        _http = http;
        _cfg = cfg.Value;
        _log = log;
    }

    public async Task<object> SearchAsync(JsonElement body)
    {
        return await ProxyAsync("/api/unbind/search", body);
    }

    public async Task<object> OuterUnbindAsync(JsonElement body)
    {
        return await ProxyAsync("/api/unbind/outer", body);
    }

    public async Task<object> RepairUnbindAsync(JsonElement body)
    {
        return await ProxyAsync("/api/unbind/repair", body);
    }

    public async Task<object> AuthSetupAsync(JsonElement body)
    {
        var url = (_cfg.ProxyBaseUrl?.TrimEnd('/') ?? "") + "/_auth/setup";
        try
        {
            using var resp = await _http.PostAsJsonAsync(url, body);
            var raw = await resp.Content.ReadAsStringAsync();
            if (!resp.IsSuccessStatusCode)
                return Results.Problem(title: "中转服务拒绝录入", detail: raw, statusCode: (int)resp.StatusCode);
            using var doc = JsonDocument.Parse(raw);
            return doc.RootElement.Clone();
        }
        catch (Exception ex)
        {
            return Results.Problem(title: "无法连接中转服务", detail: ex.Message, statusCode: 502);
        }
    }

    public async Task<object> AuthStatusAsync()
    {
        var url = (_cfg.ProxyBaseUrl?.TrimEnd('/') ?? "") + "/_auth/status";
        try
        {
            using var resp = await _http.GetAsync(url);
            var raw = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(raw);
            return doc.RootElement.Clone();
        }
        catch (Exception ex)
        {
            return Results.Problem(title: "无法连接中转服务", detail: ex.Message, statusCode: 502);
        }
    }

    public async Task<object> AuthRefreshAsync()
    {
        var url = (_cfg.ProxyBaseUrl?.TrimEnd('/') ?? "") + "/_auth/refresh";
        try
        {
            using var resp = await _http.PostAsync(url, null);
            var raw = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(raw);
            return doc.RootElement.Clone();
        }
        catch (Exception ex)
        {
            return Results.Problem(title: "无法连接中转服务", detail: ex.Message, statusCode: 502);
        }
    }

    private async Task<object> ProxyAsync(string path, JsonElement body)
    {
        var url = (_cfg.ProxyBaseUrl?.TrimEnd('/') ?? "") + path;
        try
        {
            using var resp = await _http.PostAsJsonAsync(url, body, new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.CamelCase
            });
            var raw = await resp.Content.ReadAsStringAsync();
            _log.LogDebug("Proxy {Url} -> {Status}, body={Body}", url, (int)resp.StatusCode, raw[..Math.Min(raw.Length, 200)]);

            if (!resp.IsSuccessStatusCode)
            {
                return Results.Problem(
                    title: "中转服务返回错误",
                    detail: $"{(int)resp.StatusCode}: {raw}",
                    statusCode: (int)resp.StatusCode);
            }

            // 透传 JSON，保持目标系统原始响应结构
            using var doc = JsonDocument.Parse(raw);
            return doc.RootElement.Clone();
        }
        catch (HttpRequestException ex)
        {
            _log.LogError(ex, "无法连接中转服务 {Url}", url);
            return Results.Problem(
                title: "无法连接中转服务",
                detail: $"请确认 Node.js 中转服务已启动，当前配置地址：{_cfg.ProxyBaseUrl}。错误：{ex.Message}",
                statusCode: 502);
        }
        catch (Exception ex)
        {
            _log.LogError(ex, "转发请求到中转服务失败 {Url}", url);
            return Results.Problem(
                title: "转发请求失败",
                detail: ex.Message,
                statusCode: 500);
        }
    }
}
