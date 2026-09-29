using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Configuration;
using Platform.Infrastructure;

namespace Platform.Modules.Unbind;

/// <summary>
/// 二维码服务端强解码兜底。
/// 当 H5 端所有纯 JS 解码库（BarcodeDetector / jsQR / ZXing / Quagga）都失败时，
/// 把图像（base64 dataURL 或上传文件）发给本机 Python(zxing-cpp) 做最后兜底。
///
/// 专门应对反光 / 低对比度白底二维码——这类场景 zxing-js 浏览器版（只支持
/// Binarizer.LocalAverage）无法定位 finder pattern，而 zxing-cpp 的
/// GlobalHistogram + CLAHE + 多尺度 + 标签 ROI 正方形裁剪能解出。
///
/// 使用平台级 IPythonProcessFactory 的干净 Python 环境（与宿主隔离，UTF-8）。
/// 注意：不再借用 Drawings 模块的 OcrService —— 那属于「业务模块横向依赖业务模块」，
/// 会在 Drawings 模块被替换时导致本模块 DI 解析失败、平台启动崩溃。
/// </summary>
public class QrDecodeService
{
    private readonly string _pythonExe;
    private readonly string _script;
    private readonly IPythonProcessFactory _python;

    public QrDecodeService(IConfiguration config, IPythonProcessFactory python)
    {
        _pythonExe = config["Ocr:PythonExe"]
            ?? @"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe";
        _python = python;
        _script = FindScript("qr_decode.py");
    }

    /// <summary>从 BaseDirectory 向上查找脚本文件（兼容 dotnet run 与发布后路径）。</summary>
    private static string FindScript(string filename)
    {
        var baseDir = AppContext.BaseDirectory;
        var candidates = new[]
        {
            Path.Combine(Directory.GetCurrentDirectory(), "Modules", "Unbind"),
            Directory.GetCurrentDirectory(),
            baseDir
        };
        foreach (var dir in candidates)
        {
            var p = Path.Combine(dir, filename);
            if (File.Exists(p)) return p;
        }
        var current = baseDir;
        for (int i = 0; i < 6; i++)
        {
            var parent = Path.GetDirectoryName(current);
            if (string.IsNullOrEmpty(parent) || parent == current) break;
            var p = Path.Combine(parent, filename);
            if (File.Exists(p)) return p;
            current = parent;
        }
        return Path.Combine(baseDir, filename);
    }

    /// <summary>HTTP 入口：接收 base64 dataURL（JSON {image}）或上传文件（multipart form-data file），
    /// 调 Python 兜底解码，返回 {text, format, method, elapsed_ms} 或 {text:null, error}。</summary>
    public async Task<IResult> DecodeAsync(HttpContext ctx)
    {
        string? inputB64 = null;

        // 1) multipart/form-data（file 字段）
        if (ctx.Request.HasFormContentType)
        {
            var file = ctx.Request.Form.Files["file"];
            if (file != null && file.Length > 0)
            {
                using var ms = new MemoryStream();
                await file.CopyToAsync(ms);
                var b64 = Convert.ToBase64String(ms.ToArray());
                var ct = file.ContentType ?? "image/jpeg";
                inputB64 = $"data:{ct};base64,{b64}";
            }
        }
        // 2) application/json（{image: dataURL}）
        if (string.IsNullOrWhiteSpace(inputB64))
        {
            try
            {
                using var reader = new StreamReader(ctx.Request.Body);
                var body = await reader.ReadToEndAsync();
                if (!string.IsNullOrWhiteSpace(body))
                {
                    using var doc = JsonDocument.Parse(body);
                    if (doc.RootElement.TryGetProperty("image", out var imgEl) &&
                        imgEl.ValueKind == JsonValueKind.String)
                    {
                        inputB64 = imgEl.GetString();
                    }
                }
            }
            catch { }
        }

        if (string.IsNullOrWhiteSpace(inputB64))
            return Results.Json(new { text = (string?)null, error = "empty image" },
                statusCode: 400);

        if (!File.Exists(_script))
            return Results.Json(new { text = (string?)null, error = "qr_decode.py not found" },
                statusCode: 500);

        // 3) 调 Python 子进程：stdin 喂 JSON，stdout 收结果
        var psi = _python.Create(_script, Array.Empty<string>());
        psi.RedirectStandardInput = true;   // qr_decode.py 从 stdin 读
        try
        {
            using var p = Process.Start(psi)!;
            var stdin = p.StandardInput;
            await stdin.WriteAsync(JsonSerializer.Serialize(new { image = inputB64 }));
            await stdin.FlushAsync();
            stdin.Close();   // 关闭 stdin → 通知 Python EOF

            var outTask = p.StandardOutput.ReadToEndAsync();
            var errTask = p.StandardError.ReadToEndAsync();
            var exited = p.WaitForExit(15_000);
            if (!exited) { try { p.Kill(); } catch { } }
            await Task.WhenAll(outTask, errTask);

            var stdout = (outTask.Result ?? "").Trim();
            if (string.IsNullOrWhiteSpace(stdout))
            {
                var err = (errTask.Result ?? "").Trim();
                return Results.Json(new { text = (string?)null, error = "no output from decoder", detail = err });
            }

            using var doc = JsonDocument.Parse(stdout);
            var root = doc.RootElement;
            if (root.TryGetProperty("text", out var t) && t.ValueKind != JsonValueKind.Null)
            {
                var text = t.GetString();
                if (!string.IsNullOrEmpty(text))
                {
                    return Results.Json(new
                    {
                        text,
                        format = root.TryGetProperty("format", out var f) ? f.GetString() : "qr_code",
                        method = root.TryGetProperty("method", out var m) ? m.GetString() : null,
                        elapsed_ms = root.TryGetProperty("elapsed_ms", out var e) ? e.GetInt32() : (int?)null
                    });
                }
            }
            var errMsg = root.TryGetProperty("error", out var er) && er.ValueKind == JsonValueKind.String
                ? er.GetString() : "decode failed";
            return Results.Json(new { text = (string?)null, error = errMsg });
        }
        catch (Exception ex)
        {
            return Results.Json(new { text = (string?)null, error = "decode exception: " + ex.Message },
                statusCode: 500);
        }
    }
}
