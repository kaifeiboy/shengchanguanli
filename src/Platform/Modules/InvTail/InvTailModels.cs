namespace Platform.Modules.InvTail;

/// <summary>
/// 库存尾数模块（v2 全新重建）领域模型 / DTO / 类型化异常。
/// 该模块完全独立：独立 SQLite 库（invtail.db）、独立 Python 脚本、独立 Python 进程工厂，
/// 不复用也不依赖平台内其它模块的任何服务，确保改动不会波及其它模块。
/// </summary>

/// <summary>出入库方向。</summary>
public static class InvDirection
{
    public const string In = "in";
    public const string Out = "out";

    public static string Normalize(string? raw)
    {
        var s = (raw ?? "").Trim().ToLowerInvariant();
        return s switch
        {
            "in" or "入" or "入库" or "i" => In,
            "out" or "出" or "出库" or "o" => Out,
            _ => throw new InvTailException("方向不合法，仅支持 入库(in) / 出库(out)")
        };
    }

    public static string ToLabel(string dir) => dir == Out ? "出库" : "入库";
}

/// <summary>材料主档（库号 / 编码 / 材料信息，库号与编码各自唯一）。</summary>
public sealed class InvMaterial
{
    public long Id { get; set; }
    public string BinNo { get; set; } = "";
    public string Code { get; set; } = "";
    public string MaterialInfo { get; set; } = "";
    public string CreatedAt { get; set; } = "";
    public string CreatedBy { get; set; } = "";
    /// <summary>由流水累加得出的当前库存（入库 +，出库 -）。</summary>
    public double Stock { get; set; }
    /// <summary>流水笔数。</summary>
    public int RecordCount { get; set; }
    /// <summary>最近一次出入库时间（无流水则空）。</summary>
    public string LastMovedAt { get; set; } = "";
    /// <summary>最近一次经手人。</summary>
    public string LastHandler { get; set; } = "";
}

/// <summary>出入库流水（不可变事实记录；库存永远由此累加得出）。</summary>
public sealed class InvRecord
{
    public long Id { get; set; }
    public long MaterialId { get; set; }
    public string Direction { get; set; } = InvDirection.In;
    public double Qty { get; set; }
    public string Handler { get; set; } = "";
    public string Note { get; set; } = "";
    public string OccurredAt { get; set; } = "";
    /// <summary>该笔之后的结存（查询时按时间顺序滚动计算）。</summary>
    public double Balance { get; set; }
    // 导出用冗余字段
    public string BinNo { get; set; } = "";
    public string Code { get; set; } = "";
    public string MaterialInfo { get; set; } = "";
}

/// <summary>新增材料请求。</summary>
public sealed class InvCreateRequest
{
    public string? BinNo { get; set; }
    public string? Code { get; set; }
    public string? MaterialInfo { get; set; }
    /// <summary>初始数量：记为该材料的首笔入库流水（经手人 = 新增人）。</summary>
    public double Qty { get; set; }
    public string? Handler { get; set; }
    public string? Note { get; set; }
}

/// <summary>出入库请求。</summary>
public sealed class InvMoveRequest
{
    public string? Direction { get; set; }
    public double Qty { get; set; }
    public string? Handler { get; set; }
    public string? Note { get; set; }
}

/// <summary>业务错误（模块层统一捕获转 400，客户端只看到规范化错误结构）。</summary>
public class InvTailException : Exception
{
    public InvTailException(string message) : base(message) { }
}

/// <summary>唯一性冲突（库号或编码已存在）——携带冲突材料，供 H5 直接跳转做出入库。</summary>
public sealed class InvDuplicateException : InvTailException
{
    /// <summary>冲突字段：binNo / code。</summary>
    public string Field { get; }
    public InvMaterial Existing { get; }

    public InvDuplicateException(string field, InvMaterial existing, string message) : base(message)
    {
        Field = field;
        Existing = existing;
    }
}

/// <summary>库存不足（出库数量大于当前库存，拦截，库存永不为负）。</summary>
public sealed class InvStockShortageException : InvTailException
{
    public double Stock { get; }
    public double Requested { get; }

    public InvStockShortageException(double stock, double requested, string message) : base(message)
    {
        Stock = stock;
        Requested = requested;
    }
}
