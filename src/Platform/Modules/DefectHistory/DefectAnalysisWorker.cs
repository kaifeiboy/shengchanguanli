namespace Platform.Modules.DefectHistory;

/// <summary>
/// 每月 2 号 09:00 后自动生成上月全型号不良分析报告（analysis_auto，幂等）。
/// 每小时检查一次：命中（2 号 + ≥9 点 + 上月报告未生成）才执行，其余时间零开销。
/// 报告写入 {root}/_analysis/YYYY年M月份不良分析报告.xlsx，H5 顶部提醒条通过 /analysis/status 获取。
/// </summary>
public sealed class DefectAnalysisWorker : BackgroundService
{
    private readonly DefectHistoryService _svc;
    private readonly ILogger<DefectAnalysisWorker> _log;

    public DefectAnalysisWorker(DefectHistoryService svc, ILogger<DefectAnalysisWorker> log)
    {
        _svc = svc;
        _log = log;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // 启动时先跑一次（覆盖服务器在 2 号期间重启的情况）
        try { CheckAndRun(); }
        catch (Exception e) { _log.LogError("不良分析定时任务启动检查失败: {msg}", e.Message); }

        while (!stoppingToken.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromHours(1), stoppingToken); }
            catch (OperationCanceledException) { break; }
            try { CheckAndRun(); }
            catch (Exception e) { _log.LogError("不良分析定时任务失败: {msg}", e.Message); }
        }
    }

    private void CheckAndRun()
    {
        var now = DateTime.Now;
        if (now.Day != 2 || now.Hour < 9) return;   // 仅每月 2 号 09:00 后执行

        var lastMonth = now.AddMonths(-1).ToString("yyyy-MM");
        var (stMonth, stFile, available) = _svc.AnalysisStatusInternal();
        if (available && stMonth == lastMonth && !string.IsNullOrEmpty(stFile) && File.Exists(stFile))
        {
            _log.LogInformation("上月（{month}）分析报告已存在，跳过自动生成", lastMonth);
            return;
        }
        var res = _svc.AutoAnalysis();
        _log.LogInformation("自动生成上月不良分析：month={month} file={file} already={already}",
            res.GetType().GetProperty("month")?.GetValue(res),
            res.GetType().GetProperty("file")?.GetValue(res),
            res.GetType().GetProperty("already")?.GetValue(res));
    }
}
