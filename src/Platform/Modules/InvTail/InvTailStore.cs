using Microsoft.Data.Sqlite;

namespace Platform.Modules.InvTail;

/// <summary>
/// 库存尾数仓储层（唯一的数据访问出口）。
/// <para>独立性：使用自己的 SQLite 数据库文件（默认 {dataDir}/invtail.db），
/// 与平台共享库 app.db 完全隔离——本模块的建表、迁移、写入都不会触碰其它模块的数据。</para>
/// <para>库存不落库：materials 只存主档，库存一律由 records 流水实时累加（入 +，出 -），
/// 保证账实一致、可追溯，不存在"库存字段与流水不符"的脏数据可能。</para>
/// </summary>
public sealed class InvTailStore
{
    private readonly string _connStr;
    private static readonly object _initLock = new();
    private static bool _initialized;

    public string DbPath { get; }
    public string DataDir { get; }

    public InvTailStore(IConfiguration config)
    {
        // 独立库：优先读 InvTail:DbPath；否则放在平台 data 目录下（与 app.db 同目录但不同文件）
        var configured = config["InvTail:DbPath"];
        if (!string.IsNullOrWhiteSpace(configured))
        {
            DbPath = configured!;
        }
        else
        {
            var appDb = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
            var dir = Path.GetDirectoryName(appDb);
            if (string.IsNullOrWhiteSpace(dir)) dir = AppContext.BaseDirectory;
            DbPath = Path.Combine(dir!, "invtail.db");
        }
        DataDir = Path.GetDirectoryName(DbPath) ?? AppContext.BaseDirectory;
        try { Directory.CreateDirectory(DataDir); } catch { }
        _connStr = new SqliteConnectionStringBuilder
        {
            DataSource = DbPath,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Shared
        }.ToString();
        Initialize();
    }

    private SqliteConnection Open()
    {
        var c = new SqliteConnection(_connStr);
        c.Open();
        using (var pragma = c.CreateCommand())
        {
            // WAL + busy_timeout：H5 端并发读写（查询同时有人出入库）时不因锁失败
            pragma.CommandText = "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;";
            pragma.ExecuteNonQuery();
        }
        return c;
    }

    /// <summary>建表（幂等）。全新库，无需兼容旧结构。</summary>
    public void Initialize()
    {
        if (_initialized) return;
        lock (_initLock)
        {
            if (_initialized) return;
            using var c = Open();
            using var cmd = c.CreateCommand();
            cmd.CommandText = @"
CREATE TABLE IF NOT EXISTS materials (
    Id            INTEGER PRIMARY KEY AUTOINCREMENT,
    BinNo         TEXT    NOT NULL,
    Code          TEXT    NOT NULL,
    MaterialInfo  TEXT    NOT NULL,
    CreatedAt     TEXT    NOT NULL,
    CreatedBy     TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_invtail_materials_binno ON materials(BinNo);
CREATE UNIQUE INDEX IF NOT EXISTS ux_invtail_materials_code  ON materials(Code);
CREATE INDEX IF NOT EXISTS ix_invtail_materials_info ON materials(MaterialInfo);

CREATE TABLE IF NOT EXISTS records (
    Id          INTEGER PRIMARY KEY AUTOINCREMENT,
    MaterialId  INTEGER NOT NULL REFERENCES materials(Id) ON DELETE CASCADE,
    Direction   TEXT    NOT NULL,
    Qty         REAL    NOT NULL,
    Handler     TEXT    NOT NULL DEFAULT '',
    Note        TEXT    NOT NULL DEFAULT '',
    OccurredAt  TEXT    NOT NULL,
    CreatedAt   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invtail_records_mat  ON records(MaterialId, Id);
CREATE INDEX IF NOT EXISTS ix_invtail_records_time ON records(OccurredAt DESC);

CREATE TABLE IF NOT EXISTS handlers (
    Name        TEXT PRIMARY KEY,
    LastUsedAt  TEXT NOT NULL,
    UseCount    INTEGER NOT NULL DEFAULT 0
);
";
            cmd.ExecuteNonQuery();
            _initialized = true;
        }
    }

    // ---------------- 材料主档 ----------------

    private const string MaterialSelect = @"
SELECT m.Id, m.BinNo, m.Code, m.MaterialInfo, m.CreatedAt, m.CreatedBy,
       COALESCE((SELECT SUM(CASE WHEN r.Direction='out' THEN -r.Qty ELSE r.Qty END)
                 FROM records r WHERE r.MaterialId = m.Id), 0)            AS Stock,
       COALESCE((SELECT COUNT(*) FROM records r WHERE r.MaterialId = m.Id), 0) AS Cnt,
       COALESCE((SELECT r.OccurredAt FROM records r WHERE r.MaterialId = m.Id
                 ORDER BY r.Id DESC LIMIT 1), '')                          AS LastAt,
       COALESCE((SELECT r.Handler FROM records r WHERE r.MaterialId = m.Id
                 ORDER BY r.Id DESC LIMIT 1), '')                          AS LastHandler
FROM materials m";

    private static InvMaterial ReadMaterial(SqliteDataReader r) => new()
    {
        Id = r.GetInt64(0),
        BinNo = r.GetString(1),
        Code = r.GetString(2),
        MaterialInfo = r.GetString(3),
        CreatedAt = r.GetString(4),
        CreatedBy = r.GetString(5),
        Stock = r.GetDouble(6),
        RecordCount = r.GetInt32(7),
        LastMovedAt = r.GetString(8),
        LastHandler = r.GetString(9)
    };

    /// <summary>按 Id 取材料（含库存）。不存在返回 null。</summary>
    public InvMaterial? GetMaterial(long id)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = MaterialSelect + " WHERE m.Id = @id";
        cmd.Parameters.AddWithValue("@id", id);
        using var r = cmd.ExecuteReader();
        return r.Read() ? ReadMaterial(r) : null;
    }

    /// <summary>按库号精确取材料（唯一约束）。</summary>
    public InvMaterial? GetByBinNo(string binNo)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = MaterialSelect + " WHERE m.BinNo = @v";
        cmd.Parameters.AddWithValue("@v", binNo);
        using var r = cmd.ExecuteReader();
        return r.Read() ? ReadMaterial(r) : null;
    }

    /// <summary>按编码精确取材料（唯一约束）。</summary>
    public InvMaterial? GetByCode(string code)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = MaterialSelect + " WHERE m.Code = @v";
        cmd.Parameters.AddWithValue("@v", code);
        using var r = cmd.ExecuteReader();
        return r.Read() ? ReadMaterial(r) : null;
    }

    /// <summary>
    /// 模糊查询：关键词同时匹配 库号 / 编码 / 材料信息（任一命中）。
    /// 关键词为空则返回全部（按最近操作时间倒序）。
    /// </summary>
    public (List<InvMaterial> rows, int total) SearchMaterials(string? keyword, int page, int size)
    {
        var kw = (keyword ?? "").Trim();
        var where = "";
        if (kw.Length > 0)
            where = " WHERE (m.BinNo LIKE @kw OR m.Code LIKE @kw OR m.MaterialInfo LIKE @kw)";

        using var c = Open();
        int total;
        using (var cnt = c.CreateCommand())
        {
            cnt.CommandText = "SELECT COUNT(*) FROM materials m" + where;
            if (kw.Length > 0) cnt.Parameters.AddWithValue("@kw", "%" + kw + "%");
            total = Convert.ToInt32(cnt.ExecuteScalar() ?? 0);
        }

        if (page < 1) page = 1;
        if (size < 1) size = 20;
        if (size > 500) size = 500;

        var rows = new List<InvMaterial>();
        using (var cmd = c.CreateCommand())
        {
            // 最近有出入库操作的排前面（LastAt 空的新料按创建时间兜底）
            cmd.CommandText = MaterialSelect + where +
                " ORDER BY (CASE WHEN LastAt = '' THEN m.CreatedAt ELSE LastAt END) DESC, m.Id DESC" +
                " LIMIT @size OFFSET @off";
            if (kw.Length > 0) cmd.Parameters.AddWithValue("@kw", "%" + kw + "%");
            cmd.Parameters.AddWithValue("@size", size);
            cmd.Parameters.AddWithValue("@off", (page - 1) * size);
            using var r = cmd.ExecuteReader();
            while (r.Read()) rows.Add(ReadMaterial(r));
        }
        return (rows, total);
    }

    /// <summary>
    /// 新增材料 + 首笔入库流水（同一事务，要么都成功要么都不写）。
    /// 唯一性冲突由调用方（服务层）先行检查并给出友好提示；这里的 UNIQUE 索引是最后一道防线。
    /// </summary>
    public long InsertMaterialWithOpeningRecord(
        string binNo, string code, string materialInfo,
        double qty, string handler, string note, string nowText)
    {
        using var c = Open();
        using var tx = c.BeginTransaction();
        long id;
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = @"INSERT INTO materials(BinNo, Code, MaterialInfo, CreatedAt, CreatedBy)
                                VALUES(@b, @c, @m, @t, @by);
                                SELECT last_insert_rowid();";
            cmd.Parameters.AddWithValue("@b", binNo);
            cmd.Parameters.AddWithValue("@c", code);
            cmd.Parameters.AddWithValue("@m", materialInfo);
            cmd.Parameters.AddWithValue("@t", nowText);
            cmd.Parameters.AddWithValue("@by", handler);
            id = Convert.ToInt64(cmd.ExecuteScalar() ?? 0L);
        }
        if (qty > 0)
        {
            using var cmd = c.CreateCommand();
            cmd.Transaction = tx;
            cmd.CommandText = @"INSERT INTO records(MaterialId, Direction, Qty, Handler, Note, OccurredAt, CreatedAt)
                                VALUES(@mid, 'in', @q, @h, @n, @t, @t)";
            cmd.Parameters.AddWithValue("@mid", id);
            cmd.Parameters.AddWithValue("@q", qty);
            cmd.Parameters.AddWithValue("@h", handler);
            cmd.Parameters.AddWithValue("@n", string.IsNullOrWhiteSpace(note) ? "新增建档（首笔入库）" : note);
            cmd.Parameters.AddWithValue("@t", nowText);
            cmd.ExecuteNonQuery();
        }
        tx.Commit();
        return id;
    }

    /// <summary>更新材料主档（库号/编码/材料信息）。唯一性由服务层先查 + 索引兜底。</summary>
    public void UpdateMaterial(long id, string binNo, string code, string materialInfo)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "UPDATE materials SET BinNo=@b, Code=@c, MaterialInfo=@m WHERE Id=@id";
        cmd.Parameters.AddWithValue("@b", binNo);
        cmd.Parameters.AddWithValue("@c", code);
        cmd.Parameters.AddWithValue("@m", materialInfo);
        cmd.Parameters.AddWithValue("@id", id);
        cmd.ExecuteNonQuery();
    }

    /// <summary>删除材料及其全部流水（外键级联；误录建档纠正用）。</summary>
    public int DeleteMaterial(long id)
    {
        using var c = Open();
        using var tx = c.BeginTransaction();
        using (var d1 = c.CreateCommand())
        {
            d1.Transaction = tx;
            d1.CommandText = "DELETE FROM records WHERE MaterialId=@id";
            d1.Parameters.AddWithValue("@id", id);
            d1.ExecuteNonQuery();
        }
        int n;
        using (var d2 = c.CreateCommand())
        {
            d2.Transaction = tx;
            d2.CommandText = "DELETE FROM materials WHERE Id=@id";
            d2.Parameters.AddWithValue("@id", id);
            n = d2.ExecuteNonQuery();
        }
        tx.Commit();
        return n;
    }

    // ---------------- 出入库流水 ----------------

    /// <summary>当前库存 = 流水累加（入 +，出 -）。</summary>
    public double GetStock(long materialId)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"SELECT COALESCE(SUM(CASE WHEN Direction='out' THEN -Qty ELSE Qty END), 0)
                            FROM records WHERE MaterialId=@id";
        cmd.Parameters.AddWithValue("@id", materialId);
        return Convert.ToDouble(cmd.ExecuteScalar() ?? 0d);
    }

    /// <summary>
    /// 记一笔出入库。出库超量的拦截在同一事务内二次校验（防并发下两人同时出库把库存做负）。
    /// 返回 (流水Id, 记账后库存)。
    /// </summary>
    public (long recordId, double stock) InsertRecord(
        long materialId, string direction, double qty, string handler, string note, string nowText)
    {
        using var c = Open();
        using var tx = c.BeginTransaction();

        double stock;
        using (var q = c.CreateCommand())
        {
            q.Transaction = tx;
            q.CommandText = @"SELECT COALESCE(SUM(CASE WHEN Direction='out' THEN -Qty ELSE Qty END), 0)
                              FROM records WHERE MaterialId=@id";
            q.Parameters.AddWithValue("@id", materialId);
            stock = Convert.ToDouble(q.ExecuteScalar() ?? 0d);
        }
        if (direction == InvDirection.Out && qty - stock > 1e-9)
            throw new InvStockShortageException(stock, qty,
                $"出库数量 {InvNum.Fmt(qty)} 超过当前库存 {InvNum.Fmt(stock)}，已拦截（库存不允许为负）");

        long rid;
        using (var cmd = c.CreateCommand())
        {
            cmd.Transaction = tx;
            cmd.CommandText = @"INSERT INTO records(MaterialId, Direction, Qty, Handler, Note, OccurredAt, CreatedAt)
                                VALUES(@mid, @d, @q, @h, @n, @t, @t);
                                SELECT last_insert_rowid();";
            cmd.Parameters.AddWithValue("@mid", materialId);
            cmd.Parameters.AddWithValue("@d", direction);
            cmd.Parameters.AddWithValue("@q", qty);
            cmd.Parameters.AddWithValue("@h", handler);
            cmd.Parameters.AddWithValue("@n", note ?? "");
            cmd.Parameters.AddWithValue("@t", nowText);
            rid = Convert.ToInt64(cmd.ExecuteScalar() ?? 0L);
        }
        tx.Commit();
        var after = direction == InvDirection.Out ? stock - qty : stock + qty;
        return (rid, after);
    }

    /// <summary>某材料的流水（时间正序滚动出结存后，按倒序返回给前端）。</summary>
    public List<InvRecord> ListRecords(long materialId, int limit = 500)
    {
        var asc = new List<InvRecord>();
        using (var c = Open())
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = @"SELECT Id, MaterialId, Direction, Qty, Handler, Note, OccurredAt
                                FROM records WHERE MaterialId=@id ORDER BY Id ASC";
            cmd.Parameters.AddWithValue("@id", materialId);
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                asc.Add(new InvRecord
                {
                    Id = r.GetInt64(0),
                    MaterialId = r.GetInt64(1),
                    Direction = r.GetString(2),
                    Qty = r.GetDouble(3),
                    Handler = r.GetString(4),
                    Note = r.GetString(5),
                    OccurredAt = r.GetString(6)
                });
            }
        }
        double bal = 0;
        foreach (var x in asc)
        {
            bal += x.Direction == InvDirection.Out ? -x.Qty : x.Qty;
            x.Balance = bal;
        }
        asc.Reverse();
        return limit > 0 && asc.Count > limit ? asc.GetRange(0, limit) : asc;
    }

    /// <summary>删除一笔流水（误录纠正）。返回受影响材料 Id（0=流水不存在）。</summary>
    public long DeleteRecord(long recordId)
    {
        using var c = Open();
        long mid;
        using (var q = c.CreateCommand())
        {
            q.CommandText = "SELECT MaterialId FROM records WHERE Id=@id";
            q.Parameters.AddWithValue("@id", recordId);
            var v = q.ExecuteScalar();
            if (v == null || v == DBNull.Value) return 0;
            mid = Convert.ToInt64(v);
        }
        using (var d = c.CreateCommand())
        {
            d.CommandText = "DELETE FROM records WHERE Id=@id";
            d.Parameters.AddWithValue("@id", recordId);
            d.ExecuteNonQuery();
        }
        return mid;
    }

    // ---------------- 经手人 ----------------

    /// <summary>记录经手人使用（最后操作的人下次排最前）。</summary>
    public void TouchHandler(string name, string nowText)
    {
        var n = (name ?? "").Trim();
        if (n.Length == 0) return;
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"INSERT INTO handlers(Name, LastUsedAt, UseCount) VALUES(@n, @t, 1)
                            ON CONFLICT(Name) DO UPDATE SET LastUsedAt=@t, UseCount=UseCount+1";
        cmd.Parameters.AddWithValue("@n", n);
        cmd.Parameters.AddWithValue("@t", nowText);
        cmd.ExecuteNonQuery();
    }

    /// <summary>经手人下拉数据：最后操作的人排最前。</summary>
    public List<(string name, string lastUsedAt, int useCount)> ListHandlers(int limit = 50)
    {
        var list = new List<(string, string, int)>();
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT Name, LastUsedAt, UseCount FROM handlers ORDER BY LastUsedAt DESC LIMIT @n";
        cmd.Parameters.AddWithValue("@n", limit < 1 ? 50 : limit);
        using var r = cmd.ExecuteReader();
        while (r.Read()) list.Add((r.GetString(0), r.GetString(1), r.GetInt32(2)));
        return list;
    }

    /// <summary>删除经手人：仅从下拉候选移除，历史流水中的名字原样保留（不动已记账数据）。</summary>
    public int DeleteHandler(string name)
    {
        var n = (name ?? "").Trim();
        if (n.Length == 0) return 0;
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "DELETE FROM handlers WHERE Name=@n";
        cmd.Parameters.AddWithValue("@n", n);
        return cmd.ExecuteNonQuery();
    }

    /// <summary>经手人改名；syncRecords=true 时同步把历史流水中的旧名改为新名。</summary>
    public bool RenameHandler(string oldName, string newName, bool syncRecords)
    {
        var o = (oldName ?? "").Trim();
        var nw = (newName ?? "").Trim();
        if (o.Length == 0 || nw.Length == 0 || string.Equals(o, nw, StringComparison.Ordinal)) return false;
        using var c = Open();
        using var tx = c.BeginTransaction();
        try
        {
            if (syncRecords)
            {
                using var ur = c.CreateCommand();
                ur.Transaction = tx;
                ur.CommandText = "UPDATE records SET Handler=@nw WHERE Handler=@o";
                ur.Parameters.AddWithValue("@nw", nw);
                ur.Parameters.AddWithValue("@o", o);
                ur.ExecuteNonQuery();
            }

            // 新名字已存在则合并（保留较新的使用时间、累计次数），否则直接改名
            long exists;
            using (var chk = c.CreateCommand())
            {
                chk.Transaction = tx;
                chk.CommandText = "SELECT COUNT(*) FROM handlers WHERE Name=@nw";
                chk.Parameters.AddWithValue("@nw", nw);
                exists = Convert.ToInt64(chk.ExecuteScalar() ?? 0);
            }
            if (exists > 0)
            {
                using var mg = c.CreateCommand();
                mg.Transaction = tx;
                mg.CommandText = @"UPDATE handlers
                                   SET UseCount = UseCount + COALESCE((SELECT UseCount FROM handlers WHERE Name=@o), 0),
                                       LastUsedAt = MAX(LastUsedAt, COALESCE((SELECT LastUsedAt FROM handlers WHERE Name=@o), LastUsedAt))
                                   WHERE Name=@nw";
                mg.Parameters.AddWithValue("@nw", nw);
                mg.Parameters.AddWithValue("@o", o);
                mg.ExecuteNonQuery();

                using var dl = c.CreateCommand();
                dl.Transaction = tx;
                dl.CommandText = "DELETE FROM handlers WHERE Name=@o";
                dl.Parameters.AddWithValue("@o", o);
                dl.ExecuteNonQuery();
            }
            else
            {
                using var up = c.CreateCommand();
                up.Transaction = tx;
                up.CommandText = "UPDATE handlers SET Name=@nw WHERE Name=@o";
                up.Parameters.AddWithValue("@nw", nw);
                up.Parameters.AddWithValue("@o", o);
                up.ExecuteNonQuery();
            }
            tx.Commit();
            return true;
        }
        catch
        {
            try { tx.Rollback(); } catch { }
            throw;
        }
    }

    /// <summary>修改单笔流水的经手人：只改经手人，不动方向与数量，因此不影响库存。</summary>
    public long UpdateRecordHandler(long recordId, string handler)
    {
        using var c = Open();
        long mid;
        using (var q = c.CreateCommand())
        {
            q.CommandText = "SELECT MaterialId FROM records WHERE Id=@id";
            q.Parameters.AddWithValue("@id", recordId);
            var v = q.ExecuteScalar();
            if (v == null || v == DBNull.Value) return 0;
            mid = Convert.ToInt64(v);
        }
        using (var u = c.CreateCommand())
        {
            u.CommandText = "UPDATE records SET Handler=@h WHERE Id=@id";
            u.Parameters.AddWithValue("@h", handler);
            u.Parameters.AddWithValue("@id", recordId);
            u.ExecuteNonQuery();
        }
        return mid;
    }

    // ---------------- 统计 / 导出取数 ----------------

    /// <summary>模块概览。</summary>
    public (int materials, int records, double totalStock) Stats()
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"SELECT
            (SELECT COUNT(*) FROM materials),
            (SELECT COUNT(*) FROM records),
            (SELECT COALESCE(SUM(CASE WHEN Direction='out' THEN -Qty ELSE Qty END),0) FROM records)";
        using var r = cmd.ExecuteReader();
        if (!r.Read()) return (0, 0, 0);
        return (r.GetInt32(0), r.GetInt32(1), r.GetDouble(2));
    }

    /// <summary>
    /// 导出取数：出入库明细流水（一行一笔，含材料主档冗余列与逐笔结存）。
    /// keyword 为空 = 全部；否则按 库号/编码/材料信息 模糊过滤。
    /// 另可按经手人、方向、日期区间过滤。
    /// </summary>
    public List<InvRecord> ExportRecords(string? keyword, string? handler, string? direction,
                                         string? dateFrom, string? dateTo)
    {
        var sql = @"SELECT r.Id, r.MaterialId, r.Direction, r.Qty, r.Handler, r.Note, r.OccurredAt,
                           m.BinNo, m.Code, m.MaterialInfo
                    FROM records r JOIN materials m ON m.Id = r.MaterialId WHERE 1=1";
        var kw = (keyword ?? "").Trim();
        if (kw.Length > 0) sql += " AND (m.BinNo LIKE @kw OR m.Code LIKE @kw OR m.MaterialInfo LIKE @kw)";
        var hd = (handler ?? "").Trim();
        if (hd.Length > 0) sql += " AND r.Handler = @hd";
        var dir = (direction ?? "").Trim().ToLowerInvariant();
        if (dir == InvDirection.In || dir == InvDirection.Out) sql += " AND r.Direction = @dir";
        var df = (dateFrom ?? "").Trim();
        if (df.Length > 0) sql += " AND r.OccurredAt >= @df";
        var dt = (dateTo ?? "").Trim();
        if (dt.Length > 0) sql += " AND r.OccurredAt <= @dt";
        sql += " ORDER BY m.Id ASC, r.Id ASC";

        var rows = new List<InvRecord>();
        using (var c = Open())
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = sql;
            if (kw.Length > 0) cmd.Parameters.AddWithValue("@kw", "%" + kw + "%");
            if (hd.Length > 0) cmd.Parameters.AddWithValue("@hd", hd);
            if (dir == InvDirection.In || dir == InvDirection.Out) cmd.Parameters.AddWithValue("@dir", dir);
            if (df.Length > 0) cmd.Parameters.AddWithValue("@df", df + " 00:00:00");
            if (dt.Length > 0) cmd.Parameters.AddWithValue("@dt", dt + " 23:59:59");
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                rows.Add(new InvRecord
                {
                    Id = r.GetInt64(0),
                    MaterialId = r.GetInt64(1),
                    Direction = r.GetString(2),
                    Qty = r.GetDouble(3),
                    Handler = r.GetString(4),
                    Note = r.GetString(5),
                    OccurredAt = r.GetString(6),
                    BinNo = r.GetString(7),
                    Code = r.GetString(8),
                    MaterialInfo = r.GetString(9)
                });
            }
        }
        // 按材料分组滚动结存（SQL 已按 材料→流水Id 升序，这里线性即可）
        long cur = -1;
        double bal = 0;
        foreach (var x in rows)
        {
            if (x.MaterialId != cur) { cur = x.MaterialId; bal = 0; }
            bal += x.Direction == InvDirection.Out ? -x.Qty : x.Qty;
            x.Balance = bal;
        }
        return rows;
    }
}

/// <summary>数量格式化：库存尾数可能带小数（米/公斤），去掉无意义的尾随 0。</summary>
public static class InvNum
{
    public static string Fmt(double v)
    {
        if (Math.Abs(v - Math.Round(v)) < 1e-9) return ((long)Math.Round(v)).ToString();
        return v.ToString("0.####");
    }

    /// <summary>解析数量：必须为正数（新增/出入库都不接受 0 或负数）。</summary>
    public static double ParsePositive(double v, string label)
    {
        if (double.IsNaN(v) || double.IsInfinity(v)) throw new InvTailException($"{label}不合法");
        if (v <= 1e-9) throw new InvTailException($"{label}必须大于 0");
        if (v > 1e12) throw new InvTailException($"{label}过大");
        return Math.Round(v, 4);
    }
}
