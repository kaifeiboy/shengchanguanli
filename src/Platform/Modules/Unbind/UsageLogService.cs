using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading;
using Microsoft.AspNetCore.Http;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Configuration;
using Platform.Infrastructure;

namespace Platform.Modules.Unbind;

/// <summary>
/// 使用记录服务端存储。
/// 改用服务端 SQLite 持久化，避免手机浏览器 localStorage 被 WebView / 隐私模式 / 存储回收
/// 静默清空导致“记录很快消失”。前端只负责读写本服务，不再依赖浏览器本地存储。
/// </summary>
public class UsageLogService
{
    private readonly DbContext _db;
    private readonly int _max;
    private readonly SemaphoreSlim _gate = new(1, 1);

    public UsageLogService(DbContext db, IConfiguration cfg)
    {
        _db = db;
        int.TryParse(cfg["UsageLog:Max"], out var m);
        _max = m > 0 ? m : 30;
    }

    /// <summary>读取最近 N 条（最新在前），返回原始 JSON 元素，保留 params 等嵌套结构。</summary>
    public async Task<List<JsonElement>> ListAsync()
    {
        var list = new List<JsonElement>();
        await using var conn = _db.Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT Payload FROM usage_log ORDER BY Id DESC LIMIT @max";
        cmd.Parameters.Add(new SqliteParameter("@max", _max));
        using var r = await cmd.ExecuteReaderAsync();
        while (await r.ReadAsync())
        {
            var raw = r.IsDBNull(0) ? null : r.GetString(0);
            if (string.IsNullOrWhiteSpace(raw)) continue;
            try
            {
                using var doc = JsonDocument.Parse(raw);
                list.Add(doc.RootElement.Clone());
            }
            catch { /* 单条损坏不影响整体 */ }
        }
        return list;
    }

    /// <summary>以 UTF-8（坏字节替换为 U+FFFD）方式读取请求体，避免个别非法字节导致写入崩溃。</summary>
    public static async Task<string> ReadRawBodyAsync(HttpRequest req)
    {
        using var reader = new StreamReader(req.Body, new UTF8Encoding(false, false), leaveOpen: false);
        return await reader.ReadToEndAsync();
    }

    /// <summary>追加一条记录（前端POST的原始JSON字符串），超出上限时删除最旧部分。</summary>
    public async Task AppendAsync(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return;
        // 容错：非法字节已在读取时替换；此处仅校验是否为合法 JSON
        try { using var _ = JsonDocument.Parse(raw); }
        catch { return; }
        await _gate.WaitAsync();
        try
        {
            await using var conn = _db.Open();
            using var tx = conn.BeginTransaction();
            using (var ins = conn.CreateCommand())
            {
                ins.Transaction = tx;
                ins.CommandText = "INSERT INTO usage_log (CreatedAt, Payload) VALUES (@created, @payload)";
                ins.Parameters.Add(new SqliteParameter("@created", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss")));
                ins.Parameters.Add(new SqliteParameter("@payload", raw));
                await ins.ExecuteNonQueryAsync();
            }
            // 超过上限则删除最旧记录（COUNT - max 行）
            using (var del = conn.CreateCommand())
            {
                del.Transaction = tx;
                del.CommandText = @"
                    DELETE FROM usage_log
                    WHERE Id IN (
                        SELECT Id FROM usage_log ORDER BY Id ASC
                        LIMIT MAX(0, (SELECT COUNT(*) FROM usage_log) - @max)
                    )";
                del.Parameters.Add(new SqliteParameter("@max", _max));
                await del.ExecuteNonQueryAsync();
            }
            await tx.CommitAsync();
        }
        finally { _gate.Release(); }
    }

    /// <summary>清空全部使用记录。</summary>
    public async Task ClearAsync()
    {
        await _gate.WaitAsync();
        try
        {
            await using var conn = _db.Open();
            using var cmd = conn.CreateCommand();
            cmd.CommandText = "DELETE FROM usage_log";
            await cmd.ExecuteNonQueryAsync();
        }
        finally { _gate.Release(); }
    }
}
