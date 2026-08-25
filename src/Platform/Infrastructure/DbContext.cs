using Microsoft.Data.Sqlite;

namespace Platform.Infrastructure;

/// <summary>
/// 平台核心库（SQLite）。采用手写 SQL，避免 EF Core 复杂度，依赖最少、最稳。
/// 后续模块可在各自库或此核心库中按需建表。
/// </summary>
public class DbContext
{
    private readonly string _connString;

    public DbContext(IConfiguration config)
    {
        var dbPath = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
        var dir = Path.GetDirectoryName(dbPath);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        _connString = $"Data Source={dbPath}";
    }

    public void Initialize()
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            CREATE TABLE IF NOT EXISTS drawings (
                Id          INTEGER PRIMARY KEY AUTOINCREMENT,
                Model       TEXT,
                FileName    TEXT,
                VirtualPath TEXT,
                OcrText     TEXT,
                CreatedAt   TEXT
            );
            CREATE TABLE IF NOT EXISTS drawing_parts (
                Id          INTEGER PRIMARY KEY AUTOINCREMENT,
                DrawingId   INTEGER,
                PartName    TEXT,
                PartText    TEXT,
                CreatedAt   TEXT
            );
            CREATE TABLE IF NOT EXISTS drawing_histories (
                Id            INTEGER PRIMARY KEY AUTOINCREMENT,
                QueryType     TEXT,
                QueryText     TEXT,
                DetectedModel TEXT,
                DetectedPart  TEXT,
                DrawingId     INTEGER,
                Matched       INTEGER,
                CreatedAt     TEXT
            );
            CREATE TABLE IF NOT EXISTS drawing_blocks (
                Id          INTEGER PRIMARY KEY AUTOINCREMENT,
                DrawingId   INTEGER,
                BIdx        INTEGER,
                X           INTEGER,
                Y           INTEGER,
                W           INTEGER,
                H           INTEGER,
                FileRel     TEXT,
                Tokens      TEXT,
                RawText     TEXT,
                CreatedAt   TEXT
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                Id        INTEGER PRIMARY KEY AUTOINCREMENT,
                CreatedAt TEXT,
                Payload   TEXT
            );
        ";
        cmd.ExecuteNonQuery();

        // 兼容旧库：drawings 可能已存在但缺少 OcrText 列
        try
        {
            using var a = conn.CreateCommand();
            a.CommandText = "ALTER TABLE drawings ADD COLUMN OcrText TEXT;";
            a.ExecuteNonQuery();
        }
        catch { /* 列已存在则忽略 */ }

        // 兼容旧库：drawing_histories 可能已存在但缺少 SnapshotPath 列（任务3 快照入库）
        try
        {
            using var a = conn.CreateCommand();
            a.CommandText = "ALTER TABLE drawing_histories ADD COLUMN SnapshotPath TEXT;";
            a.ExecuteNonQuery();
        }
        catch { /* 列已存在则忽略 */ }
    }

    /// <summary>每次操作开新连接，规避 SQLite 多线程共享连接的问题。</summary>
    public SqliteConnection Open()
    {
        var c = new SqliteConnection(_connString);
        c.Open();
        return c;
    }
}
