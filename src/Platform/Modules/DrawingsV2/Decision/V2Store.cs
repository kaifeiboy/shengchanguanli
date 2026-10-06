using System.Text.Json;
using System.Text.Json.Nodes;
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
    /// <summary>
    /// M1 缓存的**响应结构**版本号（#36 引入）。
    /// <para>只要 compare 响应的字段结构发生变化就 +1：旧快照会因版本不匹配自动失效，
    /// 避免缓存把「上一次代码版本的响应」原样返回，从而掩盖新改动
    /// （典型症状：新字段永远看不到，还以为是没生效）。</para>
    /// <para>注意它和 <c>DecisionVersion</c> 不同：DecisionVersion 描述**判定规则**，
    /// 本值描述**响应格式**。只加字段不动规则时，前者不变、后者必须 +1。</para>
    /// </summary>
    public const string ResponseSchemaVersion = "2";

    /// <summary>
    /// 代码指纹（本程序集 MVID 前 8 位）：**每次重新编译都会变**。
    /// <para>纳入缓存校验后，代码一变更旧缓存自动失效 —— 否则缓存会把「旧代码算出的结论」
    /// 当成新代码的结论返回，既掩盖新改动，也掩盖修复（本次就是这么发现的：
    /// 改了配置、改了空组置信度，端点却一直返回旧值）。</para>
    /// <para>代价：每次部署后第一批请求会重新计算一次（之后照常命中），
    /// 这正是「代码变了就不该复用旧结论」应有的代价。</para>
    /// </summary>
    public static string CodeFingerprint =>
        typeof(V2Store).Assembly.ManifestModule.ModuleVersionId.ToString("N").Substring(0, 8);

    private readonly string _connStr;
    private static readonly object InitLock = new();
    private static bool _initialized;

    /// <summary>
    /// X1（#36）重剖析时若「应打标对象集合逐字未变」则保留人工复核状态（默认开）。
    /// 关闭即回到旧行为：任何一次重剖析都无条件打回 draft。
    /// </summary>
    private readonly bool _keepReviewedOnUnchanged;

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
        _keepReviewedOnUnchanged = !bool.TryParse(config["DrawingsV2:KeepReviewedOnUnchanged"], out var krc) || krc;
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
        _keepReviewedOnUnchanged = true;   // 离线验证台无配置：与生产默认一致（开）
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

-- === P2 · 逻辑图块（docs/逻辑图块方案_2026-09-26.md §4）===
-- 【与旧 v2_drawing_blocks 的区别】旧表是「物理切图矩形 + 块级过滤」，已被实测证伪
-- （p62 聚出 8 块全是标题栏、产品部位 0 个）并于 2026-09-26 删除。
-- 新表是「元素逻辑集合 + 包围盒」：元素保留全局归一化坐标，只多一个 block_id，不切 PDF。
CREATE TABLE IF NOT EXISTS v2_logical_blocks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id     INTEGER NOT NULL REFERENCES v2_drawing_profiles(id) ON DELETE CASCADE,
    page_index     INTEGER NOT NULL DEFAULT 0,
    block_key      TEXT    NOT NULL,        -- 稳定键（含算法版本 + 几何指纹，防 markKey 式漂移）
    block_index    INTEGER NOT NULL,        -- 面积序（沿用 v8.4 部位分离输出顺序）
    name           TEXT    NOT NULL DEFAULT '',   -- §0.5.2：以块内打标内容命名（排除 d 类）
    name_fp        TEXT    NOT NULL DEFAULT '',   -- 归一化指纹（去空格/全半角/大小写）→ 命中检索键
    norm_x         REAL    NULL,
    norm_y         REAL    NULL,
    norm_w         REAL    NULL,
    norm_h         REAL    NULL,
    bbox_json      TEXT    NULL,            -- 原始 [x0,y0,x1,y1]（pt）
    area_pct       REAL    NOT NULL DEFAULT 0,
    view_hint      TEXT    NOT NULL DEFAULT 'Unspecified',
    has_marking    INTEGER NOT NULL DEFAULT 1,    -- §2.1：0=空块，不参与命中检索、不产生四色判定
    empty_reason   TEXT    NULL,            -- §2.2：no_text_layer/ocr_empty/all_dim_note/ocr_noise_only
    low_conf       INTEGER NOT NULL DEFAULT 0,    -- 内容来自低置信 OCR → 命中走【黄】，不算空
    algo_version   TEXT    NOT NULL DEFAULT '',
    geom_fp        TEXT    NULL,            -- 几何指纹（bbox+元素数），漂移检测
    n_elements     INTEGER NOT NULL DEFAULT 0,
    n_participate  INTEGER NOT NULL DEFAULT 0,
    n_image        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);
-- 按「位置 + 算法版本」唯一：同一 profile 重跑即覆盖，不产生重复行
CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_lblock_pos
    ON v2_logical_blocks(profile_id, page_index, block_index, algo_version);
CREATE INDEX IF NOT EXISTS ix_v2_lblock_profile  ON v2_logical_blocks(profile_id, page_index);
-- 命中检索恒带 has_marking=1 条件，该索引直接缩小候选集
CREATE INDEX IF NOT EXISTS ix_v2_lblock_marking  ON v2_logical_blocks(has_marking, name_fp);

-- 块内元素（逻辑集合成员）：坐标仍是【全局归一化】，只多一个 block_id
CREATE TABLE IF NOT EXISTS v2_block_elements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id     INTEGER NOT NULL REFERENCES v2_logical_blocks(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL,          -- text/curve_text/raster_text/icon/qr/dim/note
    text         TEXT    NULL,
    ocr_conf     REAL    NULL,
    norm_x       REAL    NULL,
    norm_y       REAL    NULL,
    norm_w       REAL    NULL,
    norm_h       REAL    NULL,
    bbox_json    TEXT    NULL,
    participate  INTEGER NOT NULL DEFAULT 0,  -- 是否参与匹配（d 类=0）
    is_anchor    INTEGER NOT NULL DEFAULT 0,  -- 是否选为锚点（P4 配准用，当前恒 0）
    source       TEXT    NOT NULL DEFAULT ''  -- text_layer/outline_ocr/outline_ocr_quad/image/vector
);
CREATE INDEX IF NOT EXISTS ix_v2_lelem_block ON v2_block_elements(block_id);
CREATE INDEX IF NOT EXISTS ix_v2_lelem_part  ON v2_block_elements(participate, kind);
";
            cmd.ExecuteNonQuery();
            MigrateMarkColumns(c);
            MigrateProfileColumns(c);
            MigrateRecordColumns(c);
            MigrateVerdictReviewTable(c);
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
        if (!cols.Contains("excluded"))
        {
            using var alter = c.CreateCommand();
            alter.CommandText = "ALTER TABLE v2_drawing_marks ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0";
            alter.ExecuteNonQuery();
        }
        if (!cols.Contains("exclude_reason"))
        {
            using var alter = c.CreateCommand();
            alter.CommandText = "ALTER TABLE v2_drawing_marks ADD COLUMN exclude_reason TEXT NULL";
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
        // M7（库管理）逻辑块抽取状态机：pending(待抽取)/extracting(抽取中)/ready(就绪)/failed(失败)
        Add("blocks_status", "TEXT NOT NULL DEFAULT 'pending'");
        Add("blocks_error", "TEXT NULL");
        Add("blocks_updated_at", "TEXT NULL");
    }

    /// <summary>设置档案的逻辑块抽取状态（异步抽取队列回调）。</summary>
    public void SetBlockStatus(long profileId, string status, string? error)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "UPDATE v2_drawing_profiles SET blocks_status=$s, blocks_error=$e, blocks_updated_at=$t WHERE id=$id";
        cmd.Parameters.AddWithValue("$s", status);
        cmd.Parameters.AddWithValue("$e", (object?)error ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$t", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
        cmd.Parameters.AddWithValue("$id", profileId);
        cmd.ExecuteNonQuery();
    }

    /// <summary>查询档案的逻辑块抽取状态与块数。</summary>
    public object? GetBlockStatus(long profileId)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT blocks_status, blocks_error, blocks_updated_at,
       (SELECT COUNT(*) FROM v2_logical_blocks WHERE profile_id=$id) AS block_count
FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", profileId);
        using var rd = cmd.ExecuteReader();
        if (!rd.Read()) return null;
        return new
        {
            profileId,
            status = rd.IsDBNull(0) ? "pending" : rd.GetString(0),
            error = rd.IsDBNull(1) ? null : rd.GetString(1),
            updatedAt = rd.IsDBNull(2) ? null : rd.GetString(2),
            blockCount = rd.GetInt64(3)
        };
    }

    /// <summary>已登记档案的 drawing_key 集合（供目录扫描去重）。</summary>
    public HashSet<string> AllDrawingKeys()
    {
        var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT drawing_key FROM v2_drawing_profiles";
        using var rd = cmd.ExecuteReader();
        while (rd.Read()) set.Add(rd.GetString(0));
        return set;
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
        // M1（#35）照片指纹缓存：完整响应快照，供「完全相同的输入」直接复用，保证逐字一致。
        Add("response_json", "TEXT NULL");
    }

    /// <summary>
    /// ② 判定级人工确认（幂等建表）：人工对「单条判定」的确认结果独立审计表。
    /// 不修改 verdicts_json 原始快照 —— 系统判定永久留痕，人工判定叠加在上层，可随时对照/撤销。
    /// </summary>
    private static void MigrateVerdictReviewTable(SqliteConnection c)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
CREATE TABLE IF NOT EXISTS v2_verdict_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    INTEGER NOT NULL REFERENCES v2_compare_records(id) ON DELETE CASCADE,
    session_id   TEXT    NULL,
    profile_id   INTEGER NOT NULL DEFAULT 0,
    mark_key     TEXT    NOT NULL,
    system_state TEXT    NOT NULL DEFAULT '',
    human_state  TEXT    NOT NULL,
    human_by     TEXT    NULL,
    human_note   TEXT    NULL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_vreview      ON v2_verdict_reviews(record_id, mark_key);
CREATE INDEX        IF NOT EXISTS ix_v2_vreview_sess ON v2_verdict_reviews(session_id);
CREATE INDEX        IF NOT EXISTS ix_v2_vreview_prof ON v2_verdict_reviews(profile_id);
";
        cmd.ExecuteNonQuery();

        // M2（#35）对象级确认记忆所需的三个维度（幂等）。
        // ver        = 判据版本 + 图纸内容版本 —— 任一变化，历史记忆全部失效（防「记忆串台」）
        // photo_sha  = 来源照片指纹（追溯用）
        // geom_norm  = 该对象的几何指纹 —— markKey 因剖析顺序可能漂移，坐标指纹用于二次校验
        var cols = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var q2 = c.CreateCommand())
        {
            q2.CommandText = "PRAGMA table_info(v2_verdict_reviews)";
            using var rd2 = q2.ExecuteReader();
            while (rd2.Read()) cols.Add(rd2.GetString(1));
        }
        void Add(string n, string def)
        {
            if (cols.Contains(n)) return;
            using var alt = c.CreateCommand();
            alt.CommandText = $"ALTER TABLE v2_verdict_reviews ADD COLUMN {n} {def}";
            alt.ExecuteNonQuery();
        }
        Add("ver", "TEXT NULL");
        Add("photo_sha", "TEXT NULL");
        Add("geom_norm", "TEXT NULL");
    }

    /// <summary>一条人工判定（人工确认动作的最小落库单元）。</summary>
    public sealed class VerdictReview
    {
        public string HumanState = "";
        public string SystemState = "";
        public string? By;
        public string? Note;
        public string At = "";
    }

    /// <summary>
    /// ② 保存人工判定（upsert：同一 record+markKey 以最新一次为准）。
    /// system_state 从该记录的 verdicts_json 里回填，保证「系统当时怎么判的」永久留痕。
    /// </summary>
    public int SaveVerdictReviews(long recordId,
        IEnumerable<(string MarkKey, string HumanState, string? Note)> items, string? by)
    {
        long profileId = 0;
        string? sessionId = null;
        string pdfSha = "", photoSha = "";
        var sysStates = new Dictionary<string, string>(StringComparer.Ordinal);
        var geomByMark = new Dictionary<string, string>(StringComparer.Ordinal);
        using (var c0 = Open())
        {
            using var q = c0.CreateCommand();
            q.CommandText = @"
SELECT r.profile_id, r.session_id, r.verdicts_json, IFNULL(p.pdf_sha256,''), IFNULL(r.photo_sha256,'')
FROM v2_compare_records r LEFT JOIN v2_drawing_profiles p ON p.id = r.profile_id
WHERE r.id=$id";
            q.Parameters.AddWithValue("$id", recordId);
            using var rd0 = q.ExecuteReader();
            if (!rd0.Read()) return 0;
            profileId = rd0.GetInt64(0);
            sessionId = rd0.IsDBNull(1) ? null : rd0.GetString(1);
            var vj = rd0.GetString(2);
            pdfSha = rd0.IsDBNull(3) ? "" : rd0.GetString(3);
            photoSha = rd0.IsDBNull(4) ? "" : rd0.GetString(4);
            try
            {
                if (JsonNode.Parse(vj) is JsonArray arr)
                    foreach (var jn in arr)
                        if (jn is JsonObject jo)
                        {
                            var mk = jo["markKey"]?.GetValue<string>() ?? "";
                            var stt = jo["state"]?.GetValue<string>() ?? "";
                            if (mk.Length > 0) sysStates[mk] = stt;
                            // 几何指纹：优先期望位置（预期该在哪），其次图纸页面位置
                            var gb = jo["expectedBbox"] ?? jo["drawingBbox"];
                            if (gb is JsonArray ga)
                            {
                                var nums = ga.Where(x => x is JsonValue)
                                             .Select(x => x.GetValue<double>().ToString("F4")).ToArray();
                                if (nums.Length >= 4) geomByMark[mk] = string.Join(",", nums);
                            }
                        }
            }
            catch { }
        }

        // M2（#35）版本指纹 = 判定规则版本 + 图纸内容版本。
        // 任一变化 → 全部历史记忆失效：防止剖析重排后「同一 markKey 指向不同对象」导致的串台。
        var ver = DecisionVersion + ":" + (pdfSha.Length >= 16 ? pdfSha.Substring(0, 16) : pdfSha);

        var now = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
        int n = 0;
        using var c = Open();
        using var tx = c.BeginTransaction();
        foreach (var (mk, st, note) in items)
        {
            if (string.IsNullOrWhiteSpace(mk) || string.IsNullOrWhiteSpace(st)) continue;
            using var del = c.CreateCommand();
            del.Transaction = tx;
            del.CommandText = "DELETE FROM v2_verdict_reviews WHERE record_id=$rid AND mark_key=$mk";
            del.Parameters.AddWithValue("$rid", recordId);
            del.Parameters.AddWithValue("$mk", mk);
            del.ExecuteNonQuery();

            using var ins = c.CreateCommand();
            ins.Transaction = tx;
            ins.CommandText = @"
INSERT INTO v2_verdict_reviews
 (record_id, session_id, profile_id, mark_key, system_state, human_state, human_by, human_note,
  created_at, updated_at, ver, photo_sha, geom_norm)
VALUES ($rid,$sid,$pid,$mk,$ss,$hs,$by,$nt,$ts,$ts,$ver,$psh,$geo)";
            ins.Parameters.AddWithValue("$rid", recordId);
            ins.Parameters.AddWithValue("$sid", (object?)sessionId ?? DBNull.Value);
            ins.Parameters.AddWithValue("$pid", profileId);
            ins.Parameters.AddWithValue("$mk", mk);
            ins.Parameters.AddWithValue("$ss", sysStates.TryGetValue(mk, out var ss) ? ss : "");
            ins.Parameters.AddWithValue("$hs", st);
            ins.Parameters.AddWithValue("$by", (object?)by ?? DBNull.Value);
            ins.Parameters.AddWithValue("$nt", (object?)note ?? DBNull.Value);
            ins.Parameters.AddWithValue("$ts", now);
            ins.Parameters.AddWithValue("$ver", ver);
            ins.Parameters.AddWithValue("$psh", photoSha);
            ins.Parameters.AddWithValue("$geo", (object?)(geomByMark.TryGetValue(mk, out var gm) ? gm : null) ?? DBNull.Value);
            n += ins.ExecuteNonQuery();
        }
        tx.Commit();
        return n;
    }

    /// <summary>
    /// M2（#35）对象级确认记忆：同一「档案版本 + 判据版本」下，每个 markKey 最近一次人工结论。
    /// 只取 ver 完全匹配的行 —— 换版 / 规则升级前的记忆一律不复用。
    /// **只读**：不改写任何系统判定，也不参与 state 的产生。
    /// </summary>
    /// <summary>
    /// M2/M3 共用的「人工三态 ↔ 系统八态」一致性映射 —— **单一真相源**。
    /// <para>两者枚举空间不同（HumanPresent/HumanMissing/HumanWrongPart vs 八态），
    /// 直接字符串相等恒为 false。映射沿用既有会话聚合语义（<c>SessionAgg.Rollup</c>）：
    /// HumanPresent→Matched/Extra、HumanMissing→Missing、
    /// HumanWrongPart→「没拍到或不可比」。此处不引入任何新规则。</para>
    /// </summary>
    public static bool MemoryAgrees(string human, string sys) => human switch
    {
        "HumanPresent"   => sys is "Matched" or "Extra",
        "HumanMissing"   => sys is "Missing",
        "HumanWrongPart" => sys is "NotDetected" or "NotComparable",
        _ => false
    };


    public Dictionary<string, (string Human, string? By, string At)> ListMarkMemory(long profileId, string ver)
    {
        var d = new Dictionary<string, (string, string?, string)>(StringComparer.Ordinal);
        if (string.IsNullOrWhiteSpace(ver)) return d;
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT mark_key, human_state, human_by, updated_at
FROM v2_verdict_reviews
WHERE profile_id=$pid AND ver=$ver
ORDER BY id DESC";
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$ver", ver);
        using var rd = cmd.ExecuteReader();
        while (rd.Read())
        {
            var k = rd.GetString(0);
            if (d.ContainsKey(k)) continue;   // id DESC：首次出现即最新
            d[k] = (rd.GetString(1), rd.IsDBNull(2) ? null : rd.GetString(2), rd.GetString(3));
        }
        return d;
    }

    /// <summary>取某条记录的全部人工判定（markKey → 判定）。</summary>
    public Dictionary<string, VerdictReview> ListVerdictReviews(long recordId)
    {
        var r = new Dictionary<string, VerdictReview>(StringComparer.Ordinal);
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT mark_key, system_state, human_state, human_by, human_note, updated_at
FROM v2_verdict_reviews WHERE record_id=$rid";
        cmd.Parameters.AddWithValue("$rid", recordId);
        using var rd = cmd.ExecuteReader();
        while (rd.Read())
            r[rd.GetString(0)] = new VerdictReview
            {
                SystemState = rd.GetString(1),
                HumanState = rd.GetString(2),
                By = rd.IsDBNull(3) ? null : rd.GetString(3),
                Note = rd.IsDBNull(4) ? null : rd.GetString(4),
                At = rd.GetString(5)
            };
        return r;
    }

    /// <summary>② 会话级人工判定汇总（markKey → 跨照片的判定列表），供会话聚合「人工优先」使用。</summary>
    private static Dictionary<string, List<(string State, string? By, string? Note, string At)>>
        LoadVerdictReviewsOfSession(SqliteConnection c, string sessionId)
    {
        var d = new Dictionary<string, List<(string, string?, string?, string)>>(StringComparer.Ordinal);
        if (string.IsNullOrWhiteSpace(sessionId)) return d;
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT mark_key, human_state, human_by, human_note, updated_at
FROM v2_verdict_reviews WHERE session_id=$sid ORDER BY id";
        cmd.Parameters.AddWithValue("$sid", sessionId);
        using var rd = cmd.ExecuteReader();
        while (rd.Read())
        {
            var mk = rd.GetString(0);
            if (!d.TryGetValue(mk, out var l)) { l = new(); d[mk] = l; }
            l.Add((rd.GetString(1), rd.IsDBNull(2) ? null : rd.GetString(2),
                   rd.IsDBNull(3) ? null : rd.GetString(3), rd.GetString(4)));
        }
        return d;
    }

    // ---------------- 写入 ----------------

    /// <summary>
    /// 保存一次剖析结果（profile + views + marks + group 子项）。
    /// 同一 (drawing_key, pdf_sha256) 重复保存时复用原 profile id，只替换 views/marks，
    /// 保留已关联的历史比对记录；对象变化后状态重置为 draft，必须重新人工复核。
    /// </summary>
    public long SaveProfile(VpdfDocument doc, MarkBuilder.Result res, string pdfPath, string parserVersion)
    {
        ArgumentNullException.ThrowIfNull(doc);
        ArgumentNullException.ThrowIfNull(res);

        using var c = Open();
        using var tx = c.BeginTransaction();

        long? existingId = null;
        using (var find = c.CreateCommand())
        {
            find.Transaction = tx;
            find.CommandText = "SELECT id FROM v2_drawing_profiles WHERE drawing_key=$k AND pdf_sha256=$s";
            find.Parameters.AddWithValue("$k", res.DrawingKey);
            find.Parameters.AddWithValue("$s", doc.Source?.Sha256 ?? "");
            var value = find.ExecuteScalar();
            if (value is not null and not DBNull) existingId = Convert.ToInt64(value);
        }

        long pid;
        if (existingId is { } id)
        {
            pid = id;

            // X1（#36）：先算「旧对象集合」的内容指纹 —— 必须在 DELETE 之前读。
            // 重剖析若产出的应打标对象集合逐字未变，则此前的人工复核结论依然成立，
            // 不应被打回 draft（现状无条件打回，是「每次都要重新人工确认」的 L1 根因，见方案评估 §4）。
            // 对象集合有任何变化 → 结论不再成立，照旧打回 draft：不放宽任何一条质量要求。
            var oldFp = ContentFingerprintDb(c, tx, pid);
            var newFp = ContentFingerprint(res.Marks);
            var keepReviewed = _keepReviewedOnUnchanged
                               && oldFp.Length > 0
                               && string.Equals(oldFp, newFp, StringComparison.Ordinal);

            foreach (var table in new[] { "v2_drawing_views", "v2_drawing_marks" })
            {
                using var del = c.CreateCommand();
                del.Transaction = tx;
                del.CommandText = $"DELETE FROM {table} WHERE profile_id=$id";
                del.Parameters.AddWithValue("$id", pid);
                del.ExecuteNonQuery();
            }

            using var update = c.CreateCommand();
            update.Transaction = tx;
            update.CommandText = @"
UPDATE v2_drawing_profiles SET
 pdf_path=$p, vpdf_schema=$sch, parser_version=$pv, decision_version=$dv,
 page_count=$pc, vision_fallback=$fb, declared_items=$di, scopes=$sc,
 warnings_json=$wn, rule_hits=$rh, created_at=$ts"
 + (keepReviewed ? "" : @",
 status='draft', reviewed_at=NULL, reviewed_by=NULL, review_note=NULL")
 + @"
WHERE id=$id";
            update.Parameters.AddWithValue("$id", pid);
            update.Parameters.AddWithValue("$p", pdfPath ?? "");
            update.Parameters.AddWithValue("$sch", doc.Schema ?? "");
            update.Parameters.AddWithValue("$pv", parserVersion ?? "");
            update.Parameters.AddWithValue("$dv", DecisionVersion);
            update.Parameters.AddWithValue("$pc", doc.Pages?.Count ?? 0);
            update.Parameters.AddWithValue("$fb", res.VisionFallbackRequired ? 1 : 0);
            update.Parameters.AddWithValue("$di", Json.Str(res.DeclaredItems));
            update.Parameters.AddWithValue("$sc", Json.Str(res.Scopes));
            update.Parameters.AddWithValue("$wn", Json.Str(res.Warnings));
            update.Parameters.AddWithValue("$rh", Json.Obj(res.RuleHits));
            update.Parameters.AddWithValue("$ts", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            update.ExecuteNonQuery();
        }
        else
        {
            using var ins = c.CreateCommand();
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
  rule_id, page_index, span_ids, image_ids, evidence, parent_id, is_active, confirmed,
  excluded, exclude_reason)
VALUES
 ($pid,$mk,$ty,$vw,$tx,$rq,$cd,$nx,$ny,$nw,$nh,$bb,$vb,$dr,$cf,$rl,$pi,$si,$ii,$ev,$par,$ia,$cn,$ex,$er);
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
            ins.Parameters.AddWithValue("$ex", m.Excluded ? 1 : 0);
            ins.Parameters.AddWithValue("$er", (object?)m.ExcludeReason ?? DBNull.Value);
            id = Convert.ToInt64(ins.ExecuteScalar());
        }

        foreach (var kid in m.Children ?? new List<DrawingMark>())
            InsertMark(c, tx, pid, kid, id);
    }

    // ---------------- X1（#36）剖析内容指纹 ----------------

    /// <summary>
    /// 应打标对象集合的内容指纹（sha256）。只描述「对象集合本身」：
    /// 不含复核产物（is_active / confirmed）、不含时间戳、不含数据库自增 id。
    /// <para>用途：判断重剖析后对象集合是否**逐字未变**，未变则保留人工复核状态。</para>
    /// <para>⚠️ mark_key 含剖析顺序编号（<c>{drawingKey}#{seq:D3}</c>），
    /// 剖析顺序变化会让同一对象的 key 改变 → 指纹随之改变 → 打回 draft。
    /// 这是刻意的保守行为：key 漂移意味着历史沉淀可能串台，宁可重新复核。</para>
    /// </summary>
    public static string ContentFingerprint(IReadOnlyList<DrawingMark> marks)
    {
        var lines = new List<string>();
        void Walk(DrawingMark m)
        {
            var nb = m.NormBbox is { Length: >= 4 } b
                ? string.Join(",", b.Select(x => x.ToString("F4", System.Globalization.CultureInfo.InvariantCulture)))
                : "-";
            lines.Add(FpRow(m.Id, m.Type.ToString(), m.View.ToString(), m.Text ?? "",
                            m.Required ? "1" : "0", m.Condition ?? "", nb,
                            m.Source.RuleId ?? "", m.Source.Evidence ?? "",
                            m.Source.PageIndex.ToString(System.Globalization.CultureInfo.InvariantCulture),
                            m.Source.SpanIds.Count.ToString(System.Globalization.CultureInfo.InvariantCulture),
                            m.Source.ImageIds.Count.ToString(System.Globalization.CultureInfo.InvariantCulture)));
            foreach (var kid in m.Children ?? new List<DrawingMark>()) Walk(kid);
        }
        foreach (var m in marks) Walk(m);
        if (lines.Count == 0) return "";
        lines.Sort(StringComparer.Ordinal);   // 遍历顺序变化不应导致指纹变化
        return Sha256Hex(string.Join("\n", lines));
    }

    /// <summary>库内对象集合的内容指纹（字段与 <see cref="ContentFingerprint"/> 逐项对齐）。</summary>
    private static string ContentFingerprintDb(SqliteConnection c, SqliteTransaction tx, long pid)
    {
        var lines = new List<string>();
        using var cmd = c.CreateCommand();
        cmd.Transaction = tx;
        cmd.CommandText = @"
SELECT mark_key, mark_type, IFNULL(view,''), IFNULL(text,''), required, IFNULL(condition_text,''),
       norm_x, norm_y, norm_w, norm_h,
       IFNULL(rule_id,''), IFNULL(evidence,''), page_index,
       IFNULL(span_ids,''), IFNULL(image_ids,'')
FROM v2_drawing_marks WHERE profile_id=$pid";
        cmd.Parameters.AddWithValue("$pid", pid);
        using var rd = cmd.ExecuteReader();
        while (rd.Read())
        {
            var norm = rd.IsDBNull(6)
                ? "-"
                : string.Join(",", new[] { rd.GetDouble(6), rd.GetDouble(7), rd.GetDouble(8), rd.GetDouble(9) }
                                   .Select(x => x.ToString("F4", System.Globalization.CultureInfo.InvariantCulture)));
            lines.Add(FpRow(rd.GetString(0), rd.GetString(1), rd.GetString(2), rd.GetString(3),
                            rd.GetInt32(4) != 0 ? "1" : "0", rd.GetString(5), norm,
                            rd.GetString(10), rd.GetString(11),
                            rd.GetInt32(12).ToString(System.Globalization.CultureInfo.InvariantCulture),
                            JsonArrayCount(rd.GetString(13)).ToString(System.Globalization.CultureInfo.InvariantCulture),
                            JsonArrayCount(rd.GetString(14)).ToString(System.Globalization.CultureInfo.InvariantCulture)));
        }
        if (lines.Count == 0) return "";
        lines.Sort(StringComparer.Ordinal);
        return Sha256Hex(string.Join("\n", lines));
    }

    private static string FpRow(params string[] cells) => string.Join('|', cells);

    private static int JsonArrayCount(string json)
    {
        if (string.IsNullOrWhiteSpace(json)) return 0;
        try { return JsonNode.Parse(json) is JsonArray a ? a.Count : 0; }
        catch { return 0; }
    }

    private static string Sha256Hex(string s)
    {
        using var sha = System.Security.Cryptography.SHA256.Create();
        return Convert.ToHexString(sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(s)));
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
       page_count, vision_fallback, created_at,
       COALESCE(status,'draft') AS status,
       COALESCE(blocks_status,'pending') AS blocks_status,
       blocks_error,
       (SELECT COUNT(*) FROM v2_logical_blocks WHERE profile_id=v2_drawing_profiles.id) AS block_count
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
                status = rd.GetString(10),
                blocksStatus = rd.GetString(11),
                blocksError = rd.IsDBNull(12) ? null : rd.GetString(12),
                blockCount = rd.GetInt64(13),
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
                            string? marksSnapshotJson = null, string? paramsSnapshotJson = null,
                            string? responseJson = null)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
INSERT INTO v2_compare_records
 (profile_id, drawing_key, photo_path, photo_sha256, photo_name, usable,
  code_detector, code_degraded, verify_ms, counts_json, quality_json, verdicts_json, created_at,
  session_id, marks_snapshot_json, params_snapshot_json, response_json)
VALUES
 ($pid,$dk,$pp,$sh,$pn,$us,$cd,$dg,$ms,$ct,$qj,$vj,$ts,$sid,$msj,$psj,$rj);
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
        cmd.Parameters.AddWithValue("$rj", (object?)responseJson ?? DBNull.Value);
        return Convert.ToInt64(cmd.ExecuteScalar());
    }

    /// <summary>M1（#35）：回填本次比对的完整响应 JSON（照片指纹缓存的复用源，recordId 已校正）。</summary>
    public void UpdateCompareResponse(long id, string responseJson)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "UPDATE v2_compare_records SET response_json=$rj WHERE id=$id";
        cmd.Parameters.AddWithValue("$rj", (object?)responseJson ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$id", id);
        cmd.ExecuteNonQuery();
    }

    /// <summary>档案图纸文件的 sha256（前 16 位作为「档案版本指纹」，图纸换版即变化）。</summary>
    public string ProfilePdfSha(long profileId)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT pdf_sha256 FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", profileId);
        var v = cmd.ExecuteScalar();
        return v is null || v is DBNull ? "" : Convert.ToString(v) ?? "";
    }

    /// <summary>M1（#35）照片指纹缓存命中的历史记录。</summary>
    public sealed class CachedRecord
    {
        public long Id;
        public string ResponseJson = "";
        public string VerdictsJson = "[]";
        public string CountsJson = "{}";
        public string QualityJson = "[]";
        public bool Usable;
        public string CodeDetector = "";
        public bool CodeDegraded;
        public string MarksSnapshotJson = "";
        public string ParamsSnapshotJson = "";
    }

    /// <summary>
    /// M1（#35）按「照片指纹」查找可复用记录。
    /// 键 = profileId + 档案版本(pdf_sha256 前16位) + 判定版本(DecisionVersion) + photo_sha256 + view。
    /// 任一维度变化都视为不同输入 → 不复用（图纸换版 / 规则升级 / 换照片 / 换部位 必须失效）。
    /// </summary>
    public CachedRecord? FindCachedCompare(long profileId, string photoSha, string pdfSha16,
                                           string verifierVersion, string? view)
    {
        if (string.IsNullOrWhiteSpace(photoSha)) return null;
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT r.id, r.verdicts_json, r.counts_json, r.quality_json, r.usable, r.code_detector,
       r.code_degraded, r.marks_snapshot_json, r.params_snapshot_json, r.response_json,
       IFNULL(p.pdf_sha256,'')
FROM v2_compare_records r LEFT JOIN v2_drawing_profiles p ON p.id = r.profile_id
WHERE r.profile_id=$pid AND r.photo_sha256=$sha
ORDER BY r.id DESC LIMIT 20";
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$sha", photoSha);
        string Str(JsonObject jo, string key)
        {
            try { return jo[key]?.GetValue<string>() ?? ""; } catch { return ""; }
        }
        using var rd = cmd.ExecuteReader();
        while (rd.Read())
        {
            var pdfSha = rd.IsDBNull(10) ? "" : rd.GetString(10);
            if (pdfSha.Length >= 16 && pdfSha16.Length >= 16
                && !string.Equals(pdfSha.Substring(0, 16), pdfSha16.Substring(0, 16), StringComparison.OrdinalIgnoreCase))
                continue;   // 图纸换版 → 历史结论失效
            var psj = rd.IsDBNull(8) ? "" : rd.GetString(8);
            JsonObject? jo0 = null;
            try { jo0 = JsonNode.Parse(string.IsNullOrWhiteSpace(psj) ? "{}" : psj) as JsonObject; } catch { }
            if (jo0 is null) continue;
            if (!string.Equals(Str(jo0, "verifierVersion"), verifierVersion, StringComparison.Ordinal))
                continue;   // 判定规则升级 → 历史结论失效
            if (!string.Equals(Str(jo0, "selectedView"), view ?? "", StringComparison.OrdinalIgnoreCase))
                continue;   // 拍摄部位不同 → 不是同一输入
            var resp = rd.IsDBNull(9) ? "" : rd.GetString(9);
            if (string.IsNullOrWhiteSpace(resp)) continue;   // 老记录无响应快照 → 不可复用
            // M1 加固（#36）：响应结构升级后旧快照必须失效，否则缓存会掩盖新字段。
            if (resp.IndexOf($"\"schemaVersion\":\"{ResponseSchemaVersion}\"", StringComparison.Ordinal) < 0)
                continue;
            // 代码指纹不同 → 判定逻辑可能已变，旧结论不可复用
            if (resp.IndexOf($"\"code\":\"{CodeFingerprint}\"", StringComparison.Ordinal) < 0)
                continue;
            return new CachedRecord
            {
                Id = rd.GetInt64(0),
                VerdictsJson = rd.IsDBNull(1) ? "[]" : rd.GetString(1),
                CountsJson = rd.IsDBNull(2) ? "{}" : rd.GetString(2),
                QualityJson = rd.IsDBNull(3) ? "[]" : rd.GetString(3),
                Usable = !rd.IsDBNull(4) && rd.GetInt32(4) != 0,
                CodeDetector = rd.IsDBNull(5) ? "" : rd.GetString(5),
                CodeDegraded = !rd.IsDBNull(6) && rd.GetInt32(6) != 0,
                MarksSnapshotJson = rd.IsDBNull(7) ? "" : rd.GetString(7),
                ParamsSnapshotJson = psj,
                ResponseJson = resp
            };
        }
        return null;
    }

    /// <summary>比对记录列表（最近的在前）。</summary>
    /// <summary>
    /// 历史卡片图文同源（2026-09-30）：缩略图按 response_json.blockMatch 画四色框，
    /// 但部分记录 counts_json 为空（或仅存 selected_view）→ 卡片摘要显示「—」与配图不符。
    /// counts 缺失时从 blockMatch.top 派生 green/red/yellow/gray（与配图同一数据源）；
    /// 兼容 e68dd34 之前 PascalCase 落盘的旧响应（nGreen/NGreen 双读）。
    /// </summary>
    private static Dictionary<string, int> MergeBlockMatchCounts(Dictionary<string, int> counts, string? responseJson)
    {
        if (counts.Keys.Any(k => !string.Equals(k, "selected_view", StringComparison.Ordinal)))
            return counts;
        if (string.IsNullOrWhiteSpace(responseJson)) return counts;
        try
        {
            using var doc = JsonDocument.Parse(responseJson);
            if (!doc.RootElement.TryGetProperty("blockMatch", out var bm) || bm.ValueKind != JsonValueKind.Object)
                return counts;
            if (!bm.TryGetProperty("top", out var top) || top.ValueKind != JsonValueKind.Object)
                return counts;
            long G(string k)
            {
                foreach (var key in new[] { k, char.ToUpperInvariant(k[0]) + k[1..] })
                    if (top.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt64(out var n))
                        return n;
                return 0;
            }
            var merged = new Dictionary<string, int>();
            void Add(string key, long v) { if (v > 0) merged[key] = (int)v; }
            Add("green", G("nGreen"));
            Add("red", G("nRed"));
            Add("yellow", G("nYellow"));
            Add("gray", G("nGray"));
            return merged.Count > 0 ? merged : counts;
        }
        catch { return counts; }
    }

    public List<object> ListCompareRecords(int limit = 50)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, profile_id, drawing_key, photo_path, photo_sha256, photo_name, usable,
       code_detector, code_degraded, verify_ms, counts_json, quality_json, created_at, response_json
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
                counts = MergeBlockMatchCounts((Dictionary<string, int>)JsonDict(rd.GetString(10)), rd.IsDBNull(13) ? null : rd.GetString(13)),
                qualityReasons = JsonArr(rd.GetString(11)),
                createdAt = rd.GetString(12)
            });
        return list;
    }

    /// <summary>取某条比对记录的「原始照片路径 + 完整响应 JSON」，供已标示照片渲染（缩略图/原图）。</summary>
    public (string PhotoPath, string ResponseJson)? GetRecordMedia(long id)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT photo_path, response_json FROM v2_compare_records WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", id);
        using var rd = cmd.ExecuteReader();
        if (!rd.Read()) return null;
        var pp = rd.IsDBNull(0) ? "" : rd.GetString(0);
        var rj = rd.IsDBNull(1) ? "" : rd.GetString(1);
        return (pp, rj);
    }

    /// <summary>比对记录详情（含完整 verdicts）。</summary>
    public object? GetCompareRecord(long id)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, profile_id, drawing_key, photo_path, photo_sha256, photo_name, usable,
       code_detector, code_degraded, verify_ms, counts_json, quality_json, verdicts_json, created_at, response_json
FROM v2_compare_records WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", id);
        using var rd = cmd.ExecuteReader();
        if (!rd.Read()) return null;
        var vj = rd.GetString(12);
        // ② 人工判定叠加：原始 verdicts 不动，仅在输出层挂 human 字段
        var reviews = ListVerdictReviews(id);
        object verdicts;
        try
        {
            var node = JsonNode.Parse(vj);
            if (node is JsonArray arr)
            {
                foreach (var n in arr)
                {
                    if (n is not JsonObject o) continue;
                    var mk = o["markKey"]?.GetValue<string>() ?? "";
                    if (mk.Length > 0 && reviews.TryGetValue(mk, out var rv))
                        o["human"] = new JsonObject
                        {
                            ["state"] = rv.HumanState,
                            ["systemState"] = rv.SystemState,
                            ["by"] = rv.By,
                            ["note"] = rv.Note,
                            ["at"] = rv.At
                        };
                }
                verdicts = node;
            }
            else verdicts = node ?? (object)new JsonArray();
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
            counts = MergeBlockMatchCounts((Dictionary<string, int>)JsonDict(rd.GetString(10)), rd.IsDBNull(14) ? null : rd.GetString(14)),
            qualityReasons = JsonArr(rd.GetString(11)),
            verdicts,
            humanApplied = reviews.Count > 0,
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
        public bool Excluded;
        public string? ExcludeReason;
        public List<MarkRow> Children = new();
    }

    private static List<MarkRow> ReadMarkRows(SqliteConnection c, long profileId)
    {
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
SELECT id, parent_id, mark_key, mark_type, view, text, required, condition_text,
       norm_x, norm_y, norm_w, norm_h, bbox_json, direction, confidence,
       rule_id, page_index, span_ids, image_ids, evidence, view_bbox, is_active, confirmed,
       excluded, exclude_reason
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
                Confirmed = rd.GetInt32(22) != 0,
                Excluded = rd.GetInt32(23) != 0,
                ExcludeReason = S(rd, 24)
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
WHERE profile_id=$pid AND parent_id IS NULL AND is_active=1
  AND view IS NOT NULL AND view != 'Unspecified'
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

    /// <summary>载入某档案的生效 mark（含 Group 子项树，类型化）。</summary>
    public List<DrawingMark>? LoadMarks(long profileId)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT COUNT(*) FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", profileId);
        if (Convert.ToInt64(cmd.ExecuteScalar()) == 0) return null;

        var allRows = ReadMarkRows(c, profileId);
        var byRowId = allRows.ToDictionary(r => r.Id);
        bool EffectivelyActive(MarkRow row)
        {
            if (!row.IsActive) return false;
            var parentId = row.ParentId;
            while (parentId is { } id)
            {
                if (!byRowId.TryGetValue(id, out var parent) || !parent.IsActive) return false;
                parentId = parent.ParentId;
            }
            return true;
        }
        var rows = allRows.Where(EffectivelyActive).ToList();
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
            Excluded = r.Excluded,
            ExcludeReason = r.ExcludeReason,
            Children = new List<DrawingMark>()
        };
    }

    /// <summary>
    /// 【B 项·capture】为 QR mark 录入/清除预期解码内容（m.Text）。
    /// 仅对 mark_type='Qr' 生效，其余类型忽略（返回 false）。清空传 null/空串。
    /// </summary>
    public bool SetMarkExpectedText(long profileId, long markId, string? text)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
UPDATE v2_drawing_marks SET text=$t
WHERE profile_id=$pid AND id=$mid AND mark_type='Qr'";
        cmd.Parameters.AddWithValue("$t", (object?)text ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$mid", markId);
        return cmd.ExecuteNonQuery() > 0;
    }

    // ---------------- 人工复核·增删改 mark（设计§3） ----------------
    public long AddMark(long profileId, string markType, string view, string? text,
                         bool required, string? conditionText,
                         double? nx, double? ny, double? nw, double? nh)
    {
        using var c = Open();
        long seq;
        using (var sc = c.CreateCommand())
        {
            sc.CommandText = "SELECT COALESCE(MAX(id),0)+1 FROM v2_drawing_marks WHERE profile_id=$pid";
            sc.Parameters.AddWithValue("$pid", profileId);
            seq = Convert.ToInt64(sc.ExecuteScalar());
        }
        var key = $"manual_{markType}_{seq}";
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
INSERT INTO v2_drawing_marks
 (profile_id, mark_key, mark_type, view, text, required, condition_text,
  norm_x, norm_y, norm_w, norm_h, rule_id, page_index, span_ids, image_ids, evidence, parent_id, is_active, confirmed)
VALUES
 ($pid,$mk,$ty,$vw,$tx,$rq,$cd,$nx,$ny,$nw,$nh,'manual',0,'[]','[]',$ev,NULL,1,0);
SELECT last_insert_rowid();";
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$mk", key);
        cmd.Parameters.AddWithValue("$ty", markType);
        cmd.Parameters.AddWithValue("$vw", string.IsNullOrWhiteSpace(view) ? "Unspecified" : view);
        cmd.Parameters.AddWithValue("$tx", (object?)text ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$rq", required ? 1 : 0);
        cmd.Parameters.AddWithValue("$cd", (object?)conditionText ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$nx", nx.HasValue ? (object)nx.Value : DBNull.Value);
        cmd.Parameters.AddWithValue("$ny", ny.HasValue ? (object)ny.Value : DBNull.Value);
        cmd.Parameters.AddWithValue("$nw", nw.HasValue ? (object)nw.Value : DBNull.Value);
        cmd.Parameters.AddWithValue("$nh", nh.HasValue ? (object)nh.Value : DBNull.Value);
        cmd.Parameters.AddWithValue("$ev", "manual-add");
        var id = Convert.ToInt64(cmd.ExecuteScalar());
        using (var up = c.CreateCommand())
        {
            up.CommandText = "UPDATE v2_drawing_marks SET mark_key=$k WHERE id=$id";
            up.Parameters.AddWithValue("$k", $"manual_{id}");
            up.Parameters.AddWithValue("$id", id);
            up.ExecuteNonQuery();
        }
        return id;
    }

    public bool UpdateMark(long profileId, long markId, string markType, string view, string? text,
                           bool required, string? conditionText,
                           double? nx, double? ny, double? nw, double? nh)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
UPDATE v2_drawing_marks
SET mark_type=$ty, view=$vw, text=$tx, required=$rq, condition_text=$cd,
    norm_x=$nx, norm_y=$ny, norm_w=$nw, norm_h=$nh
WHERE profile_id=$pid AND id=$mid";
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$mid", markId);
        cmd.Parameters.AddWithValue("$ty", markType);
        cmd.Parameters.AddWithValue("$vw", string.IsNullOrWhiteSpace(view) ? "Unspecified" : view);
        cmd.Parameters.AddWithValue("$tx", (object?)text ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$rq", required ? 1 : 0);
        cmd.Parameters.AddWithValue("$cd", (object?)conditionText ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$nx", nx.HasValue ? (object)nx.Value : DBNull.Value);
        cmd.Parameters.AddWithValue("$ny", ny.HasValue ? (object)ny.Value : DBNull.Value);
        cmd.Parameters.AddWithValue("$nw", nw.HasValue ? (object)nw.Value : DBNull.Value);
        cmd.Parameters.AddWithValue("$nh", nh.HasValue ? (object)nh.Value : DBNull.Value);
        return cmd.ExecuteNonQuery() > 0;
    }

    public bool DeleteMark(long profileId, long markId)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "DELETE FROM v2_drawing_marks WHERE profile_id=$pid AND id=$mid";
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$mid", markId);
        return cmd.ExecuteNonQuery() > 0;
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
        public List<string> HumanStates = new();
        public object? HumanLatest;
        public string Rollup()
        {
            // ② 人工判定优先：只要人确认过，就以人工结论为准（系统判定仅作参考留痕）
            if (HumanStates.Count > 0)
            {
                if (HumanStates.Any(s => s == "HumanMissing")) return "Missing";
                if (HumanStates.Any(s => s == "HumanWrongPart")) return "Wrong";
                if (HumanStates.All(s => s == "HumanPresent")) return "Matched";
                return "NeedReview";
            }
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
        // ② 会话内人工判定按 markKey 汇总（人工优先于系统判定）
        var humanByMark = LoadVerdictReviewsOfSession(c, sessionId);
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
                    if (humanByMark.TryGetValue(mk, out var hl) && hl.Count > 0)
                    {
                        a.HumanStates.AddRange(hl.Select(x => x.State));
                        var last = hl[^1];
                        a.HumanLatest = new { state = last.State, by = last.By, note = last.Note, at = last.At };
                    }
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
            humanStates = a.HumanStates.Distinct().ToList(),
            human = a.HumanLatest,
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
    public const string DecisionVersion = "m2.3-optimized";

    // ---------------- 逻辑图块仓储（P2 · 方案 §4） ----------------

    /// <summary>逻辑图块写入单元（Python logical-blocks/1 契约的 C# 映射）。</summary>
    public sealed class LogicalBlockRow
    {
        public int BlockIndex { get; set; }
        public string BlockKey { get; set; } = "";
        public string Name { get; set; } = "";
        public string NameFp { get; set; } = "";
        public double[]? Norm { get; set; }          // [x,y,w,h] 归一化
        public string? BboxJson { get; set; }        // [x0,y0,x1,y1] pt
        public string ViewHint { get; set; } = "Unspecified";
        public double AreaPct { get; set; }
        public int HasMarking { get; set; } = 1;
        public string? EmptyReason { get; set; }
        public int LowConf { get; set; }
        public string AlgoVersion { get; set; } = "";
        public string? GeomFp { get; set; }
        public int NElements { get; set; }
        public int NParticipate { get; set; }
        public int NImage { get; set; }
        public List<LogicalElementRow> Elements { get; set; } = new();
    }

    /// <summary>块内元素写入单元（5 分类见方案 §2）。</summary>
    public sealed class LogicalElementRow
    {
        public string Kind { get; set; } = "";
        public string? Text { get; set; }
        public double? OcrConf { get; set; }
        public double[]? Norm { get; set; }
        public string? BboxJson { get; set; }
        public int Participate { get; set; }
        public string Source { get; set; } = "";
    }

    /// <summary>读取结果单元（可变类：elements 需回填，匿名类型做不到）。</summary>
    public sealed class LogicalBlockView
    {
        public long Id { get; set; }
        public int PageIndex { get; set; }
        public string BlockKey { get; set; } = "";
        public int BlockIndex { get; set; }
        public string Name { get; set; } = "";
        public string NameFp { get; set; } = "";
        public double?[] Norm { get; set; } = new double?[4];
        public string? BboxJson { get; set; }
        public double AreaPct { get; set; }
        public string ViewHint { get; set; } = "Unspecified";
        public bool HasMarking { get; set; }
        public string? EmptyReason { get; set; }
        public bool LowConf { get; set; }
        public string AlgoVersion { get; set; } = "";
        public string? GeomFp { get; set; }
        public int NElements { get; set; }
        public int NParticipate { get; set; }
        public int NImage { get; set; }
        public string CreatedAt { get; set; } = "";
        public List<LogicalElementView> Elements { get; set; } = new();
    }

    /// <summary>读取结果：块内元素。</summary>
    public sealed class LogicalElementView
    {
        public string Kind { get; set; } = "";
        public string? Text { get; set; }
        public double? OcrConf { get; set; }
        public double?[] Norm { get; set; } = new double?[4];
        public string? BboxJson { get; set; }
        public bool Participate { get; set; }
        public bool IsAnchor { get; set; }
        public string Source { get; set; } = "";
    }

    /// <summary>取 profile 的 drawing_key / pdf_path / pdf_sha256（供逻辑图块提取定位源文件）。</summary>
    public (string drawingKey, string pdfPath, string sha256)? GetProfileLocation(long id)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT drawing_key, pdf_path, pdf_sha256 FROM v2_drawing_profiles WHERE id=$id";
        cmd.Parameters.AddWithValue("$id", id);
        using var rd = cmd.ExecuteReader();
        if (!rd.Read()) return null;
        return (rd.GetString(0), rd.GetString(1), rd.GetString(2));
    }

    /// <summary>
    /// 整体替换某 profile 某页的逻辑图块（幂等覆盖）。
    ///
    /// <para>先删该页旧 elements 再删旧 blocks，最后写入 —— 保证「同一 profile 重跑不产生重复行」，
    /// 也让 has_marking 口径变化（如判空规则调整）能干净地整体重算。</para>
    /// </summary>
    /// <returns>(块数, 元素数, 有打标块数)</returns>
    public (int blocks, int elements, int hasMarking) ReplaceLogicalBlocks(
        long profileId, int pageIndex, IReadOnlyList<LogicalBlockRow> rows)
    {
        var now = DateTime.UtcNow.ToString("o");
        using var c = Open();
        using var tx = c.BeginTransaction();

        // 1) 删旧：先 elements 再 blocks（顺序不可颠倒）
        using (var del = c.CreateCommand())
        {
            del.Transaction = tx;
            del.CommandText = @"
DELETE FROM v2_block_elements
 WHERE block_id IN (SELECT id FROM v2_logical_blocks WHERE profile_id=$p AND page_index=$g)";
            del.Parameters.AddWithValue("$p", profileId);
            del.Parameters.AddWithValue("$g", pageIndex);
            del.ExecuteNonQuery();
        }
        using (var del2 = c.CreateCommand())
        {
            del2.Transaction = tx;
            del2.CommandText = "DELETE FROM v2_logical_blocks WHERE profile_id=$p AND page_index=$g";
            del2.Parameters.AddWithValue("$p", profileId);
            del2.Parameters.AddWithValue("$g", pageIndex);
            del2.ExecuteNonQuery();
        }

        // 2) 写新
        int nEl = 0, nHas = 0;
        foreach (var b in rows)
        {
            long blockId;
            using (var ins = c.CreateCommand())
            {
                ins.Transaction = tx;
                ins.CommandText = @"
INSERT INTO v2_logical_blocks
 (profile_id, page_index, block_key, block_index, name, name_fp,
  norm_x, norm_y, norm_w, norm_h, bbox_json, area_pct, view_hint,
  has_marking, empty_reason, low_conf, algo_version, geom_fp,
  n_elements, n_participate, n_image, created_at)
VALUES
 ($pid,$pg,$bk,$bi,$nm,$fp,$nx,$ny,$nw,$nh,$bb,$ap,$vh,$hm,$er,$lc,$av,$gf,$ne,$np,$ni,$ts);
SELECT last_insert_rowid();";
                ins.Parameters.AddWithValue("$pid", profileId);
                ins.Parameters.AddWithValue("$pg", pageIndex);
                ins.Parameters.AddWithValue("$bk", b.BlockKey);
                ins.Parameters.AddWithValue("$bi", b.BlockIndex);
                ins.Parameters.AddWithValue("$nm", b.Name ?? "");
                ins.Parameters.AddWithValue("$fp", b.NameFp ?? "");
                AddNullable(ins, "$nx", b.Norm is { Length: 4 } n0 ? n0[0] : (double?)null);
                AddNullable(ins, "$ny", b.Norm is { Length: 4 } n1 ? n1[1] : (double?)null);
                AddNullable(ins, "$nw", b.Norm is { Length: 4 } n2 ? n2[2] : (double?)null);
                AddNullable(ins, "$nh", b.Norm is { Length: 4 } n3 ? n3[3] : (double?)null);
                ins.Parameters.AddWithValue("$bb", (object?)b.BboxJson ?? DBNull.Value);
                ins.Parameters.AddWithValue("$ap", b.AreaPct);
                ins.Parameters.AddWithValue("$vh", b.ViewHint ?? "Unspecified");
                ins.Parameters.AddWithValue("$hm", b.HasMarking);
                ins.Parameters.AddWithValue("$er", (object?)b.EmptyReason ?? DBNull.Value);
                ins.Parameters.AddWithValue("$lc", b.LowConf);
                ins.Parameters.AddWithValue("$av", b.AlgoVersion ?? "");
                ins.Parameters.AddWithValue("$gf", (object?)b.GeomFp ?? DBNull.Value);
                ins.Parameters.AddWithValue("$ne", b.NElements);
                ins.Parameters.AddWithValue("$np", b.NParticipate);
                ins.Parameters.AddWithValue("$ni", b.NImage);
                ins.Parameters.AddWithValue("$ts", now);
                blockId = Convert.ToInt64(ins.ExecuteScalar());
            }
            if (b.HasMarking != 0) nHas++;

            foreach (var e in b.Elements)
            {
                using var ie = c.CreateCommand();
                ie.Transaction = tx;
                ie.CommandText = @"
INSERT INTO v2_block_elements
 (block_id, kind, text, ocr_conf, norm_x, norm_y, norm_w, norm_h, bbox_json, participate, is_anchor, source)
VALUES
 ($b,$k,$t,$cf,$nx,$ny,$nw,$nh,$bb,$pa,0,$src)";
                ie.Parameters.AddWithValue("$b", blockId);
                ie.Parameters.AddWithValue("$k", e.Kind ?? "");
                ie.Parameters.AddWithValue("$t", (object?)e.Text ?? DBNull.Value);
                AddNullable(ie, "$cf", e.OcrConf);
                AddNullable(ie, "$nx", e.Norm is { Length: 4 } q0 ? q0[0] : (double?)null);
                AddNullable(ie, "$ny", e.Norm is { Length: 4 } q1 ? q1[1] : (double?)null);
                AddNullable(ie, "$nw", e.Norm is { Length: 4 } q2 ? q2[2] : (double?)null);
                AddNullable(ie, "$nh", e.Norm is { Length: 4 } q3 ? q3[3] : (double?)null);
                ie.Parameters.AddWithValue("$bb", (object?)e.BboxJson ?? DBNull.Value);
                ie.Parameters.AddWithValue("$pa", e.Participate);
                ie.Parameters.AddWithValue("$src", e.Source ?? "");
                ie.ExecuteNonQuery();
                nEl++;
            }
        }

        tx.Commit();
        return (rows.Count, nEl, nHas);
    }

    private static void AddNullable(SqliteCommand cmd, string name, double? v)
    {
        if (v.HasValue) cmd.Parameters.AddWithValue(name, v.Value);
        else cmd.Parameters.AddWithValue(name, DBNull.Value);
    }

    /// <summary>
    /// 读取某 profile 的逻辑图块。
    /// <para><paramref name="hasMarkingOnly"/> 为 true 时只返回 <c>has_marking=1</c> 的块 ——
    /// 即 P3 命中检索的候选集；空块在此被天然隔离，见方案 §2.3。</para>
    /// </summary>
    public List<LogicalBlockView> ListLogicalBlocks(long profileId, bool hasMarkingOnly = false,
        bool withElements = true)
    {
        using var c = Open();

        var where = hasMarkingOnly
            ? "WHERE profile_id=$p AND has_marking=1"
            : "WHERE profile_id=$p";

        var blocks = new List<LogicalBlockView>();
        using (var cmd = c.CreateCommand())
        {
            cmd.CommandText =
@"SELECT id, page_index, block_key, block_index, name, name_fp,
       norm_x, norm_y, norm_w, norm_h, bbox_json, area_pct, view_hint,
       has_marking, empty_reason, low_conf, algo_version, geom_fp,
       n_elements, n_participate, n_image, created_at
FROM v2_logical_blocks " + where + " ORDER BY block_index";
            cmd.Parameters.AddWithValue("$p", profileId);
            using var rd = cmd.ExecuteReader();
            while (rd.Read())
            {
                blocks.Add(new LogicalBlockView
                {
                    Id = rd.GetInt64(0),
                    PageIndex = rd.GetInt32(1),
                    BlockKey = rd.GetString(2),
                    BlockIndex = rd.GetInt32(3),
                    Name = rd.GetString(4),
                    NameFp = rd.GetString(5),
                    Norm = new[] { Nul(rd, 6), Nul(rd, 7), Nul(rd, 8), Nul(rd, 9) },
                    BboxJson = RdStr(rd, 10),
                    AreaPct = rd.GetDouble(11),
                    ViewHint = rd.GetString(12),
                    HasMarking = rd.GetInt32(13) != 0,
                    EmptyReason = RdStr(rd, 14),
                    LowConf = rd.GetInt32(15) != 0,
                    AlgoVersion = rd.GetString(16),
                    GeomFp = RdStr(rd, 17),
                    NElements = rd.GetInt32(18),
                    NParticipate = rd.GetInt32(19),
                    NImage = rd.GetInt32(20),
                    CreatedAt = rd.GetString(21)
                });
            }
        }

        if (!withElements || blocks.Count == 0) return blocks;

        using (var cmd = c.CreateCommand())
        {
            var ph = new System.Text.StringBuilder();
            for (int i = 0; i < blocks.Count; i++)
            {
                if (i > 0) ph.Append(',');
                ph.Append("$i").Append(i);
            }
            cmd.CommandText =
@"SELECT block_id, kind, text, ocr_conf, norm_x, norm_y, norm_w, norm_h,
       bbox_json, participate, is_anchor, source
FROM v2_block_elements WHERE block_id IN (" + ph + ") ORDER BY id";
            for (int i = 0; i < blocks.Count; i++)
                cmd.Parameters.AddWithValue("$i" + i, blocks[i].Id);

            using var rd = cmd.ExecuteReader();
            while (rd.Read())
            {
                long bid = rd.GetInt64(0);
                LogicalBlockView? host = null;
                for (int i = 0; i < blocks.Count; i++)
                    if (blocks[i].Id == bid) { host = blocks[i]; break; }
                if (host is null) continue;
                host.Elements.Add(new LogicalElementView
                {
                    Kind = rd.GetString(1),
                    Text = RdStr(rd, 2),
                    OcrConf = Nul(rd, 3),
                    Norm = new[] { Nul(rd, 4), Nul(rd, 5), Nul(rd, 6), Nul(rd, 7) },
                    BboxJson = RdStr(rd, 8),
                    Participate = rd.GetInt32(9) != 0,
                    IsAnchor = rd.GetInt32(10) != 0,
                    Source = rd.GetString(11)
                });
            }
        }

        return blocks;
    }

    private static double? Nul(SqliteDataReader rd, int i) => rd.IsDBNull(i) ? null : rd.GetDouble(i);
    private static string? RdStr(SqliteDataReader rd, int i) => rd.IsDBNull(i) ? null : rd.GetString(i);

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
