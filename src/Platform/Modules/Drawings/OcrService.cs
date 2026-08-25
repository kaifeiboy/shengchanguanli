using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;
using System.Threading;
using Microsoft.Extensions.Configuration;

namespace Platform.Modules.Drawings;

/// <summary>
/// OCR 服务：
///  - Recognize(IFormFile)：对上传图片做 OCR，识别文字用于提取部位。
///  - Recognize(string)：对本地图片路径做 OCR（供切割块预识别）。
///  - BatchOcr(List&lt;string&gt;)：一次拉起 Python 进程，内部批量 OCR 多张图。
///  - RecognizePdf(物理路径)：调用 pdf_ocr.py（PyMuPDF 渲染 + 本机 Tesseract）。
/// 对应需求 FR-01。
///
/// 关键设计（根治 .NET 反复拉起原生进程退化）：
///  - Tesseract 不再由 .NET 直接 Process.Start 拉起，而是收敛到「单个 Python 进程
///    内顺序调用」——经 ocr_cli.py（单图）与 batch_ocr.py（批量）包装。
///    原因：.NET 反复启动 tesseract.exe 会出现「前 1~2 次成功、之后静默不写输出」
///    的退化；统一收口到 Python 进程后由 Python 妥善管理子进程，稳定可靠。
///  - 所有 I/O 统一落到 data/ocr/（真实 E:\ 路径），避开 GetTempPath 沙箱解析坑。
/// </summary>
/// <summary>OCR 子进程（Python）三次重试仍全部静默崩溃（exit=1、零输出）时抛出。
/// 用于把「识别服务临时不可用」与「照片确实没拍到字」在业务层区分开，
/// 避免把服务端抖动误诊为「用户照片无法识别」。</summary>
public class OcrUnavailableException : Exception
{
    public OcrUnavailableException(string message) : base(message) { }
}

public class OcrService
{
    private readonly string _pythonExe;
    private readonly string _pdfScript;
    private readonly string _ocrCliScript;
    private readonly string _batchOcrScript;
    private readonly string _ocrWork;
    private readonly string _workerScript;
    private readonly OcrWorkerClient _worker;

    /// <summary>供 DrawingService 调用图形脚本时复用同一 Python 路径。</summary>
    public string PythonExe => _pythonExe;

    /// <summary>常驻 OCR 工作进程脚本路径（ocr_worker.py）。</summary>
    public string WorkerScriptPath => _workerScript;

    /// <summary>供诊断：pdf_ocr.py 的解析路径。</summary>
    public string PdfScriptPath => _pdfScript;

    public OcrService(IConfiguration config)
    {
        _pythonExe = config["Ocr:PythonExe"]
            ?? @"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe";
        _pdfScript = config["Ocr:PdfScript"] ?? FindScript("pdf_ocr.py");
        _ocrCliScript = FindScript("ocr_cli.py");
        _batchOcrScript = FindScript("batch_ocr.py");
        _workerScript = FindScript("ocr_worker.py");
        // 常驻 OCR 工作进程客户端（根治每次请求 Process.Start 原生进程退化）
        _worker = new OcrWorkerClient(this);

        // OCR 工作目录：与 Database:Path 同树（data/ocr），真实 E:\ 路径，
        // 保证原生 Tesseract 进程能稳定读写（避开 GetTempPath 的沙箱解析坑）。
        var dbPath = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
        var dataDir = Path.GetDirectoryName(dbPath) ?? AppContext.BaseDirectory;
        _ocrWork = Path.Combine(dataDir, "ocr");
        try { Directory.CreateDirectory(_ocrWork); } catch { }
    }

    /// <summary>从 BaseDirectory 向上查找脚本文件（兼容 dotnet run 与发布后路径）。</summary>
    private static string FindScript(string filename)
    {
        var baseDir = AppContext.BaseDirectory;
        var candidates = new[]
        {
            Path.Combine(Directory.GetCurrentDirectory(), "Modules", "Drawings"),
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
            var p2 = Path.Combine(parent, "Modules", "Drawings", filename);
            if (File.Exists(p2)) return p2;
            current = parent;
        }
        return Path.Combine(baseDir, filename);
    }

    /// <summary>对上传的图片文件做 OCR，返回识别文本。</summary>
    public string Recognize(IFormFile file)
    {
        var ext = Path.GetExtension(file.FileName);
        if (string.IsNullOrWhiteSpace(ext)) ext = ".png";
        var stamp = Guid.NewGuid().ToString("N");
        var inFile = Path.Combine(_ocrWork, "in_" + stamp + ext);
        try
        {
            // 先读到内存再原子写入磁盘，避免 FileCreate+CopyTo 的文件锁/刷新时序问题
            using var ms = new MemoryStream();
            file.CopyTo(ms);
            var bytes = ms.ToArray();
            File.WriteAllBytes(inFile, bytes);

            var (text, failed) = _worker.OcrOne(inFile);
            // 上传路径：服务不可用要明确抛出，让业务层给「稍后重试」而非「照片无法识别」
            if (failed) throw new OcrUnavailableException(
                "OCR 子进程连续重试仍静默崩溃（识别服务暂时不可用）");
            return text;
        }
        finally
        {
            TryDelete(inFile);
        }
    }

    /// <summary>对本地图片文件路径做 OCR（供切割块预识别用），返回识别文本。
    /// 注：此路径（切图预识别）保持「静默失败」语义，不抛 OcrUnavailableException，
    /// 以免影响切割流程——切图失败由调用方另行处理。</summary>
    public string Recognize(string imagePath)
    {
        var (text, _) = _worker.OcrOne(imagePath);
        return text;
    }

    /// <summary>
    /// ⭐ 合并任务接入：在常驻 OCR worker 内跑 diff_visualizer.run_on_photo，零 RapidOCR 重载。
    /// 返回 (PayloadJson, Failed, Error)。失败时调用方应回退到「单独 OCR + MarkDifferencesOnPhoto」旧路径。
    /// </summary>
    public (string? PayloadJson, bool Failed, string? Error) CombinedDiff(string imagePath, string did, string output, string? db, string? seg)
        => _worker.CombinedDiff(imagePath, did, output, db, seg);

    /// <summary>批量 OCR：一次拉起 Python 进程，内部顺序对各图片调用 Tesseract，
    /// 返回 {图片路径 → 识别文本}。规避 .NET 反复拉起原生 Tesseract 进程导致的退化。</summary>
    public Dictionary<string, string> BatchOcr(List<string> images)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (images == null || images.Count == 0) return result;
        var outJson = Path.Combine(_ocrWork, "batch_" + Guid.NewGuid().ToString("N") + ".json");
        var psi = CreatePythonPsi(_batchOcrScript,
            new[] { outJson }.Concat(images).ToArray());
        try
        {
            using var p = Process.Start(psi)!;
            // 并发排空 stdout/stderr，避免「先读 stdout 阻塞到 EOF、而子进程因 stderr 缓冲满而卡写」的经典死锁
            var outTask = p.StandardOutput.ReadToEndAsync();
            var errTask = p.StandardError.ReadToEndAsync();
            var exited = p.WaitForExit(180_000);
            if (!exited) { try { p.Kill(); } catch { } }
            Task.WaitAll(outTask, errTask);
        }
        catch { }
        try
        {
            if (File.Exists(outJson))
            {
                var map = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(outJson));
                if (map != null)
                    foreach (var kv in map) result[kv.Key] = kv.Value ?? "";
            }
        }
        catch { }
        try { File.Delete(outJson); } catch { }
        return result;
    }

    /// <summary>L1 引擎锁定：主动拉起并预热常驻 OCR worker（构造引擎 + 跑一次真实推理 warm-up）。
    /// Platform 启动时调用，使首拍匹配跳过冷启动窗口，从根上消除「忽好忽坏」。</summary>
    public void WarmupWorker()
    {
        try
        {
            // 占位输入：64x64 白底黑块 PNG（base64），仅用于触发 worker 拉起 + 内部 warm-up 推理。
            // ⭐ v19.50 修复（2026-08-21 对比超时根因）：原 1x1 图有两重缺陷——
            //   ① 旧 base64 的 PNG 数据流损坏，PIL load() 报 "broken PNG file (chunk b'\x00IEN')"
            //   ② 即使 1x1 图合法，RapidOCR 处理 1x1 极端小图也会失败 → warm-up 推理从未真实
            //      执行（ENGINE=tesseract_fallback）→ 引擎 ONNX 模型冷状态却宣告 ready →
            //      业务 combined 请求偶发卡死 60s → KillWorker → 重启失败 → 冷却期内回退
            //      subprocess 3×60s → "对比结果超时"。64x64 图可触发 det/rec 真实推理（实测 OK）。
            var tmp = Path.Combine(_ocrWork, "warmup_" + Guid.NewGuid().ToString("N") + ".png");
            var pngBytes = Convert.FromBase64String(
                "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAnUlEQVR4nO3ZQQrDMAwF0dGQ+1/ZvUCySFswE/K2AiMQSPA9ay3KJE7iJE7iJE7iJO64KszM3be2nBSJkziJkziJkziJkziJkziJkziJkziJk7h5g63NJE7iJE7i5Km50L98kS9dOd34+QlInMRJnMRJnMRJnMRJnMRJnMRJnMRJnMRJnMRJnMRJnMRJ3Lz/A5tJnMRJnMS5u4FffQCXvA95w8VdowAAAABJRU5ErkJggg==");
            File.WriteAllBytes(tmp, pngBytes);
            // EnsureStarted → 拉起 worker → 内部 warm-up → __ready__ 后才返回
            var (text, failed) = _worker.OcrOne(tmp);
            File.AppendAllText(Path.Combine(_ocrWork, "ocr_worker.log"),
                $"[{DateTime.Now:HH:mm:ss}] [OCR-WARMUP] done text_len={text.Length} failed={failed}\n");
            TryDelete(tmp);
        }
        catch (Exception ex)
        {
            try { File.AppendAllText(Path.Combine(_ocrWork, "ocr_worker.log"),
                $"[{DateTime.Now:HH:mm:ss}] [OCR-WARMUP] exc {ex.GetType().Name}:{ex.Message}\n"); } catch { }
        }
    }

    /// <summary>统一驱动 OCR（经 ocr_cli.py 包装调用 Tesseract，文本走 stdout 返回）。
    /// <para>健壮性 v10：宿主（长生命周期 ASP.NET 进程）反复拉起原生 Python 子进程时，
    /// 偶发「子进程静默崩溃 exit=1、无 stdout/stderr」（原生 DLL 加载 / 句柄耗尽类抖动）。
    /// 单次失败会让照片 OCR 秒返空 → 匹配全 0 → "无法识别"。这里对【非 0 退出 / 超时 / 异常】
    /// 做最多 3 次重试，每次间隔递增，覆盖偶发抖动；exit=0 但文本为空视为「确无可识别内容」
    /// 直接返回，避免无效重试。</para>
    /// </summary>
    /// <returns>(Text, Failed)：Text=识别文本（崩溃时为空）；Failed=三次重试全挂（服务不可用）。</returns>
    /// <summary>Legacy OCR：每次请求 Process.Start 拉起一个新 Python 进程（旧路径）。
    /// 现作为【常驻 worker 起不来时的兜底】保留——永不丢失 OCR 能力。</summary>
    private (string Text, bool Failed) LegacyOcr(string inFile)
    {
        if (string.IsNullOrWhiteSpace(inFile) || !File.Exists(inFile)) return ("", false);
        const int maxAttempts = 3;
        string? lastDiag = null;
        for (int attempt = 1; attempt <= maxAttempts; attempt++)
        {
            var psi = CreatePythonPsi(_ocrCliScript, inFile);
            try
            {
                using var p = Process.Start(psi)!;
                // 并发排空 stdout/stderr，避免「先读 stdout 阻塞到 EOF、而子进程因 stderr 缓冲满而卡写」的经典死锁
                var outTask = p.StandardOutput.ReadToEndAsync();
                var errTask = p.StandardError.ReadToEndAsync();
                var exited = p.WaitForExit(60_000);
                if (!exited)
                {
                    try { p.Kill(); } catch { }
                    lastDiag = $"[attempt{attempt}] TIMEOUT";
                    if (attempt < maxAttempts) { Thread.Sleep(200 * attempt); continue; }
                    break;
                }
                Task.WaitAll(outTask, errTask);
                var text = outTask.Result ?? "";
                var err = errTask.Result ?? "";
                // [DIAG] 每次尝试都留痕，便于区分「偶发崩溃」与「真无文本」
                try { File.AppendAllText(Path.Combine(_ocrWork, "ocr_dbg.log"),
                    $"[{DateTime.Now:HH:mm:ss}] RUNOCR attempt{attempt}/{maxAttempts} exit={p.ExitCode} stdout_len={text?.Length ?? 0} stderr_len={err?.Length ?? 0} " +
                    $"stdout_head={((text?.Length > 0) ? text[..Math.Min(80,text.Length)] : "(empty)")} " +
                    $"stderr_head={((err?.Length > 0) ? err[..Math.Min(120,err.Length)] : "(none)")}\n"); } catch { }
                if (!string.IsNullOrWhiteSpace(err))
                {
                    // OCR 内部报错（极少发生）：留痕到 ocr_dbg.log 便于后续排查
                    try { File.AppendAllText(Path.Combine(_ocrWork, "ocr_dbg.log"),
                        $"[{DateTime.Now:HH:mm:ss}] STDERR: {err}\n"); } catch { }
                }
                // exit=0 → 无论是否有文本都视为有效产物（空=确实没识别到），不重试
                if (p.ExitCode == 0)
                    return (text ?? "", false);
                // 非 0 退出（含静默崩溃 exit=1 无输出）→ 可能是偶发抖动，重试
                lastDiag = $"[attempt{attempt}] exit={p.ExitCode}";
                if (attempt < maxAttempts) { Thread.Sleep(200 * attempt); continue; }
                break;
            }
            catch (Exception ex)
            {
                lastDiag = $"[attempt{attempt}] EXC {ex.GetType().Name}:{ex.Message}";
                if (attempt < maxAttempts) { Thread.Sleep(200 * attempt); continue; }
                break;
            }
        }
        try { File.AppendAllText(Path.Combine(_ocrWork, "ocr_dbg.log"),
            $"[{DateTime.Now:HH:mm:ss}] RUNOCR ALL_ATTEMPTS_FAILED {lastDiag}\n"); } catch { }
        return ("", true);   // 三次全挂：服务暂不可用（非照片问题）
    }

    private static void TryDelete(string p)
    {
        try { File.Delete(p); } catch { }
    }

    /// <summary>
    /// 构造「与宿主环境隔离」的 Python 子进程启动信息。
    /// <para>根因：ASP.NET 宿主（dotnet run / PowerShell 启动脚本）会向子进程继承一份
    /// 可能被污染的 PATH（dotnet 原生运行库目录、系统 Python311 的 vcruntime 等），
    /// 以及宿主自己的 PYTHONPATH（可能挂有 sitecustomize / .pth，在解释器初始化阶段崩溃）。
    /// 这会让 venv 的 python.exe 在【原生 DLL 加载 / site 初始化】阶段静默崩溃
    /// （exit=1、无 stdout/stderr、模块代码完全不执行）→ OCR 秒返空 → "无法匹配+秒报错"。</para>
    /// <para>这里显式构造一份干净环境：继承宿主的全部变量后，仅重写 PATH 为
    /// 「venv 自身目录 + Windows 系统目录 + Tesseract」，丢弃可能指向不兼容包的
    /// PYTHONPATH，PYTHON* 显式设为 utf-8，从根上消除对宿主环境的依赖。</para>
    /// </summary>
    public ProcessStartInfo CreatePythonPsi(string script, params string[] args)
    {
        var psi = new ProcessStartInfo(_pythonExe)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            // 不重定向 stdin：detached 宿主（nohup 拉起）下，.NET 自建的 stdin 管道
            // 在子进程（控制台 python.exe）初始化阶段偶发静默崩溃（exit=1、无输出、不执行任何 Python 代码）。
            // 改为继承宿主 stdin（nohup 下为 /dev/null，合法且无害；ocr_cli.py 走文件参数不读 stdin）。
            RedirectStandardInput = false,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,    // ocr_cli.py 输出 UTF-8（含中文噪声），默认 GBK 会解码异常被吞
            StandardErrorEncoding = System.Text.Encoding.UTF8
        };
        psi.ArgumentList.Add(script);
        foreach (var a in args) psi.ArgumentList.Add(a);

        // 工作目录固定到脚本所在目录，规避宿主 cwd（可能为 bin/Debug）带来的
        // DLL 搜索顺序 / 相对路径歧义（DLL 搜索顺序中 cwd 位于 EXE 目录之后，
        // 若宿主 cwd 含同名原生库会被错误优先加载，导致解释器初始化崩溃）。
        try
        {
            var absScript = Path.GetFullPath(script);
            var wd = Path.GetDirectoryName(absScript);
            if (!string.IsNullOrEmpty(wd)) psi.WorkingDirectory = wd;
        }
        catch { }

        // 不再继承宿主【全部】环境变量：detached 宿主经由 Git-Bash/nohup 拉起时可能携带
        // MSYS 相关或异常变量（如 PYTHONPATH/PYTHONHOME/异常句柄相关），会令 venv 解释器
        // 在初始化阶段静默崩溃（exit=1、无 stdout/stderr、脚本首行探针都不执行）。
        // 改为显式构造「最小必要环境」，从根上消除对宿主环境的依赖。
        var env = psi.EnvironmentVariables;
        env.Clear();
        var pyScripts = Path.GetDirectoryName(_pythonExe) ?? "";
        var pyRoot = Path.GetDirectoryName(pyScripts) ?? "";
        // Tesseract 由 ocr_cli.py 以绝对路径（C:\Program Files\Tesseract-OCR\tesseract.exe）拉起，
        // 其 tessdata 也相对自身解析，不依赖 PATH；这里仅顺带纳入，无伤大雅。
        var tessDir = @"C:\Program Files\Tesseract-OCR";
        env["PATH"] = string.Join(";",
            new[] { pyScripts, pyRoot, @"C:\windows\system32", @"C:\windows", tessDir }
                .Where(d => !string.IsNullOrEmpty(d)));
        env["PYTHONUTF8"] = "1";
        env["PYTHONIOENCODING"] = "utf-8";
        // 关键 Windows 运行所需基础变量（缺失会导致 CRT / Win32 API 初始化异常）
        env["SYSTEMROOT"] = Environment.GetEnvironmentVariable("SYSTEMROOT") ?? @"C:\windows";
        env["WINDIR"] = Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\windows";
        env["SystemDrive"] = Environment.GetEnvironmentVariable("SystemDrive") ?? @"C:";
        var tmp = Environment.GetEnvironmentVariable("TEMP") ?? Path.GetTempPath();
        env["TEMP"] = tmp;
        env["TMP"] = Environment.GetEnvironmentVariable("TMP") ?? tmp;
        env["COMSPEC"] = Environment.GetEnvironmentVariable("COMSPEC") ?? @"C:\windows\system32\cmd.exe";
        var nproc = Environment.GetEnvironmentVariable("NUMBER_OF_PROCESSORS");
        if (!string.IsNullOrEmpty(nproc)) env["NUMBER_OF_PROCESSORS"] = nproc;
        env["OS"] = "Windows_NT";
        var arch = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE");
        if (!string.IsNullOrEmpty(arch)) env["PROCESSOR_ARCHITECTURE"] = arch;
        var userProfile = Environment.GetEnvironmentVariable("USERPROFILE");
        if (!string.IsNullOrEmpty(userProfile)) env["USERPROFILE"] = userProfile;
        var homeDrive = Environment.GetEnvironmentVariable("HOMEDRIVE");
        if (!string.IsNullOrEmpty(homeDrive)) env["HOMEDRIVE"] = homeDrive;
        var homePath = Environment.GetEnvironmentVariable("HOMEPATH");
        if (!string.IsNullOrEmpty(homePath)) env["HOMEPATH"] = homePath;

        return psi;
    }

    /// <summary>
    /// 几何辅助打分：给定一张照片 + 若干切块图路径，调用 geo_score.py 返回每张切块相对照片的
    /// 几何 inlier 比 / 表观 NCC / 融合分（顺序与输入切块一一对应）。
    /// 用于 DrawingService.MatchBlock 在 OCR 已有弱信号时做「几何确认/加分」。
    /// 任何失败 → 返回空列表（绝不影响 OCR 主链路）。
    /// </summary>
    public List<(int idx, double geo, double app, double fused)> GeoScore(string photoPath, List<string> blockImages)
    {
        var result = new List<(int, double, double, double)>();
        if (string.IsNullOrWhiteSpace(photoPath) || !File.Exists(photoPath)) return result;
        if (blockImages == null || blockImages.Count == 0) return result;
        var script = FindScript("geo_score.py");
        if (!File.Exists(script)) return result;

        var psi = CreatePythonPsi(script, new[] { photoPath }.Concat(blockImages).ToArray());
        try
        {
            using var p = Process.Start(psi)!;
            // 并发排空 stdout/stderr，避免经典死锁
            var outTask = p.StandardOutput.ReadToEndAsync();
            var errTask = p.StandardError.ReadToEndAsync();
            var exited = p.WaitForExit(120_000);
            if (!exited) { try { p.Kill(); } catch { } return result; }
            Task.WaitAll(outTask, errTask);
            var json = (outTask.Result ?? "").Trim();
            if (string.IsNullOrWhiteSpace(json)) return result;
            var arr = JsonSerializer.Deserialize<List<JsonElement>>(json);
            if (arr == null) return result;
            foreach (var el in arr)
            {
                int idx = el.TryGetProperty("idx", out var iEl) ? iEl.GetInt32() : 0;
                double geo = el.TryGetProperty("geo", out var gEl) ? gEl.GetDouble() : 0;
                double app = el.TryGetProperty("app", out var aEl) ? aEl.GetDouble() : 0;
                double fused = el.TryGetProperty("fused", out var fEl) ? fEl.GetDouble() : 0;
                result.Add((idx, geo, app, fused));
            }
        }
        catch { }
        return result;
    }

    /// <summary>
    /// 对本地 PDF 做 OCR：调用 pdf_ocr.py（PyMuPDF 渲染 + 本机 Tesseract）。
    /// 返回原始文本；失败时返回空串（不影响建库）。
    /// </summary>
    public string RecognizePdf(string pdfPhysicalPath)
    {
        if (!File.Exists(_pdfScript) || !File.Exists(pdfPhysicalPath))
            return "";

        var psi = CreatePythonPsi(_pdfScript, pdfPhysicalPath);

        try
        {
            using var p = Process.Start(psi)!;
            var outText = p.StandardOutput.ReadToEnd();
            var err = p.StandardError.ReadToEnd();
            var exited = p.WaitForExit(120_000);
            if (!exited) { try { p.Kill(); } catch { } return ""; }
            return outText;
        }
        catch
        {
            return "";
        }
    }

    /// <summary>
    /// 常驻 OCR 工作进程客户端（根治 .NET 反复拉起原生 Python 进程导致的偶发静默崩溃）。
    ///  - 宿主内【单例】惰性拉起一个 ocr_worker.py 进程，RapidOCR 引擎仅加载一次。
    ///  - 经 stdin/stdout 行式 JSON 收图/返回，请求间进程常驻、复用引擎（消除每次重加载 + 原生初始化抖动）。
    ///  - 进程退出 / 请求超时 → 自动重启；彻底起不来 → 回退 legacy LegacyOcr（永不丢失 OCR 能力）。
    /// </summary>
    private sealed class OcrWorkerClient
    {
        private const int ReadyTimeoutMs = 45_000;
        private const int ReqTimeoutMs = 60_000;

        private readonly OcrService _svc;
        private Process? _proc;
        private StreamWriter? _stdin;
        private Task? _readerTask;
        private readonly object _startLock = new();
        private readonly SemaphoreSlim _sendLock = new(1, 1);
        private readonly ConcurrentDictionary<string, TaskCompletionSource<OcrResp>> _pending = new();
        private TaskCompletionSource<bool>? _readyTcs;
        private bool _startedOk;
        private volatile bool _disabled; // 启动彻底失败后降级 legacy
        private DateTime _disabledUntil = DateTime.MinValue; // 允许定时重试启动
        private const int DisabledCooldownSec = 30;  // 30s 冷却后允许重试

        // ⭐ L1 引擎锁定：引擎埋点 + 连续 fallback 自愈
        private string _lastEngine = "";
        private int _consecFallback = 0;
        private const int FallbackSelfHealThreshold = 2;  // 连续 N 次 Tesseract fallback → 重启自愈

        public OcrWorkerClient(OcrService svc) { _svc = svc; }

        public bool Enabled => !_disabled;
        /// <summary>L1 引擎锁定：最近一次 OCR 实际使用的引擎（rapidocr / rapidocr_roi / tesseract_fallback）。</summary>
        public string LastEngine => _lastEngine;
        /// <summary>L1 引擎锁定：连续 Tesseract fallback 计数（用于自愈监控）。</summary>
        public int ConsecutiveFallback => _consecFallback;

        /// <returns>(Text, Failed)：Text=识别文本；Failed=True 表示服务暂不可用（上传路径据此抛 OcrUnavailableException）。</returns>
        public (string Text, bool Failed) OcrOne(string imagePath)
        {
            if (_disabled) return _svc.LegacyOcr(imagePath);       // 已降级
            if (!EnsureStarted()) return _svc.LegacyOcr(imagePath); // 本次起不来，回退
            // 健康检查：上次调用后进程可能已退出
            if (_proc == null || _proc.HasExited)
            {
                if (!EnsureStarted()) return _svc.LegacyOcr(imagePath);
            }

            var id = Guid.NewGuid().ToString("N");
            var tcs = new TaskCompletionSource<OcrResp>(TaskCreationOptions.RunContinuationsAsynchronously);
            _pending[id] = tcs;

            // 串行写 stdin（多请求不交错），响应在锁外 await
            try
            {
                _sendLock.Wait();
                if (_stdin == null || _proc == null || _proc.HasExited)
                {
                    _pending.TryRemove(id, out _);
                    return _svc.LegacyOcr(imagePath);
                }
                var req = JsonSerializer.Serialize(new { id, image = imagePath, json = true });
                _stdin.WriteLine(req);
            }
            catch
            {
                _pending.TryRemove(id, out _);
                KillWorker();
                return _svc.LegacyOcr(imagePath);
            }
            finally
            {
                try { _sendLock.Release(); } catch { }
            }

            var timeout = Task.Delay(ReqTimeoutMs);
            var completed = Task.WhenAny(tcs.Task, timeout).GetAwaiter().GetResult();
            if (completed == timeout)
            {
                _pending.TryRemove(id, out _);
                Log($"[OCR-WORKER] req {id} TIMEOUT -> restart");
                KillWorker();                  // 超时：杀掉重建
                return ("", true);           // 服务暂不可用（上传路径抛 OcrUnavailableException）
            }
            var resp = tcs.Task.GetAwaiter().GetResult();
            if (resp.Error != null)
            {
                if (resp.Error == "file_not_found") return ("", false);
                Log($"[OCR-WORKER] req {id} error={resp.Error} -> restart");
                KillWorker();
                return ("", true);
            }
            // ⭐ L1 引擎锁定：引擎埋点 + 连续 Tesseract fallback 自愈
            _lastEngine = resp.Engine;
            if (resp.Engine == "tesseract_fallback")
            {
                _consecFallback++;
                Log($"[OCR-WORKER] req {id} ENGINE={resp.Engine} consec_fallback={_consecFallback}");
                if (_consecFallback >= FallbackSelfHealThreshold)
                {
                    Log($"[OCR-WORKER] consecutive Tesseract fallback x{_consecFallback} -> kill worker to recover RapidOCR");
                    _consecFallback = 0;
                    KillWorker();   // 重建后重新 warm-up，恢复 RapidOCR
                }
            }
            else
            {
                if (_consecFallback != 0) Log($"[OCR-WORKER] engine recovered ({resp.Engine})");
                _consecFallback = 0;
            }
            return (resp.Text ?? "", false);
        }

        /// <summary>
        /// ⭐ 合并任务：在常驻 worker 进程内直接跑 diff_visualizer.run_on_photo（RapidOCR 已加载→零重载），
        /// 一次调用完成「整图 OCR + 照片标示 + 返回 photoText」，避免 fresh subprocess 重复加载引擎。
        /// 返回 (PayloadJson, Failed, Error)。PayloadJson 为 run_on_photo 的结果 JSON（含 photoText / photo / blocks）。
        /// </summary>
        public (string? PayloadJson, bool Failed, string? Error) CombinedDiff(string imagePath, string did, string output, string? db, string? seg)
        {
            if (_disabled) return (null, true, "worker_disabled");
            if (!EnsureStarted()) return (null, true, "start_failed");
            if (_proc == null || _proc.HasExited)
            {
                if (!EnsureStarted()) return (null, true, "start_failed");
            }

            var id = Guid.NewGuid().ToString("N");
            var tcs = new TaskCompletionSource<OcrResp>(TaskCreationOptions.RunContinuationsAsynchronously);
            _pending[id] = tcs;

            try
            {
                _sendLock.Wait();
                if (_stdin == null || _proc == null || _proc.HasExited)
                {
                    _pending.TryRemove(id, out _);
                    return (null, true, "no_stdin");
                }
                var req = JsonSerializer.Serialize(new { id, mode = "combined", image = imagePath, did, output, db = db ?? "", seg = seg ?? "" });
                _stdin.WriteLine(req);
            }
            catch
            {
                _pending.TryRemove(id, out _);
                KillWorker();
                return (null, true, "write_fail");
            }
            finally
            {
                try { _sendLock.Release(); } catch { }
            }

            var timeout = Task.Delay(ReqTimeoutMs);
            var completed = Task.WhenAny(tcs.Task, timeout).GetAwaiter().GetResult();
            if (completed == timeout)
            {
                _pending.TryRemove(id, out _);
                Log($"[OCR-WORKER] combined req {id} TIMEOUT -> restart");
                KillWorker();
                return (null, true, "timeout");
            }
            var resp = tcs.Task.GetAwaiter().GetResult();
            if (resp.Error != null)
            {
                // file_not_found 等可恢复错误不杀 worker；其它错误杀掉重建
                if (resp.Error != "file_not_found")
                {
                    Log($"[OCR-WORKER] combined req {id} error={resp.Error} -> restart");
                    KillWorker();
                }
                return (null, true, resp.Error);
            }
            return (resp.PayloadJson, false, null);
        }

        private bool EnsureStarted()
        {
            lock (_startLock)
            {
                if (_proc != null && !_proc.HasExited && _startedOk) return true;
                // 若在禁用冷却期，跳过重试（避免每次请求都尝试启动失败的 worker）
                if (_disabled && DateTime.Now < _disabledUntil) return false;
                // 冷却已过 → 允许重试，清除禁用标记
                if (_disabled) {
                    _disabled = false;
                    Log($"[OCR-WORKER] retrying after cooldown");
                }
                // 真正拉起（持锁；期间其它线程阻塞在锁上，避免拉起多个进程）
                try
                {
                    var psi = _svc.CreatePythonPsi(_svc.WorkerScriptPath, Array.Empty<string>());
                    psi.RedirectStandardInput = true; // worker 从 stdin 收请求（覆盖 CreatePythonPsi 的 false）
                    var p = new Process { StartInfo = psi, EnableRaisingEvents = true };
                    p.Start();
                    _stdin = p.StandardInput;   // 已是 StreamWriter
                    _stdin.AutoFlush = true;
                    _proc = p;
                    _pending.Clear();
                    _readyTcs = new TaskCompletionSource<bool>();
                    _readerTask = Task.Run(() => ReaderLoop(p));
                    _startedOk = false;

                    // 等待 __ready__（带超时）；同时轮询进程是否提前退出
                    var sw = Stopwatch.StartNew();
                    bool ready = false;
                    while (sw.ElapsedMilliseconds < ReadyTimeoutMs)
                    {
                        if (_readyTcs.Task.IsCompleted)
                        {
                            ready = _readyTcs.Task.GetAwaiter().GetResult();
                            break;
                        }
                        if (p.HasExited) { ready = false; break; }
                        Thread.Sleep(50);
                    }
                    if (!ready)
                    {
                        Log($"[OCR-WORKER] start FAILED (ready timeout / exit)");
                        TryDisposeStdin();
                        KillWorker();
                        _disabled = true;
                        _disabledUntil = DateTime.Now.AddSeconds(DisabledCooldownSec);  // 冷却后允许重试
                        return false;
                    }
                    _startedOk = true;
                    Log($"[OCR-WORKER] started pid={p.Id}");
                    return true;
                }
                catch (Exception ex)
                {
                    Log($"[OCR-WORKER] start EXC {ex.GetType().Name}:{ex.Message}");
                    _disabled = true;
                    _disabledUntil = DateTime.Now.AddSeconds(DisabledCooldownSec);
                    return false;
                }
            }
        }

        private void ReaderLoop(Process p)
        {
            try
            {
                string? line;
                while ((line = p.StandardOutput.ReadLine()) != null)
                {
                    line = line.Trim();
                    if (line.Length == 0) continue;
                    try
                    {
                        using var doc = JsonDocument.Parse(line);
                        var root = doc.RootElement;
                        if (root.TryGetProperty("__ready__", out var rv) && rv.ValueKind == JsonValueKind.True)
                        {
                            _readyTcs?.TrySetResult(true);
                            continue;
                        }
                        if (root.TryGetProperty("id", out var idEl) && idEl.ValueKind == JsonValueKind.String)
                        {
                            var id = idEl.GetString()!;
                            if (_pending.TryRemove(id, out var tcs))
                            {
                                var resp = new OcrResp
                                {
                                    Text = root.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "",
                                    Conf = root.TryGetProperty("conf", out var c) ? c.GetDouble() : 0,
                                    ModelCandidate = root.TryGetProperty("model_candidate", out var m) && m.ValueKind == JsonValueKind.True,
                                    Engine = root.TryGetProperty("engine", out var e) ? e.GetString() ?? "" : "",
                                    Error = root.TryGetProperty("error", out var er) && er.ValueKind == JsonValueKind.String ? er.GetString() : null,
                                    PayloadJson = root.TryGetProperty("payload", out var pl) && pl.ValueKind != JsonValueKind.Null ? pl.GetRawText() : null,
                                };
                                tcs.TrySetResult(resp);
                            }
                        }
                    }
                    catch { /* 丢弃畸形行 */ }
                }
            }
            catch { }
            // 进程退出 / stdin EOF
            _readyTcs?.TrySetResult(false);
            foreach (var kv in _pending.ToArray())
            {
                _pending.TryRemove(kv.Key, out _);
                kv.Value.TrySetResult(new OcrResp { Error = "worker_exited" });
            }
        }

        private void KillWorker()
        {
            try { _proc?.Kill(); } catch { }
            TryDisposeStdin();
            _proc = null;
            _stdin = null;
            _startedOk = false;
            _pending.Clear();
        }

        private void TryDisposeStdin()
        {
            try { _stdin?.Dispose(); } catch { }
            _stdin = null;
        }

        private void Log(string msg)
        {
            try { File.AppendAllText(Path.Combine(_svc._ocrWork, "ocr_worker.log"),
                $"[{DateTime.Now:HH:mm:ss}] {msg}\n"); } catch { }
        }
    }

    /// <summary>常驻 worker 的单次 OCR 响应。</summary>
    private sealed class OcrResp
    {
        public string Text = "";
        public double Conf;
        public bool ModelCandidate;
        public string Engine = "";
        public string? Error;
        // ⭐ combined 任务（diff_visualizer.run_on_photo）返回的原始 JSON 载荷（含 photoText / photo / blocks）。
        public string? PayloadJson;
    }
}
