using System.Diagnostics;
using System.Text.Json;

namespace Platform.Modules.InvTail;

/// <summary>
/// 本模块自带的 Python 运行器（刻意不复用其它模块的实现，保证模块独立可插拔）。
/// <para>为什么要自己构造环境：ASP.NET 宿主（dotnet run / nohup / Git-Bash 拉起）会把一份被污染的
/// PATH、PYTHONPATH、PYTHONHOME 继承给子进程，venv 解释器可能在【原生 DLL 加载 / site 初始化】阶段
/// 静默崩溃（exit=1、无 stdout/stderr、脚本第一行都不执行）。这里显式构造「最小必要环境」，
/// 只保留 venv 目录 + Windows 系统目录，并强制 UTF-8，从根上消除对宿主环境的依赖。</para>
/// </summary>
public sealed class InvTailPython
{
    private readonly string _pythonExe;
    private readonly string _scriptDir;
    private static readonly SemaphoreSlim _heavyLock = new(2, 2); // OCR 较吃 CPU，限并发

    public string PythonExe => _pythonExe;

    public InvTailPython(IConfiguration config)
    {
        _pythonExe = config["InvTail:PythonExe"]
                     ?? config["Ocr:PythonExe"]
                     ?? @"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe";
        _scriptDir = ResolveScriptDir();
    }

    /// <summary>定位本模块 Python 脚本目录：先看运行目录/工作目录，再从 BaseDirectory 向上搜 6 层。</summary>
    private static string ResolveScriptDir()
    {
        var probe = new List<string>
        {
            Path.Combine(Directory.GetCurrentDirectory(), "Modules", "InvTail"),
            Path.Combine(AppContext.BaseDirectory, "Modules", "InvTail")
        };
        var cur = AppContext.BaseDirectory;
        for (int i = 0; i < 6; i++)
        {
            var parent = Path.GetDirectoryName(cur.TrimEnd(Path.DirectorySeparatorChar));
            if (string.IsNullOrEmpty(parent) || parent == cur) break;
            probe.Add(Path.Combine(parent, "Modules", "InvTail"));
            cur = parent;
        }
        foreach (var d in probe)
        {
            if (Directory.Exists(d) && File.Exists(Path.Combine(d, "inv_ocr.py"))) return d;
        }
        // 找不到也不在构造期抛异常（避免拖垮平台启动）；调用时再报错
        return probe[0];
    }

    /// <summary>取脚本绝对路径，缺失则抛业务异常（只影响本模块端点，不影响平台启动）。</summary>
    public string Script(string fileName)
    {
        var p = Path.Combine(_scriptDir, fileName);
        if (!File.Exists(p))
            throw new InvTailException($"脚本缺失：{fileName}（查找目录 {_scriptDir}）");
        return p;
    }

    private ProcessStartInfo CreatePsi(string script, IEnumerable<string> args)
    {
        if (!File.Exists(_pythonExe))
            throw new InvTailException($"Python 解释器不存在：{_pythonExe}（可用配置 InvTail:PythonExe 覆盖）");

        var psi = new ProcessStartInfo(_pythonExe)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = false,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8
        };
        psi.ArgumentList.Add(script);
        foreach (var a in args) psi.ArgumentList.Add(a);

        try
        {
            var wd = Path.GetDirectoryName(Path.GetFullPath(script));
            if (!string.IsNullOrEmpty(wd)) psi.WorkingDirectory = wd;
        }
        catch { }

        var env = psi.EnvironmentVariables;
        env.Clear();
        var pyScripts = Path.GetDirectoryName(_pythonExe) ?? "";
        var pyRoot = Path.GetDirectoryName(pyScripts) ?? "";
        env["PATH"] = string.Join(";", new[] { pyScripts, pyRoot, @"C:\windows\system32", @"C:\windows" }
            .Where(d => !string.IsNullOrEmpty(d)));
        env["PYTHONUTF8"] = "1";
        env["PYTHONIOENCODING"] = "utf-8";
        env["SYSTEMROOT"] = Environment.GetEnvironmentVariable("SYSTEMROOT") ?? @"C:\windows";
        env["WINDIR"] = Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\windows";
        env["SystemDrive"] = Environment.GetEnvironmentVariable("SystemDrive") ?? "C:";
        var tmp = Environment.GetEnvironmentVariable("TEMP") ?? Path.GetTempPath();
        env["TEMP"] = tmp;
        env["TMP"] = Environment.GetEnvironmentVariable("TMP") ?? tmp;
        env["COMSPEC"] = Environment.GetEnvironmentVariable("COMSPEC") ?? @"C:\windows\system32\cmd.exe";
        var nproc = Environment.GetEnvironmentVariable("NUMBER_OF_PROCESSORS");
        if (!string.IsNullOrEmpty(nproc)) env["NUMBER_OF_PROCESSORS"] = nproc;
        env["OS"] = "Windows_NT";
        var arch = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE");
        if (!string.IsNullOrEmpty(arch)) env["PROCESSOR_ARCHITECTURE"] = arch;
        var up = Environment.GetEnvironmentVariable("USERPROFILE");
        if (!string.IsNullOrEmpty(up)) env["USERPROFILE"] = up;
        return psi;
    }

    /// <summary>
    /// 执行脚本并解析 stdout 中的 JSON 段。
    /// 约定：脚本必须输出 {"success":bool, ...}；success=false 时抛 InvTailException 带 error 文案。
    /// </summary>
    public JsonDocument Run(string scriptFile, IEnumerable<string> args, int timeoutMs = 180_000, bool heavy = false)
    {
        if (heavy) _heavyLock.Wait();
        try
        {
            var script = Script(scriptFile);
            string stdout = "", stderr = "";
            int exit = -1;
            var psi = CreatePsi(script, args);
            using (var p = Process.Start(psi) ?? throw new InvTailException("无法启动 Python 进程"))
            {
                var o = p.StandardOutput.ReadToEndAsync();
                var e = p.StandardError.ReadToEndAsync();
                if (!p.WaitForExit(timeoutMs)) { try { p.Kill(true); } catch { } }
                Task.WaitAll(o, e);
                stdout = o.Result ?? "";
                stderr = e.Result ?? "";
                exit = p.ExitCode;
            }

            if (string.IsNullOrWhiteSpace(stdout))
                throw new InvTailException($"识别/导出服务无输出（exit={exit}）：{Trunc(stderr, 240)}");

            var s = stdout.IndexOf('{');
            var t = stdout.LastIndexOf('}');
            if (s < 0 || t <= s)
                throw new InvTailException($"识别/导出服务返回异常：{Trunc(stdout, 240)}");

            var doc = JsonDocument.Parse(stdout.Substring(s, t - s + 1));
            var root = doc.RootElement;
            if (!root.TryGetProperty("success", out var ok) || ok.ValueKind != JsonValueKind.True)
            {
                var msg = root.TryGetProperty("error", out var em) ? em.GetString() : null;
                doc.Dispose();
                throw new InvTailException(string.IsNullOrWhiteSpace(msg) ? "处理失败" : msg!);
            }
            return doc;
        }
        finally
        {
            if (heavy) _heavyLock.Release();
        }
    }

    private static string Trunc(string s, int n) =>
        string.IsNullOrEmpty(s) ? "(empty)" : (s.Length <= n ? s : s[..n]);
}
