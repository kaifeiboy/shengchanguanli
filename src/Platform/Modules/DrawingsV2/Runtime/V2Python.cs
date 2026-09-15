using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using Platform.Infrastructure;
using Platform.Modules.DrawingsV2.Decision;

namespace Platform.Modules.DrawingsV2.Runtime;

/// <summary>
/// v2 的 Python 桥接层：C# 只负责「起进程、拿 JSON、排空噪声」，不做任何图像/语义处理。
///
/// <para>【复用的平台设施】<see cref="IPythonProcessFactory"/>（P8 已下沉到平台层）。
/// 合规说明：该设施不含任何 OCR/图纸业务语义，只提供「构造与宿主环境隔离的干净 Python 进程」，
/// 是真正的平台能力；v2 依赖它即「模块依赖平台」，不构成模块间横向耦合。</para>
///
/// <para>【★★ 噪声防御：不能假设噪声走哪个流】
/// M1 曾记录「Ink 批注噪声走 fd 2（stderr）」，并据此要求 C# 排空 stderr。
/// M3 真实端点验证时**推翻了这条记录**：实测 `02-PC-P1HJQ` 在 `--json-only` 下
///   stdout 开头 = `cannot set rect: code=4: Ink annotations have no Rect property`（108256 B）
///   stderr      = 0 字节
/// 也就是说 MuPDF 的 C 层输出在本环境落在 **fd 1**，且排在 JSON **之前**，
/// 直接 `json.loads(stdout)` 会报 `'c' is an invalid start of a value`。
///
/// 结论：**stdout 与 stderr 都要异步排空**（哪个流被写、写多少都不确定，
/// 不排空就会在管道缓冲区写满时死锁）；并且**不能假定 stdout 是纯 JSON**，
/// 必须从 stdout 中切出 JSON 片段（首个 `{` 到最后一个 `}`）。
/// 这两条一起做，才对「噪声走哪个流」这一不确定事实具备免疫力。</para>
/// </summary>
public sealed class V2Python
{
    private readonly IPythonProcessFactory _factory;
    private readonly int _timeoutMs;
    private readonly int _photoTimeoutMs;
    private readonly ILogger<V2Python> _logger;

    public string ScriptPath { get; }
    public string ObserveScriptPath { get; }
    public string PythonExe => _factory.PythonExe;

    public V2Python(IPythonProcessFactory factory, IConfiguration config, ILogger<V2Python> logger)
    {
        _factory = factory;
        _logger = logger;

        _timeoutMs = int.TryParse(config["DrawingsV2:ParseTimeoutMs"], out var t) && t > 0 ? t : 30_000;
        // 感知（OCR+码检测）远慢于矢量解析：M4 实测单张 2~25s，取 120s 上限
        _photoTimeoutMs = int.TryParse(config["DrawingsV2:PhotoTimeoutMs"], out var p) && p > 0 ? p : 120_000;

        ScriptPath = _factory.LocateScript(
            "vpdf_run.py",
            Path.Combine("Modules", "DrawingsV2", "Scripts"),
            Path.Combine("Modules", "DrawingsV2", "Scripts", "vpdf"));

        ObserveScriptPath = _factory.LocateScript(
            "observe_run.py",
            Path.Combine("Modules", "DrawingsV2", "Scripts"),
            Path.Combine("Modules", "DrawingsV2", "Scripts", "observe"));
    }

    /// <summary>解析一份 PDF，返回 vpdf/1 文档对象。</summary>
    public async Task<VpdfDocument> ParseAsync(string pdfPath, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(pdfPath)) throw new V2Exception("pdfPath 为空");
        if (!File.Exists(pdfPath)) throw new V2Exception($"PDF 不存在：{pdfPath}");

        var json = await RunAsync(new[] { "parse", pdfPath, "--json-only" }, ct);
        try
        {
            return VectorPdfParser.Parse(json);
        }
        catch (V2Exception)
        {
            throw;
        }
        catch (Exception e)
        {
            throw new V2Exception($"vpdf 输出不是合法契约 JSON：{e.Message}");
        }
    }

    /// <summary>感知层自检（归一化数学断言），用于健康检查端点。</summary>
    public async Task<string> SelfCheckAsync(CancellationToken ct = default)
        => await RunAsync(new[] { "selfcheck" }, ct);

    /// <summary>照片全图盲感知，返回 observe/1 JSON 文本。</summary>
    public async Task<string> ObserveAsync(string photoPath, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(photoPath)) throw new V2Exception("photoPath 为空");
        if (!File.Exists(photoPath)) throw new V2Exception($"照片不存在：{photoPath}");
        return await RunAsync(new[] { "observe", photoPath, "--json-only" }, ct,
            ObserveScriptPath, _photoTimeoutMs, "observe 感知");
    }

    /// <summary>按预期位置定点感知，返回 observe-verify/1 JSON 文本。</summary>
    public async Task<string> VerifyAsync(string photoPath, string regionsJsonPath, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(photoPath)) throw new V2Exception("photoPath 为空");
        if (!File.Exists(photoPath)) throw new V2Exception($"照片不存在：{photoPath}");
        if (string.IsNullOrWhiteSpace(regionsJsonPath) || !File.Exists(regionsJsonPath))
            throw new V2Exception($"区域清单不存在：{regionsJsonPath}");
        return await RunAsync(new[] { "verify", photoPath, "--regions", regionsJsonPath, "--json-only" }, ct,
            ObserveScriptPath, _photoTimeoutMs, "observe 定点验证");
    }

    /// <summary>
    /// 渲染 PDF 指定页为 PNG 字节（供 H5 可视化图纸回显）。
    /// 调 vpdf_run.py render 子命令，stdout 输出二进制 PNG。
    /// </summary>
    public async Task<byte[]> RenderDrawingPageAsync(string pdfPath, int pageIndex = 0, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(pdfPath)) throw new V2Exception("pdfPath 为空");
        if (!File.Exists(pdfPath)) throw new V2Exception($"PDF 不存在：{pdfPath}");

        // 二进制 stdout 不能用 OutputDataReceived（按行读会破坏 PNG），
        // 必须**先**异步读 stdout 再 WaitForExit，否则管道写满死锁
        var psi = _factory.Create(ScriptPath, new[] { "render", pdfPath, "--page", pageIndex.ToString() });
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;

        using var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        if (!proc.Start())
            throw new V2Exception("Python 进程启动失败");

        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(_timeoutMs);

        // 异步并发读 stdout（PNG 二进制）和 stderr（噪声），避免管道死锁
        var stdoutTask = ReadStreamToEndAsync(proc.StandardOutput.BaseStream, timeoutCts.Token);
        var stderrTask = proc.StandardError.ReadToEndAsync(timeoutCts.Token);

        try
        {
#if NET8_0_OR_GREATER
            await proc.WaitForExitAsync(timeoutCts.Token);
#else
            await Task.Run(() => proc.WaitForExit(), timeoutCts.Token);
#endif
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            TryKill(proc);
            throw new V2Exception($"render 超时（>{_timeoutMs}ms）：{pdfPath}");
        }

        proc.WaitForExit();
        var png = await stdoutTask;
        var stderr = await stderrTask;
        if (proc.ExitCode != 0)
        {
            var msg = stderr.Trim();
            if (msg.Length == 0) msg = $"Python 退出码 {proc.ExitCode}";
            throw new V2Exception("render 失败：" + Trunc(msg, 400));
        }

        return png;
    }

    /// <summary>异步读取流到末尾，返回二进制字节数组（用于 PNG stdout）。</summary>
    private static async Task<byte[]> ReadStreamToEndAsync(System.IO.Stream stream, CancellationToken ct)
    {
        using var ms = new System.IO.MemoryStream();
        var buf = new byte[64 * 1024];
        int read;
        while ((read = await stream.ReadAsync(buf, 0, buf.Length, ct)) > 0)
            ms.Write(buf, 0, read);
        return ms.ToArray();
    }

    /// <summary>
    /// 起 Python 进程并取回 stdout。
    /// stdout / stderr 均异步排空 —— 见类注释中的「Ink 批注噪声」实测依据。
    /// </summary>
    private async Task<string> RunAsync(string[] args, CancellationToken ct,
        string? script = null, int? timeoutMs = null, string op = "vpdf 解析")
    {
        var psi = _factory.Create(script ?? ScriptPath, args);
        var effectiveTimeout = timeoutMs ?? _timeoutMs;

        using var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();

        // 必须**先**挂上异步读取再 Start，否则可能丢失开头输出
        proc.OutputDataReceived += (_, e) => { if (e.Data is not null) lock (stdout) stdout.AppendLine(e.Data); };
        proc.ErrorDataReceived += (_, e) => { if (e.Data is not null) lock (stderr) stderr.AppendLine(e.Data); };

        if (!proc.Start())
            throw new V2Exception("Python 进程启动失败");

        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(effectiveTimeout);

        try
        {
#if NET8_0_OR_GREATER
            await proc.WaitForExitAsync(timeoutCts.Token);
#else
            await Task.Run(() => proc.WaitForExit(), timeoutCts.Token);
#endif
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            TryKill(proc);
            throw new V2Exception($"{op}超时（>{effectiveTimeout}ms）：{string.Join(' ', args)}");
        }

        // WaitForExitAsync 返回后，异步读取可能尚未排空，补一次同步等待
        proc.WaitForExit();

        string err;
        lock (stderr) err = stderr.ToString();

        if (proc.ExitCode != 0)
        {
            var msg = err.Trim();
            if (msg.Length == 0) msg = $"Python 退出码 {proc.ExitCode}";
            throw new V2Exception(op + "失败：" + Trunc(msg, 400));
        }

        // stderr 非空不一定是错误（MuPDF 的 C 层噪声可能落在任一流），只记日志不影响结果
        var noise = err.Trim();
        if (noise.Length > 0)
            _logger.LogDebug("vpdf stderr（已知噪声，不影响结果）：{Noise}", Trunc(noise, 300));

        string raw;
        lock (stdout) raw = stdout.ToString();
        return ExtractJson(raw);
    }

    /// <summary>
    /// 从 stdout 中切出 JSON 片段。
    /// MuPDF 的 C 层噪声可能出现在 JSON **之前**（M3 实测落在 stdout，见类注释），
    /// 因此不能假定 stdout 以 '{' 开头；取首个 '{' 到最后一个 '}' 之间的内容。
    /// </summary>
    private static string ExtractJson(string raw)
    {
        var i = raw.IndexOf('{');
        var j = raw.LastIndexOf('}');
        if (i < 0 || j < i)
            throw new V2Exception("Python 未输出 JSON（stdout 前 200 字：" + Trunc(raw.Trim(), 200) + "）");
        return raw.Substring(i, j - i + 1);
    }

    private static void TryKill(Process p)
    {
        try { if (!p.HasExited) p.Kill(entireProcessTree: true); } catch { }
    }

    private static string Trunc(string s, int n) => s.Length <= n ? s : s[..n] + "…";
}
