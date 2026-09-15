using System.Collections.Specialized;
using System.Diagnostics;
using System.IO;
using System.Linq;
using Microsoft.Extensions.Configuration;

namespace Platform.Infrastructure;

/// <summary>
/// 平台级 Python 进程基础设施。
///
/// 【为什么存在】
/// 历史问题：各业务模块需要一个「与宿主环境隔离的干净 Python 进程」来跑自己的脚本，
/// 但由于平台层没有提供这一能力，Unbind / DefectHistory 被迫去借用 Drawings 模块的
/// OcrService.CreatePythonPsi —— 造成「业务模块依赖另一个业务模块」的违规耦合：
/// 一旦 Drawings 模块被替换或未注册，Unbind / DefectHistory 的 DI 解析会直接失败，
/// 平台随之启动崩溃。
///
/// 本类把「构造干净的 Python 进程」这一真正的平台能力下沉到平台层，
/// 使任何模块（含未来的 v2）都只依赖平台，不再横向依赖其它模块。
///
/// 【设计原则】
///  1. 与业务无关：不含任何 OCR / 图纸 / 二维码语义。
///  2. 与旧实现无关：不复用、不继承 OcrService，独立实现（旧 OcrService 已封存）。
///  3. 环境最小化：显式构造最小必要环境变量，彻底摆脱宿主环境（Git-Bash / nohup /
///     计划任务拉起时携带的 MSYS、PYTHONPATH 等）导致解释器初始化静默崩溃的问题。
/// </summary>
public interface IPythonProcessFactory
{
    /// <summary>使用的 Python 解释器绝对路径。</summary>
    string PythonExe { get; }

    /// <summary>
    /// 构造一个与宿主环境隔离的 Python 进程启动信息。
    /// 默认不重定向 stdin（detached 宿主下重定向 stdin 会令控制台 python.exe 初始化崩溃）。
    /// </summary>
    ProcessStartInfo Create(string script, params string[] args);

    /// <summary>同上，但允许显式控制是否重定向 stdin（常驻 worker 需要 true）。</summary>
    ProcessStartInfo Create(string script, bool redirectStandardInput, params string[] args);

    /// <summary>按文件名定位脚本（兼容 dotnet run 与发布后目录结构）。</summary>
    string LocateScript(string fileName, params string[] subDirCandidates);
}

public sealed class PythonProcessFactory : IPythonProcessFactory
{
    private readonly string _pythonExe;
    private readonly string[] _extraPathDirs;

    public string PythonExe => _pythonExe;

    public PythonProcessFactory(IConfiguration config)
    {
        // 配置优先级：Python:Exe（新键） > Ocr:PythonExe（历史键，兼容既有部署） > 默认 venv
        _pythonExe = config["Python:Exe"]
                     ?? config["Ocr:PythonExe"]
                     ?? @"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe";

        var extra = config["Python:ExtraPathDirs"];
        _extraPathDirs = string.IsNullOrWhiteSpace(extra)
            ? System.Array.Empty<string>()
            : extra.Split(';', System.StringSplitOptions.RemoveEmptyEntries | System.StringSplitOptions.TrimEntries);
    }

    public ProcessStartInfo Create(string script, params string[] args)
        => Create(script, false, args);

    public ProcessStartInfo Create(string script, bool redirectStandardInput, params string[] args)
    {
        var psi = new ProcessStartInfo(_pythonExe)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = redirectStandardInput,
            UseShellExecute = false,
            CreateNoWindow = true,
            // 脚本输出 UTF-8（含中文），默认 GBK 解码会静默吞掉内容
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8
        };

        psi.ArgumentList.Add(script);
        if (args != null)
            foreach (var a in args) psi.ArgumentList.Add(a);

        // 工作目录固定到脚本所在目录：宿主 cwd（可能是 bin/Debug）若含同名原生库，
        // 会因 DLL 搜索顺序被优先加载，导致解释器初始化崩溃。
        try
        {
            var wd = Path.GetDirectoryName(Path.GetFullPath(script));
            if (!string.IsNullOrEmpty(wd)) psi.WorkingDirectory = wd;
        }
        catch { }

        ApplyMinimalEnvironment(psi);
        return psi;
    }

    /// <summary>
    /// 清空继承来的全部环境变量，仅重建最小必要集。
    /// 这是「宿主拉起方式不同 → 行为不同」这一类偶发故障的根治手段。
    /// </summary>
    private void ApplyMinimalEnvironment(ProcessStartInfo psi)
    {
        var env = psi.EnvironmentVariables;
        env.Clear();

        var pyScripts = Path.GetDirectoryName(_pythonExe) ?? "";
        var pyRoot = Path.GetDirectoryName(pyScripts) ?? "";

        env["PATH"] = string.Join(";",
            new[] { pyScripts, pyRoot, @"C:\windows\system32", @"C:\windows" }
                .Concat(_extraPathDirs)
                .Where(d => !string.IsNullOrEmpty(d)));

        env["PYTHONUTF8"] = "1";
        env["PYTHONIOENCODING"] = "utf-8";

        // Windows CRT / Win32 API 初始化所需基础变量，缺失会导致子进程起不来
        env["SYSTEMROOT"] = System.Environment.GetEnvironmentVariable("SYSTEMROOT") ?? @"C:\windows";
        env["WINDIR"] = System.Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\windows";
        env["SystemDrive"] = System.Environment.GetEnvironmentVariable("SystemDrive") ?? @"C:";
        env["COMSPEC"] = System.Environment.GetEnvironmentVariable("COMSPEC") ?? @"C:\windows\system32\cmd.exe";
        env["OS"] = "Windows_NT";

        var tmp = System.Environment.GetEnvironmentVariable("TEMP") ?? Path.GetTempPath();
        env["TEMP"] = tmp;
        env["TMP"] = System.Environment.GetEnvironmentVariable("TMP") ?? tmp;

        CopyIfPresent(env, "NUMBER_OF_PROCESSORS");
        CopyIfPresent(env, "PROCESSOR_ARCHITECTURE");
        CopyIfPresent(env, "USERPROFILE");
        CopyIfPresent(env, "HOMEDRIVE");
        CopyIfPresent(env, "HOMEPATH");
    }

    private static void CopyIfPresent(StringDictionary env, string name)
    {
        var v = System.Environment.GetEnvironmentVariable(name);
        if (!string.IsNullOrEmpty(v)) env[name] = v;
    }

    public string LocateScript(string fileName, params string[] subDirCandidates)
    {
        var baseDir = AppContext.BaseDirectory;
        var cwd = Directory.GetCurrentDirectory();

        var dirs = new System.Collections.Generic.List<string>();
        foreach (var sub in subDirCandidates ?? System.Array.Empty<string>())
        {
            if (string.IsNullOrWhiteSpace(sub)) continue;
            dirs.Add(Path.Combine(cwd, sub));
            dirs.Add(Path.Combine(baseDir, sub));
        }
        dirs.Add(cwd);
        dirs.Add(baseDir);

        foreach (var d in dirs)
        {
            try
            {
                var p = Path.Combine(d, fileName);
                if (File.Exists(p)) return p;
            }
            catch { }
        }

        // 向上最多 6 级，覆盖源码树（src/Platform/...）与发布目录两种布局
        var current = baseDir;
        for (int i = 0; i < 6; i++)
        {
            var parent = Path.GetDirectoryName(current);
            if (string.IsNullOrEmpty(parent) || parent == current) break;
            try
            {
                var p = Path.Combine(parent, fileName);
                if (File.Exists(p)) return p;
                foreach (var sub in subDirCandidates ?? System.Array.Empty<string>())
                {
                    if (string.IsNullOrWhiteSpace(sub)) continue;
                    var p2 = Path.Combine(parent, sub, fileName);
                    if (File.Exists(p2)) return p2;
                }
            }
            catch { }
            current = parent;
        }

        return Path.Combine(baseDir, fileName);
    }
}
