using System.Text.Json;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Configuration;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// v2 仓储层（唯一的数据访问出口）。
///
/// <para>【独立性 —— 对应「不得影响平台及其它模块」硬规则】
/// 使用自己的 SQLite 文件（默认 {dataDir}/drawingsv2.db），与平台共享库 app.db 完全隔离。
/// 本模块的建表、迁移、写入都不触碰其它模块的数据，也不读改 drawings / drawing_blocks 等旧表。
/// 所有表名以 v2_ 前缀，与旧方案在物理层面彻底分开。</para>
///
/// <para>【与旧方案的数据模型差异 —— 对应「不继承旧 drawing_blocks 表」】
/// 旧：drawing_blocks = 物理切图得到的矩形块 + 块级过滤（KeepViewBlock），业务语义丢失。
/// 新：以「应打标对象 DrawingMark」为业务数据单元，每条都带 rule_id + span_ids/image_ids + evidence，
///     任何一条判定都能回溯到具体页、具体 span / image。</para>
///
/// <para>【为什么 Group 不单独建表】
/// Group 只是一种 mark（type=Group），其子项通过 parent_id 自关联挂在同一张表里。
/// 单表 + 自关联比"主表 + 分组表"少一次 join，且子项本身也是完整的 mark（有自己的置信度与证据）。</para>
/// </summary>
public sealed class V2Store
{
    private readonly string _connStr;
    private static readonly object InitLock = new();
    private static bool _initialized;

    public string DbPath { get; }

    public V2Store(IConfiguration config)
    {
        var configured = config["DrawingsV2:DbPath"];
        if (!string.IsNullOrWhiteSpace(configured))
        {
            DbPath = configured!;
        }
        else
        {
            var appDb = config["Database:Path"] ?? Path.Combine(AppContext.BaseDirectory, "app.db");
            var dir = Path.GetDirectoryName(appDb);
            if (string.IsNullOrWhiteSpace(dir)) dir = AppContext.BaseDirectory;
            DbPath = Path.Combine(dir!, "drawingsv2.db");
        }
        var dataDir = Path.GetDirectoryName(DbPath);
        if (!string.IsNullOrWhiteSpace(dataDir))
        {
            try { Directory.CreateDirectory(dataDir!); } catch { }
        }
        _connStr = new SqliteConnectionStringBuilder
        {
            DataSource = DbPath,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Shared
        }.ToString();
        Initialize();
    }

    /// <summary>供离线验证台使用：直接指定库文件路径。</summary>
    public V2Store(string dbPath)
    {
        DbPath = dbPath;
        var dir = Path.GetDirectoryName(dbPath);
        if (!string.IsNullOrWhiteSpace(dir))
        {
            try { Directory.CreateDirectory(dir!); } catch { }
        }
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
        using var pragma = c.CreateCommand();
        pragma.CommandText = "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;";
        pragma.ExecuteNonQuery();
        return c;
    }

    // ---------------- 建表 ----------------

    /// <summary>建表（幂等）。全新库，无需兼容旧结构。</summary>
    public void Initialize()
    {
        if (_initialized) return;
        lock (InitLock)
        {
            if (_initialized) return;
            using var c = Open();
            using var cmd = c.CreateCommand();
            cmd.CommandText = @"
CREATE TABLE IF NOT EXISTS v2_drawing_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drawing_id      INTEGER NULL,
    drawing_key     TEXT    NOT NULL,
    pdf_path        TEXT    NOT NULL DEFAULT '',
    pdf_sha256      TEXT    NOT NULL,
    vpdf_schema     TEXT    NOT NULL,
    parser_version  TEXT    NOT NULL DEFAULT '',
    decision_version TEXT   NOT NULL DEFAULT '',
    page_count      INTEGER NOT NULL DEFAULT 0,
    vision_fallback INTEGER NOT NULL DEFAULT 0,
    declared_items  TEXT    NOT NULL DEFAULT '[]',
    scopes          TEXT    NOT NULL DEFAULT '[]',
    warnings_json   TEXT    NOT NULL DEFAULT '[]',
    rule_hits       TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_profiles_key_sha
    ON v2_drawing_profiles(drawing_key, pdf_sha256);

CREATE TABLE IF NOT EXISTS v2_drawing_views (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES v2_drawing_profiles(id) ON DELETE CASCADE,
    page_index      INTEGER NOT NULL,
    width_pt        REAL    NOT NULL,
    height_pt       REAL    NOT NULL,
    orientation     TEXT    NOT NULL DEFAULT '',
    page_rotation   INTEGER NOT NULL DEFAULT 0,
    text_layer      INTEGER NOT NULL DEFAULT 0,
    span_count      INTEGER NOT NULL DEFAULT 0,
    image_count     INTEGER NOT NULL DEFAULT 0,
    source          TEXT    NULL,
    vision_fallback INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_v2_views_profile ON v2_drawing_views(profile_id, page_index);

CREATE TABLE IF NOT EXISTS v2_drawing_marks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES v2_drawing_profiles(id) ON DELETE CASCADE,
    mark_key        TEXT    NOT NULL,
    mark_type       TEXT    NOT NULL,
    view            TEXT    NOT NULL DEFAULT 'Unspecified',
    text            TEXT    NULL,
    required        INTEGER NOT NULL DEFAULT 1,
    condition_text  TEXT    NULL,
    norm_x          REAL    NULL,
    norm_y          REAL    NULL,
    norm_w          REAL    NULL,
    norm_h          REAL    NULL,
    bbox_json       TEXT    NULL,
    view_bbox       TEXT    NULL,    -- M6 修正·视图词范围：视图标签文字的 NormBbox [x,y,w,h] JSON
    direction       REAL    NULL,
    confidence      REAL    NOT NULL DEFAULT 0,
    rule_id         TEXT    NOT NULL DEFAULT '',
    page_index      INTEGER NOT NULL DEFAULT 0,
    span_ids        TEXT    NOT NULL DEFAULT '[]',
    image_ids       TEXT    NOT NULL DEFAULT '[]',
    evidence        TEXT    NULL,
    parent_id       INTEGER NULL REFERENCES v2_drawing_marks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_v2_marks_profile ON v2_drawing_marks(profile_id);
CREATE INDEX IF NOT EXISTS ix_v2_marks_parent  ON v2_drawing_marks(parent_id);

-- M5：首件比对记录（历史回溯 / 复查）。与旧 drawing_history 完全隔离。
CREATE TABLE IF NOT EXISTS v2_compare_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES v2_drawing_profiles(id) ON DELETE CASCADE,
    drawing_key     TEXT    NOT NULL DEFAULT '',
    photo_path      TEXT    NOT NULL DEFAULT '',
    photo_sha256    TEXT    NOT NULL DEFAULT '',
    photo_name      TEXT    NULL,
    usable          INTEGER NOT NULL DEFAULT 1,
    code_detector   TEXT    NOT NULL DEFAULT '',
    code_degraded   INTEGER NOT NULL DEFAULT 0,
    verify_ms       REAL    NOT NULL DEFAULT 0,
    counts_json     TEXT    NOT NULL DEFAULT '{}',
    quality_json    TEXT    NOT NULL DEFAULT '[]',
    verdicts_json   TEXT    NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_v2_records_profile ON v2_compare_records(profile_id);
CREATE INDEX IF NOT EXISTS ix_v2_records_time    ON v2_compare_records(created_at DESC);
";
            cmd.ExecuteNonQuery();
            MigrateMarkColumns(c);
            MigrateProfileColumns(c);
            MigrateRecordColumns(c);
            _initialized = true;
        }
    }

    /// <summary>
    /// 增量列迁移（幂等）：检查 <c>v2_drawing_marks</c> 是否已含 <c>view_bbox</c> 列，
    /// 缺失则 <c>ALTER TABLE ADD COLUMN</c>。使 M6 修正·视图词范围能在已有库上生效，
    /// 无需重建库；老 profile 的该列为 NULL，需重新剖析填充。
    /// </summary>
    private static void MigrateMarkColumns(SqliteConnection c)
    {
        // PRAGMA table_info(v2_drawing_marks) 每行：cid, name, type, notnull, dflt_value, pk
        var cols = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var q = c.CreateCommand())
        {
            q.CommandText = "PRAGMA table_info(v2_drawing_marks)";
            using var rd = q.ExecuteReader();
            while (rd.Read()) cols.Add(rd.GetString(1));
        }
        if (!cols.Contains("view_bbox"))
        {
            using var alter = c.CreateCommand();
            alter.CommandText = "ALTER TABLE v2_drawing_marks ADD COLUMN view_bbox TEXT NULL";
            alter.ExecuteNonQuery();
        }
        if (!cols.Contains("is_active"))
        {
            using var alter = c.CreateCommand();
            alter.CommandText = "ALTER TABLE v2_drawing_marks ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1";
            alter.ExecuteNonQuery();
        }
        if (!cols.Contains("confirmed"))
        {
            using var alter = c.CreateCommand();
            alter.CommandText = "ALTER TABLE v2_drawing_marks ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0";
            alter.ExecuteNonQuery();
        }
    }

    /// <summary>
    /// 增量列迁移（幂等）：v2_drawing_profiles 加人工复核闸门字段。
    /// status：draft(待确认)/reviewed(已确认)/archived；reviewed_* 记录确认人/时间/备注。
    /// </summary>
    private static void MigrateProfileColumns(SqliteConnection c)
    {
        var cols = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var q = c.CreateCommand())
        {
            q.CommandText = "PRAGMA table_info(v2_drawing_profiles)";
            using var rd = q.ExecuteReader();
            while (rd.Read()) cols.Add(rd.GetString(1));
        }
        void Add(string name, string def)
        {
            if (cols.Contains(name)) return;
            using var alter = c.CreateCommand();
            alter.CommandText = $"ALTER TABLE v2_drawing_profiles ADD COLUMN {name} {def}";
            alter.ExecuteNonQuery();
        }
        Add("status", "TEXT NOT NULL DEFAULT 'draft'");
        Add("reviewed_at", "TEXT NULL");
        Add("reviewed_by", "TEXT NULL");
        Add("review_note", "TEXT NULL");
    }

    /// <summary>
    /// 增量列迁移（幂等）：v2_compare_records 加会话合并 + 可追溯快照字段。
    /// session_id：同一次多部位检验的会话聚合键；marks_snapshot_json / params_snapshot_json：比对时对象与参数快照。
    /// </summary>
    private static void MigrateRecordColumns(SqliteConnection c)
    {
        var cols = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var q = c.CreateCommand())
        {
            q.CommandText = "PRAGMA table_info(v2_compare_records)";
            using var rd = q.ExecuteReader();
            while (rd.Read()) cols.Add(rd.GetString(1));
        }
        void Add(string name, string def)
        {
            if (cols.Contains(name)) return;
            using var alter = c.CreateCommand();
            alter.CommandText = $"ALTER TABLE v2_compare_records ADD COLUMN {name} {def}";
            alter.ExecuteNonQuery();
        }
        Add("session_id", "TEXT NULL");
        Add("marks_snapshot_json", "TEXT NULL");
        Add("params_snapshot_json", "TEXT NULL");
    }

    // ---------------- 写入 ----------------

    /// <summary>
    /// 保存一次剖析结果（profile + views + marks + group 子项）。
    /// 同一 (drawing_key, pdf_sha256) 重复保存时覆盖旧结果，保证"同一份图纸只有一份权威剖析"。
    /// </summary>
    public long SaveProfile(VpdfDocument doc, MarkBuilder.Result res, string pdfPath, string parserVersion)
    {
        ArgumentNullException.ThrowIfNull(doc);
        ArgumentNullException.ThrowIfNull(res);

        using var c = Open();
        using var tx = c.BeginTransaction();

        // 同 key + 同 sha → 先清旧结果（外键 ON DELETE CASCADE 会连带清 views/marks）
        using (var del = c.CreateCommand())
        {
            del.Transaction = tx;
            del.CommandText = "DELETE FROM v2_drawing_profiles WHERE drawing_key=$k AND pdf_sha256=$s";
            del.Parameters.AddWithValue("$k", res.DrawingKey);
            del.Parameters.AddWithValue("$s", doc.Source?.Sha256 ?? "");
            del.ExecuteNonQuery();
        }

        long pid;
        using (var ins = c.CreateCommand())
        {
            ins.Transaction = tx;
            ins.CommandText = @"
INSERT INTO v2_drawing_profiles
 (drawing_id, drawing_key, pdf_path, pdf_sha256, vpdf_schema, parser_version, decision_version,
  page_count, vision_fallback, declared_items, scopes, warnings_json, rule_hits, created_at)
VALUES
 ($did,$k,$p,$sha,$sch,$pv,$dv,$pc,$fb,$di,$sc,$wn,$rh,$ts);
SELECT last_insert_rowid();";
            ins.Parameters.AddWithValue("$did", DBNull.Value);
            ins.Parameters.AddWithValue("$k", res.DrawingKey);
            ins.Parameters.AddWithValue("$p", pdfPath ?? "");
            ins.Parameters.AddWithValue("$sha", doc.Source?.Sha256 ?? "");
            ins.Parameters.AddWithValue("$sch", doc.Schema ?? "");
            ins.Parameters.AddWithValue("$pv", parserVersion ?? "");
            ins.Parameters.AddWithValue("$dv", DecisionVersion);
            ins.Parameters.AddWithValue("$pc", doc.Pages?.Count ?? 0);
            ins.Parameters.AddWithValue("$fb", res.VisionFallbackRequired ? 1 : 0);
            ins.Parameters.AddWithValue("$di", Json.Str(res.DeclaredItems));
            ins.Parameters.AddWithValue("$sc", Json.Str(res.Scopes));
            ins.Parameters.AddWithValue("$wn", Json.Str(res.Warnings));
            ins.Parameters.AddWithValue("$rh", Json.Obj(res.RuleHits));
            ins.Parameters.AddWithValue("$ts", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            pid = Convert.ToInt64(ins.ExecuteScalar());
        }

        foreach (var pg in doc.Pages ?? new List<VpdfPage>())
        {
            var (w, h) = VectorPdfParser.PageSize(pg);
            using var ins = c.CreateCommand();
            ins.Transaction = tx;
            ins.CommandText = @"
INSERT INTO v2_drawing_views
 (profile_id, page_index, width_pt, height_pt, orientation, page_rotation,
  text_layer, span_count, image_count, source, vision_fallback)
VALUES ($pid,$i,$w,$h,$o,$r,$tl,$sc,$ic,$src,$fb)";
            ins.Parameters.AddWithValue("$pid", pid);
            ins.Parameters.AddWithValue("$i", pg.Index);
            ins.Parameters.AddWithValue("$w", w);
            ins.Parameters.AddWithValue("$h", h);
            ins.Parameters.AddWithValue("$o", pg.Orientation ?? "");
            ins.Parameters.AddWithValue("$r", pg.PageRotation);
            ins.Parameters.AddWithValue("$tl", pg.TextLayer?.Present == true ? 1 : 0);
            ins.Parameters.AddWithValue("$sc", pg.TextSpans?.Count ?? 0);
            ins.Parameters.AddWithValue("$ic", pg.Images?.Count ?? 0);
            ins.Parameters.AddWithValue("$src", (object?)pg.Diagnostics?.Source ?? DBNull.Value);
            ins.Parameters.AddWithValue("$fb", pg.Diagnostics?.VisionFallbackRequired == true ? 1 : 0);
            ins.ExecuteNonQuery();
        }

        foreach (var m in res.Marks)
            InsertMark(c, tx, pid, m, null);

        tx.Commit();
        return pid;
    }

    private static void InsertMark(SqliteConnection c, SqliteTransaction tx, long pid, DrawingMark m, long? parentId)
    {
        long id;
        using (var ins = c.CreateCommand())
        {
            ins.Transaction = tx;
            ins.CommandText = @"
INSERT INTO v2_drawing_marks
 (profile_id, mark_key, mark_type, view, text, required, condition_text,
  norm_x, norm_y, norm_w, norm_h, bbox_json, view_bbox, direction, confidence,
  rule_id, page_index, span_ids, image_ids, evidence, parent_id, is_active, confirmed)
VALUES
 ($pid,$mk,$ty,$vw,$tx,$rq,$cd,$nx,$ny,$nw,$nh,$bb,$vb,$dr,$cf,$rl,$pi,$si,$ii,$ev,$par,$ia,$cn);
SELECT last_insert_rowid();";
            var nb = m.NormBbox;
            ins.Parameters.AddWithValue("$pid", pid);
            ins.Parameters.AddWithValue("$mk", m.Id);
            ins.Parameters.AddWithValue("$ty", m.Type.ToString());
            ins.Parameters.AddWithValue("$vw", m.View.ToString());
            ins.Parameters.AddWithValue("$tx", (object?)m.Text ?? DBNull.Value);
            ins.Parameters.AddWithValue("$rq", m.Required ? 1 : 0);
            ins.Parameters.AddWithValue("$cd", (object?)m.Condition ?? DBNull.Value);
            ins.Parameters.AddWithValue("$nx", nb is { Length: >= 4 } ? (object)nb[0] : DBNull.Value);
            ins.Parameters.AddWithValue("$ny", nb is { Length: >= 4 } ? (object)nb[1] : DBNull.Value);
            ins.Parameters.AddWithValue("$nw", nb is { Length: >= 4 } ? (object)nb[2] : DBNull.Value);
            ins.Parameters.AddWithValue("$nh", nb is { Length: >= 4 } ? (object)nb[3] : DBNull.Value);
            ins.Parameters.AddWithValue("$bb", m.Bbox is { Length: >= 4 } ? Json.Obj(m.Bbox) : DBNull.Value);
            ins.Parameters.AddWithValue("$vb", m.ViewBbox is { Length: >= 4 } ? Json.Obj(m.ViewBbox) : DBNull.Value);
            ins.Parameters.AddWithValue("$dr", (object?)m.Direction ?? DBNull.Value);
            ins.Parameters.AddWithValue("$cf", m.Confidence);
            ins.Parameters.AddWithValue("$rl", m.Source.RuleId ?? "");
            ins.Parameters.AddWithValue("$pi", m.Source.PageIndex);
            ins.Parameters.AddWithValue("$si", Json.Str(m.Source.SpanIds));
            ins.Parameters.AddWithValue("$ii", Json.Str(m.Source.ImageIds));
            ins.Parameters.AddWithValue("$ev", (object?)m.Source.Evidence ?? DBNull.Value);
            ins.Parameters.AddWithValue("$par", (object?)parentId ?? DBNull.Value);
            ins.Parameters.AddWithValue("$ia", 1);
            ins.Parameters.AddWithValue("$cn", 0);
            id = Convert.ToInt64(ins.ExecuteScalar());
        }

        foreach (var kid in m.Children ?? new List<DrawingMark>())
            InsertMark(c, tx, pid, kid, id);
    }

    // ---------------- 读取 ----------------

    /// <summary>计数（离线验证 / 健康检查用）。</summary>
    public Dictionary<string, long> Counts()
    {
        using var c = Open();
        static long Scalar(SqliteConnection c, string sql)
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = sql;
            return Convert.ToInt64(cmd.ExecuteScalar());
        }
        return new Dictionary<string, long>
        {
            ["profiles"] = Scalar(c, "SELECT COUNT(*) FROM v2_drawing_profiles"),
            ["views"] = Scalar(c, "SELECT COUNT(*) FROM v2_drawing_views"),
            ["marks"] = Scalar(c, "SELECT COUNT(*) FROM v2_drawing_marks"),
            ["marks_top"] = Scalar(c, "SELECT COUNT(*) FROM v2_drawing_marks WHERE parent_id IS NULL"),
            ["marks_child"] = Scalar(c, "SELECT COUNT(*) FROM v2_drawing_marks WHERE parent_id IS NOT NULL")
        };
    }

    /// <summary>按规则统计（验证落库完整性）。</summary>
    public Dictionary<string, long> RuleCounts()
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT rule_id, COUNT(*) n FROM v2_drawing_marks GROUP BY rule_id ORDER BY rule_id";
        using var rd = cmd.ExecuteReader();
        var d = new Dictionary<string, long>();
        while (rd.Read()) d[rd.GetString(0)] = rd.GetInt64(1);
        return d;
    }

    /// <summary>已剖析档案列表（最近剖析的在前）。</summary>
    public List<object> ListProfiles()
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, drawing_key, pdf_path, pdf_sha256, vpdf_schema, parser_version, decision_version,
       page_count, vision_fallback, created_at
FROM v2_drawing_profiles ORDER BY id DESC";
        using var rd = cmd.ExecuteReader();
        var list = new List<object>();
        while (rd.Read())
        {
            var id = rd.GetInt64(0);
            list.Add(new
            {
                id,
                drawingKey = rd.GetString(1),
                pdfPath = rd.GetString(2),
                sha256 = rd.GetString(3),
                schema = rd.GetString(4),
                parserVersion = rd.GetString(5),
                decisionVersion = rd.GetString(6),
                pageCount = rd.GetInt32(7),
                visionFallback = rd.GetInt32(8) != 0,
                createdAt = rd.GetString(9),
                markCount = CountMarks(c, id)
            });
        }
        return list;
    }

    private static long CountMarks(SqliteConnection c, long profileId)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT COUNT(*) FROM v2_drawing_marks WHERE profile_id=$p AND parent_id IS NULL";
        cmd.Parameters.AddWithValue("$p", profileId);
        return Convert.ToInt64(cmd.ExecuteScalar());
    }

    /// <summary>档案详情：档案 + 页面 + 顶层 mark（子项挂在 children 下）。</summary>
    public object? GetProfile(long id)
    {
        using var c = Open();

        object? head = null;
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = @"
SELECT id, drawing_key, pdf_path, pdf_sha256, vpdf_schema, parser_version, decision_version,
       page_count, vision_fallback, declared_items, scopes, warnings_json, rule_hits, created_at
FROM v2_drawing_profiles WHERE id=$id";
            cmd.Parameters.AddWithValue("$id", id);
            using var rd = cmd.ExecuteReader();
            if (!rd.Read()) return null;
            head = new
            {
                id = rd.GetInt64(0),
                drawingKey = rd.GetString(1),
                pdfPath = rd.GetString(2),
                sha256 = rd.GetString(3),
                schema = rd.GetString(4),
                parserVersion = rd.GetString(5),
                decisionVersion = rd.GetString(6),
                pageCount = rd.GetInt32(7),
                visionFallback = rd.GetInt32(8) != 0,
                declaredItems = JsonArr(rd.GetString(9)),
                scopes = JsonArr(rd.GetString(10)),
                warnings = JsonArr(rd.GetString(11)),
                ruleHits = JsonDict(rd.GetString(12)),
                createdAt = rd.GetString(13)
            };
        }

        var views = new List<object>();
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText = @"
SELECT page_index, width_pt, height_pt, orientation, page_rotation,
       text_layer, span_count, image_count, source, vision_fallback
FROM v2_drawing_views WHERE profile_id=$id ORDER BY page_index";
            cmd.Parameters.AddWithValue("$id", id);
            using var rd = cmd.ExecuteReader();
            while (rd.Read())
                views.Add(new
                {
                    pageIndex = rd.GetInt32(0),
                    widthPt = rd.GetDouble(1),
                    heightPt = rd.GetDouble(2),
                    orientation = rd.GetString(3),
                    pageRotation = rd.GetInt32(4),
                    textLayer = rd.GetInt32(5) != 0,
                    spanCount = rd.GetInt32(6),
                    imageCount = rd.GetInt32(7),
                    source = rd.IsDBNull(8) ? null : rd.GetString(8),
                    visionFallback = rd.GetInt32(9) != 0
                });
        }

        var rows = ReadMarkRows(c, id);
        var byId = rows.ToDictionary(r => r.Id);
        var roots = new List<MarkRow>();
        foreach (var r in rows)
        {
            if (r.ParentId is not null && byId.TryGetValue(r.ParentId.Value, out var parent))
                parent.Children.Add(r);
            else
                roots.Add(r);
        }

        return new { profile = head, views, marks = roots.Select(r => RowToDto(r)).ToList() };
    }

    // ---------------- 比对记录（M5） ----------------

    /// <summary>
    /// 落一次首件比对结果（历史回溯 / 复查的证据留痕）。
    /// <para>【M6 视图级配准】<paramref name="selectedView"/> 非空时写入 counts_json 的
    /// <c>selected_view</c> 键（同条记录留痕），DB schema 不变；老记录读不到该键不影响。</para>
    /// </summary>
    public long SaveCompare(long profileId, string drawingKey, string photoPath, string photoSha,
                            string? photoName, bool usable, string codeDetector, bool codeDegraded,
                            double verifyMs, IReadOnlyDictionary<string, int> counts,
                            IReadOnlyList<string> qualityReasons, string verdictsJson,
                            string? selectedView = null, string? sessionId = null,
                            string? marksSnapshotJson = null, string? paramsSnapshotJson = null)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
INSERT INTO v2_compare_records
 (profile_id, drawing_key, photo_path, photo_sha256, photo_name, usable,
  code_detector, code_degraded, verify_ms, counts_json, quality_json, verdicts_json, created_at,
  session_id, marks_snapshot_json, params_snapshot_json)
VALUES
 ($pid,$dk,$pp,$sh,$pn,$us,$cd,$dg,$ms,$ct,$qj,$vj,$ts,$sid,$msj,$psj);
SELECT last_insert_rowid();";
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$dk", drawingKey ?? "");
        cmd.Parameters.AddWithValue("$pp", photoPath ?? "");
        cmd.Parameters.AddWithValue("$sh", photoSha ?? "");
        cmd.Parameters.AddWithValue("$pn", (object?)photoName ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$us", usable ? 1 : 0);
        cmd.Parameters.AddWithValue("$cd", codeDetector ?? "");
        cmd.Parameters.AddWithValue("$dg", codeDegraded ? 1 : 0);
        cmd.Parameters.AddWithValue("$ms", verifyMs);
        // counts_json 同时存 selected_view（视图级配准回溯用，老记录读不到该键也不影响）
        var countsWithView = new Dictionary<string, object>();
        foreach (var kv in counts) countsWithView[kv.Key] = kv.Value;
        if (!string.IsNullOrEmpty(selectedView)) countsWithView["selected_view"] = selectedView;
        cmd.Parameters.AddWithValue("$ct", Json.Obj(countsWithView));
        cmd.Parameters.AddWithValue("$qj", Json.Obj(qualityReasons));
        cmd.Parameters.AddWithValue("$vj", verdictsJson ?? "[]");
        cmd.Parameters.AddWithValue("$ts", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
        cmd.Parameters.AddWithValue("$sid", (object?)sessionId ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$msj", (object?)marksSnapshotJson ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$psj", (object?)paramsSnapshotJson ?? DBNull.Value);
        return Convert.ToInt64(cmd.ExecuteScalar());
    }

    /// <summary>比对记录列表（最近的在前）。</summary>
    public List<object> ListCompareRecords(int limit = 50)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, profile_id, drawing_key, photo_path, photo_sha256, photo_name, usable,
       code_detector, code_degraded, verify_ms, counts_json, quality_json, created_at
FROM v2_compare_records ORDER BY id DESC LIMIT $n";
        cmd.Parameters.AddWithValue("$n", limit > 0 ? limit : 50);
        using var rd = cmd.ExecuteReader();
        var list = new List<object>();
        while (rd.Read())
            list.Add(new
            {
                id = rd.GetInt64(0),
                profileId = rd.GetInt64(1),
                drawingKey = rd.GetString(2),
                photoPath = rd.GetString(3),
                photoSha256 = rd.GetString(4),
                photoName = rd.IsDBNull(5) ? null : rd.GetString(5),
                usable = rd.GetInt32(6) != 0,
                codeDetector = rd.GetString(7),
                codeDegraded = rd.GetInt32(8) != 0,
                verifyMs = rd.GetDouble(9),
                counts = JsonDict(rd.GetString(10)),
                qualityReasons = JsonArr(rd.GetString(11)),
                createdAt = rd.GetString(12)
            });
        return list;
    }

    /// <summary>比对记录详情（含完整 verdicts）。</summary>
    public object? GetCompareRecord(long id)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, profile_id, drawing_key, photo_path, photo_sha256, photo_name, usable,
       code_detector, code_degraded, verify_ms, counts_json, quality_json, verdicts_json, created_at
FROM v2_compare_records WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", id);
        using var rd = cmd.ExecuteReader();
        if (!rd.Read()) return null;
        var vj = rd.GetString(12);
        object verdicts;
        try
        {
            using var doc = JsonDocument.Parse(vj);
            verdicts = doc.RootElement.Clone();
        }
        catch { verdicts = new List<object>(); }
        return new
        {
            id = rd.GetInt64(0),
            profileId = rd.GetInt64(1),
            drawingKey = rd.GetString(2),
            photoPath = rd.GetString(3),
            photoSha256 = rd.GetString(4),
            photoName = rd.IsDBNull(5) ? null : rd.GetString(5),
            usable = rd.GetInt32(6) != 0,
            codeDetector = rd.GetString(7),
            codeDegraded = rd.GetInt32(8) != 0,
            verifyMs = rd.GetDouble(9),
            counts = JsonDict(rd.GetString(10)),
            qualityReasons = JsonArr(rd.GetString(11)),
            verdicts,
            createdAt = rd.GetString(13)
        };
    }

    private sealed class MarkRow
    {
        public long Id;
        public long? ParentId;
        public string MarkKey = "";
        public string Type = "";
        public string View = "";
        public string? Text;
        public bool Required;
        public string? Condition;
        public double? Nx, Ny, Nw, Nh;
        public string? BboxJson;
        public string? ViewBboxJson;   // M6 修正·视图词范围
        public double? Direction;
        public double Confidence;
        public string RuleId = "";
        public int PageIndex;
        public string SpanIds = "[]";
        public string ImageIds = "[]";
        public string? Evidence;
        public bool IsActive = true;
        public bool Confirmed;
        public List<MarkRow> Children = new();
    }

    private static List<MarkRow> ReadMarkRows(SqliteConnection c, long profileId)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, parent_id, mark_key, mark_type, view, text, required, condition_text,
       norm_x, norm_y, norm_w, norm_h, bbox_json, direction, confidence,
       rule_id, page_index, span_ids, image_ids, evidence, view_bbox, is_active, confirmed
FROM v2_drawing_marks WHERE profile_id=$id ORDER BY id";
        cmd.Parameters.AddWithValue("$id", profileId);
        using var rd = cmd.ExecuteReader();
        var list = new List<MarkRow>();
        while (rd.Read())
        {
            static double? D(SqliteDataReader r, int i) => r.IsDBNull(i) ? null : r.GetDouble(i);
            static string? S(SqliteDataReader r, int i) => r.IsDBNull(i) ? null : r.GetString(i);
            list.Add(new MarkRow
            {
                Id = rd.GetInt64(0),
                ParentId = rd.IsDBNull(1) ? null : rd.GetInt64(1),
                MarkKey = rd.GetString(2),
                Type = rd.GetString(3),
                View = rd.GetString(4),
                Text = S(rd, 5),
                Required = rd.GetInt32(6) != 0,
                Condition = S(rd, 7),
                Nx = D(rd, 8), Ny = D(rd, 9), Nw = D(rd, 10), Nh = D(rd, 11),
                BboxJson = S(rd, 12),
                Direction = D(rd, 13),
                Confidence = rd.GetDouble(14),
                RuleId = rd.GetString(15),
                PageIndex = rd.GetInt32(16),
                SpanIds = rd.IsDBNull(17) ? "[]" : rd.GetString(17),
                ImageIds = rd.IsDBNull(18) ? "[]" : rd.GetString(18),
                Evidence = S(rd, 19),
                ViewBboxJson = S(rd, 20),
                IsActive = rd.GetInt32(21) != 0,
                Confirmed = rd.GetInt32(22) != 0
            });
        }
        return list;
    }

    private static object RowToDto(MarkRow r)
    {
        double[]? nb = (r.Nx, r.Ny, r.Nw, r.Nh) is ({ } a, { } b, { } c, { } d)
            ? new[] { a, b, c, d } : null;
        return new
        {
            id = r.Id,
            markKey = r.MarkKey,
            type = r.Type,
            view = r.View,
            text = r.Text,
            required = r.Required,
            condition = r.Condition,
            normBbox = nb,
            bbox = r.BboxJson is null ? null : JsonDoubles(r.BboxJson),
            viewBbox = r.ViewBboxJson is null ? null : JsonDoubles(r.ViewBboxJson),
            direction = r.Direction,
            confidence = r.Confidence,
            ruleId = r.RuleId,
            pageIndex = r.PageIndex,
            spanIds = JsonArr(r.SpanIds),
            imageIds = JsonArr(r.ImageIds),
            evidence = r.Evidence,
            isActive = r.IsActive,
            confirmed = r.Confirmed,
            children = r.Children.Count > 0 ? r.Children.Select(x => RowToDto(x)).ToList() : null
        };
    }

    // ---------------- 比对层专用读取（类型化） ----------------

    /// <summary>
    /// 按图纸 key 找最近一次剖析档案。精确匹配优先，其次前缀匹配（key 不含扩展名时）。
    /// </summary>
    public (long Id, string DrawingKey, string PdfPath)? FindLatestProfile(string drawingKey)
    {
        if (string.IsNullOrWhiteSpace(drawingKey)) return null;
        using var c = Open();
        foreach (var sql in new[]
        {
            "SELECT id, drawing_key, pdf_path FROM v2_drawing_profiles WHERE drawing_key=$k ORDER BY id DESC LIMIT 1",
            "SELECT id, drawing_key, pdf_path FROM v2_drawing_profiles WHERE drawing_key LIKE $p ORDER BY id DESC LIMIT 1"
        })
        {
            using var cmd = c.CreateCommand();
            cmd.CommandText = sql;
            cmd.Parameters.AddWithValue("$k", drawingKey);
            cmd.Parameters.AddWithValue("$p", Path.GetFileNameWithoutExtension(drawingKey) + "%");
            using var rd = cmd.ExecuteReader();
            if (rd.Read()) return (rd.GetInt64(0), rd.GetString(1), rd.GetString(2));
        }
        return null;
    }

    /// <summary>取档案的 drawing_key（按 id 入参时用于回填记录，避免存成 profile#N）。</summary>
    public string? ProfileKey(long id)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT drawing_key FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", id);
        var v = cmd.ExecuteScalar();
        return v is null or DBNull ? null : Convert.ToString(v);
    }

    /// <summary>取某档案的 PDF 物理路径（供前端 drawing-preview 渲染图纸截图）。</summary>
    public string? ProfilePdfPath(long id)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT pdf_path FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", id);
        var v = cmd.ExecuteScalar();
        return v is null or DBNull ? null : Convert.ToString(v);
    }

    /// <summary>
    /// 取某档案 marks 的 View 分布（供前端动态生成视图选择器选项）。
    /// <para>返回非 Unspecified 的 View 去重列表 + 各自的 mark 数量，
    /// 前端据此只列出该图纸实际有的视图。</para>
    /// </summary>
    public List<object>? ViewDistribution(string drawingKey)
    {
        var found = FindLatestProfile(drawingKey);
        if (found is null) return null;
        var (pid, _, _) = found.Value;

        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT view, COUNT(*) FROM v2_drawing_marks
WHERE profile_id=$pid AND parent_id IS NULL AND view IS NOT NULL AND view != 'Unspecified'
GROUP BY view ORDER BY COUNT(*) DESC";
        cmd.Parameters.AddWithValue("$pid", pid);
        var list = new List<object>();
        using var rd = cmd.ExecuteReader();
        while (rd.Read())
        {
            list.Add(new
            {
                view = rd.GetString(0),
                count = rd.GetInt32(1)
            });
        }
        return list;
    }

    /// <summary>载入某档案的全部 mark（含 Group 子项树，类型化）。</summary>
    public List<DrawingMark>? LoadMarks(long profileId)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT COUNT(*) FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", profileId);
        if (Convert.ToInt64(cmd.ExecuteScalar()) == 0) return null;

        var rows = ReadMarkRows(c, profileId);
        var marks = rows.ToDictionary(r => r.Id, ToMark);
        var roots = new List<DrawingMark>();
        foreach (var r in rows)
        {
            if (r.ParentId is not null && marks.TryGetValue(r.ParentId.Value, out var parent))
                parent.Children!.Add(marks[r.Id]);
            else
                roots.Add(marks[r.Id]);
        }
        return roots;
    }

    private static DrawingMark ToMark(MarkRow r)
    {
        double[]? nb = (r.Nx, r.Ny, r.Nw, r.Nh) is ({ } a, { } b, { } c, { } d)
            ? new[] { a, b, c, d } : null;
        return new DrawingMark
        {
            Id = r.MarkKey,
            Type = Enum.TryParse<MarkType>(r.Type, out var t) ? t : MarkType.Text,
            View = Enum.TryParse<MarkView>(r.View, out var v) ? v : MarkView.Unspecified,
            Text = r.Text,
            Required = r.Required,
            Condition = r.Condition,
            NormBbox = nb,
            Bbox = r.BboxJson is null ? null : JsonDoubles(r.BboxJson),
            ViewBbox = r.ViewBboxJson is null ? null : JsonDoubles(r.ViewBboxJson),
            Direction = r.Direction ?? 0,
            Confidence = r.Confidence,
            Source = new MarkSource
            {
                RuleId = r.RuleId,
                PageIndex = r.PageIndex,
                SpanIds = JsonArr(r.SpanIds),
                ImageIds = JsonArr(r.ImageIds),
                Evidence = r.Evidence
            },
            Children = new List<DrawingMark>()
        };
    }

    // ---------------- 人工复核闸门（G1） ----------------

    /// <summary>取档案复核状态（draft/reviewed/archived）。</summary>
    public string GetProfileStatus(long profileId)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT status FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", profileId);
        var v = cmd.ExecuteScalar();
        return v is null or DBNull ? "draft" : Convert.ToString(v) ?? "draft";
    }

    /// <summary>取档案复核页数据：基本信息 + 全部 marks（含 is_active/confirmed，供前端勾选编辑）。</summary>
    public object? GetProfileForReview(long profileId)
    {
        using var c = Open();
        long cnt;
        using (var chk = c.CreateCommand())
        {
            chk.CommandText = "SELECT COUNT(*) FROM v2_drawing_profiles WHERE id=$id";
            chk.Parameters.AddWithValue("$id", profileId);
            cnt = Convert.ToInt64(chk.ExecuteScalar());
        }
        if (cnt == 0) return null;

        var rows = ReadMarkRows(c, profileId);
        var marks = rows.Select(RowToDto).ToList();

        string status = "draft", reviewedBy = "", note = "", reviewedAt = "";
        using (var p = c.CreateCommand())
        {
            p.CommandText = "SELECT status, reviewed_by, review_note, reviewed_at FROM v2_drawing_profiles WHERE id=$id";
            p.Parameters.AddWithValue("$id", profileId);
            using var rd = p.ExecuteReader();
            if (rd.Read())
            {
                status = rd.IsDBNull(0) ? "draft" : rd.GetString(0);
                reviewedBy = rd.IsDBNull(1) ? "" : rd.GetString(1);
                note = rd.IsDBNull(2) ? "" : rd.GetString(2);
                reviewedAt = rd.IsDBNull(3) ? "" : rd.GetString(3);
            }
        }
        return new
        {
            profileId,
            status,
            reviewedBy,
            reviewNote = note,
            reviewedAt,
            marks
        };
    }

    /// <summary>
    /// 保存人工复核结果：按 items 增量更新各 mark 的 is_active/confirmed，
    /// 并将档案置为 reviewed（含确认人/时间/备注）。仅更新状态标志，不重建树，保留原坐标/规则溯源。
    /// </summary>
    public void SaveReview(long profileId, string? reviewedBy, string? note,
                           IReadOnlyList<(string MarkKey, bool IsActive, bool Confirmed)> items)
    {
        using var c = Open();
        using var tx = c.BeginTransaction();
        using (var upd = c.CreateCommand())
        {
            upd.Transaction = tx;
            upd.CommandText = @"
UPDATE v2_drawing_marks SET is_active=$ia, confirmed=$cn
WHERE profile_id=$pid AND mark_key=$mk";
            foreach (var it in items)
            {
                upd.Parameters.Clear();
                upd.Parameters.AddWithValue("$ia", it.IsActive ? 1 : 0);
                upd.Parameters.AddWithValue("$cn", it.Confirmed ? 1 : 0);
                upd.Parameters.AddWithValue("$pid", profileId);
                upd.Parameters.AddWithValue("$mk", it.MarkKey);
                upd.ExecuteNonQuery();
            }
        }
        using (var p = c.CreateCommand())
        {
            p.Transaction = tx;
            p.CommandText = @"
UPDATE v2_drawing_profiles
SET status='reviewed', reviewed_at=$at, reviewed_by=$rb, review_note=$nt
WHERE id=$id";
            p.Parameters.AddWithValue("$at", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            p.Parameters.AddWithValue("$rb", (object?)reviewedBy ?? DBNull.Value);
            p.Parameters.AddWithValue("$nt", (object?)note ?? DBNull.Value);
            p.Parameters.AddWithValue("$id", profileId);
            p.ExecuteNonQuery();
        }
        tx.Commit();
    }

    // ---------------- 会话合并（G2） ----------------

    private sealed class SessionAgg
    {
        public string MarkKey = "";
        public string Text = "";
        public string View = "";
        public List<string> States = new();
        public string Rollup()
        {
            if (States.Count == 0) return "Unknown";
            if (States.All(s => s == "Matched")) return "Matched";
            if (States.Any(s => s is "Missing" or "NotComparable")) return "Missing";
            if (States.Any(s => s == "LowConfidence")) return "LowConfidence";
            // S1：未检出（黄）跨照片合并 → 需复核（不升级为缺标红）
            if (States.Any(s => s == "NotDetected")) return "NotDetected";
            return "NeedReview";
        }
    }

    /// <summary>
    /// 取一次多部位检验会话的聚合结果：返回该会话全部 records + 按 markKey 跨部位合并的 verdicts。
    /// 合并规则：任一部位 Missing/NotComparable → 疑似缺标(红)；全 Matched → 一致(绿)；
    /// 含 LowConfidence 或混合 → 需复核(黄)。
    /// </summary>
    public object? GetSession(string sessionId)
    {
        if (string.IsNullOrWhiteSpace(sessionId)) return null;
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, profile_id, drawing_key, photo_name, usable, counts_json, verdicts_json, created_at
FROM v2_compare_records WHERE session_id=$sid ORDER BY id";
        cmd.Parameters.AddWithValue("$sid", sessionId);
        using var rd = cmd.ExecuteReader();
        var records = new List<object>();
        var agg = new Dictionary<string, SessionAgg>();
        while (rd.Read())
        {
            var vj = rd.GetString(6);
            object? verdicts = null;
            try { using var doc = JsonDocument.Parse(vj); verdicts = doc.RootElement.Clone(); } catch { }
            records.Add(new
            {
                id = rd.GetInt64(0),
                profileId = rd.GetInt64(1),
                drawingKey = rd.GetString(2),
                photoName = rd.IsDBNull(3) ? null : rd.GetString(3),
                usable = rd.GetInt32(4) != 0,
                counts = JsonDict(rd.GetString(5)),
                verdicts,
                createdAt = rd.GetString(7)
            });
            try
            {
                using var doc = JsonDocument.Parse(vj);
                foreach (var v in doc.RootElement.EnumerateArray())
                {
                    var mk = v.GetProperty("markKey").GetString() ?? "";
                    var st = v.GetProperty("state").GetString() ?? "";
                    if (!agg.TryGetValue(mk, out var a))
                    {
                        a = new SessionAgg
                        {
                            MarkKey = mk,
                            Text = v.TryGetProperty("text", out var t) ? (t.GetString() ?? "") : "",
                            View = v.TryGetProperty("view", out var vw) ? (vw.GetString() ?? "") : ""
                        };
                        agg[mk] = a;
                    }
                    a.States.Add(st);
                }
            }
            catch { }
        }
        var aggregated = agg.Values.Select(a => new
        {
            markKey = a.MarkKey,
            text = a.Text,
            view = a.View,
            states = a.States.Distinct().ToList(),
            verdict = a.Rollup()
        }).ToList();
        return new { sessionId, recordCount = records.Count, records, aggregated };
    }

    private static List<string> JsonArr(string s)
    {
        try { return JsonSerializer.Deserialize<List<string>>(s) ?? new List<string>(); }
        catch { return new List<string>(); }
    }

    private static double[]? JsonDoubles(string s)
    {
        try { return JsonSerializer.Deserialize<double[]>(s); }
        catch { return null; }
    }

    private static object JsonDict(string s)
    {
        try
        {
            var d = JsonSerializer.Deserialize<Dictionary<string, int>>(s);
            return d ?? (object)new Dictionary<string, int>();
        }
        catch { return new Dictionary<string, int>(); }
    }

    /// <summary>决策层版本：随契约/规则变更递增，用于判断历史 profile 是否需要重算。</summary>
    public const string DecisionVersion = "m2.1";

    private static class Json
    {
        private static readonly JsonSerializerOptions Opts = new()
        {
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping
        };

        public static string Str(IEnumerable<string> v) => JsonSerializer.Serialize(v, Opts);
        public static string Str(IEnumerable<double> v) => JsonSerializer.Serialize(v, Opts);
        public static string Obj(object v) => JsonSerializer.Serialize(v, Opts);
    }
}
