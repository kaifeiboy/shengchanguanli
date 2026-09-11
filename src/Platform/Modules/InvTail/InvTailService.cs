using System.Text.Json;

namespace Platform.Modules.InvTail;

/// <summary>
/// 库存尾数业务层：承载全部业务规则，不依赖 HTTP 类型（IFormFile 除外，仅用于落盘）。
/// <para>已确认的四条铁律：</para>
/// <list type="number">
/// <item>新增材料填写的数量 = 首笔入库流水（经手人 = 新增人），库存永远 = 流水累加。</item>
/// <item>库号唯一、编码唯一；重复录入直接拦截并回传已存在记录，供前端跳转做出入库。</item>
/// <item>出库数量大于当前库存 → 拦截，库存永不为负。</item>
/// <item>经手人自动沉淀，最后操作的人排最前。</item>
/// </list>
/// </summary>
public sealed class InvTailService
{
    private readonly InvTailStore _store;
    private readonly InvTailPython _py;
    private readonly string _ocrDir;
    private readonly string _exportDir;

    public InvTailService(InvTailStore store, InvTailPython py)
    {
        _store = store;
        _py = py;
        _ocrDir = Path.Combine(store.DataDir, "invtail", "ocr");
        _exportDir = Path.Combine(store.DataDir, "invtail", "export");
        try { Directory.CreateDirectory(_ocrDir); } catch { }
        try { Directory.CreateDirectory(_exportDir); } catch { }
    }

    private static string Now() => DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");

    // ---------------- 校验 ----------------

    private static string Req(string? v, string label, int max = 200)
    {
        var s = (v ?? "").Trim();
        if (s.Length == 0) throw new InvTailException($"请填写{label}");
        if (s.Length > max) throw new InvTailException($"{label}过长（不超过 {max} 字）");
        return s;
    }

    /// <summary>编码软校验：业务规律是 10 位数字，但不强制拦截（避免特殊料号被误伤），仅回传提示。</summary>
    private static string? CodeHint(string code)
    {
        var digits = code.All(char.IsDigit);
        if (digits && code.Length == 10) return null;
        return digits
            ? $"编码为 {code.Length} 位数字（常规为 10 位），请确认无误"
            : "编码含非数字字符（常规为 10 位数字），请确认无误";
    }

    // ---------------- 概览 / 经手人 ----------------

    public object Meta()
    {
        var (materials, records, totalStock) = _store.Stats();
        return new
        {
            ok = true,
            db = Path.GetFileName(_store.DbPath),
            materials,
            records,
            totalStock = InvNum.Fmt(totalStock),
            handlers = _store.ListHandlers().Select(h => new { name = h.name, lastUsedAt = h.lastUsedAt, useCount = h.useCount })
        };
    }

    /// <summary>经手人下拉：最后操作的人排最前。</summary>
    public object Handlers()
        => new
        {
            ok = true,
            items = _store.ListHandlers().Select(h => new { name = h.name, lastUsedAt = h.lastUsedAt, useCount = h.useCount })
        };

    /// <summary>删除经手人：只从下拉候选移除，历史流水里的名字原样保留（不动已记账数据）。</summary>
    public object DeleteHandler(string? name)
    {
        var n = Req(name, "经手人", 40);
        var affected = _store.DeleteHandler(n);
        return new { ok = true, name = n, deleted = affected };
    }

    /// <summary>经手人改名：默认只改候选名；syncRecords=true 时把历史流水中的旧名一并改为新名。</summary>
    public object RenameHandler(string? oldName, string? newName, bool syncRecords)
    {
        var o = Req(oldName, "原经手人", 40);
        var nw = Req(newName, "新经手人", 40);
        if (string.Equals(o, nw, StringComparison.Ordinal))
            throw new InvTailException("新名字与原名字相同");
        var changed = _store.RenameHandler(o, nw, syncRecords);
        return new { ok = true, oldName = o, newName = nw, syncedRecords = syncRecords, changed };
    }

    /// <summary>修改某笔流水的经手人：只改经手人，不动方向与数量，因此库存不受影响。</summary>
    public object UpdateRecordHandler(long recordId, string? handler)
    {
        var h = Req(handler, "经手人", 40);
        var mid = _store.UpdateRecordHandler(recordId, h);
        if (mid == 0) throw new InvTailException("流水不存在或已被删除");
        _store.TouchHandler(h, Now());
        var m = _store.GetMaterial(mid);
        return new
        {
            ok = true,
            recordId = recordId,
            materialId = mid,
            handler = h,
            stock = m == null ? "0" : InvNum.Fmt(m.Stock)
        };
    }

    // ---------------- 材料主档 ----------------

    private static object Shape(InvMaterial m) => new
    {
        id = m.Id,
        binNo = m.BinNo,
        code = m.Code,
        materialInfo = m.MaterialInfo,
        stock = InvNum.Fmt(m.Stock),
        stockValue = m.Stock,
        recordCount = m.RecordCount,
        lastMovedAt = m.LastMovedAt,
        lastHandler = m.LastHandler,
        createdAt = m.CreatedAt,
        createdBy = m.CreatedBy
    };

    /// <summary>新增材料（数量记为首笔入库流水）。库号/编码任一重复即拦截。</summary>
    public object CreateMaterial(InvCreateRequest req)
    {
        var m = CreateMaterialCore(req);
        return new { ok = true, material = Shape(m), hint = CodeHint(m.Code) };
    }

    /// <summary>新增核心逻辑（返回领域对象，供单条与批量共用）。</summary>
    private InvMaterial CreateMaterialCore(InvCreateRequest req)
    {
        var binNo = Req(req.BinNo, "库号", 60);
        var code = Req(req.Code, "编码", 60);
        var info = Req(req.MaterialInfo, "材料信息", 500);
        var handler = Req(req.Handler, "经手人", 40);
        var qty = InvNum.ParsePositive(req.Qty, "数量");

        var dupBin = _store.GetByBinNo(binNo);
        if (dupBin != null)
            throw new InvDuplicateException("binNo", dupBin,
                $"库号「{binNo}」已存在（{dupBin.MaterialInfo}，当前库存 {InvNum.Fmt(dupBin.Stock)}），请直接对该材料做出入库");
        var dupCode = _store.GetByCode(code);
        if (dupCode != null)
            throw new InvDuplicateException("code", dupCode,
                $"编码「{code}」已存在（库号 {dupCode.BinNo}，当前库存 {InvNum.Fmt(dupCode.Stock)}），请直接对该材料做出入库");

        var now = Now();
        long id;
        try
        {
            id = _store.InsertMaterialWithOpeningRecord(binNo, code, info, qty, handler, req.Note ?? "", now);
        }
        catch (Microsoft.Data.Sqlite.SqliteException e) when (e.SqliteErrorCode == 19)
        {
            // UNIQUE 兜底（并发下两人同时提交同一库号/编码）
            throw new InvTailException("库号或编码已被占用（并发提交），请刷新后重试");
        }
        _store.TouchHandler(handler, now);
        return _store.GetMaterial(id)!;
    }

    /// <summary>
    /// 批量新增（拍照识别一次框选多条时使用）。逐条独立处理，失败不影响其它条目，
    /// 返回成功列表 + 失败原因列表，避免"一条错全部回滚"造成重复劳动。
    /// </summary>
    public object CreateMaterialsBatch(List<InvCreateRequest> items, string? fallbackHandler)
    {
        if (items == null || items.Count == 0) throw new InvTailException("没有待录入的条目");
        if (items.Count > 100) throw new InvTailException("单次最多录入 100 条");

        var okList = new List<object>();
        var failList = new List<object>();
        for (int i = 0; i < items.Count; i++)
        {
            var it = items[i];
            if (string.IsNullOrWhiteSpace(it.Handler)) it.Handler = fallbackHandler;
            try
            {
                var m = CreateMaterialCore(it);
                okList.Add(new { index = i, material = Shape(m) });
            }
            catch (InvDuplicateException e)
            {
                failList.Add(new { index = i, code = it.Code ?? "", binNo = it.BinNo ?? "", field = e.Field, error = e.Message, existingId = e.Existing.Id });
            }
            catch (InvTailException e)
            {
                failList.Add(new { index = i, code = it.Code ?? "", binNo = it.BinNo ?? "", field = "", error = e.Message, existingId = 0L });
            }
        }
        return new { ok = true, inserted = okList.Count, failed = failList.Count, items = okList, errors = failList };
    }

    /// <summary>模糊查询：库号 / 编码 / 材料信息 任一命中。</summary>
    public object Search(string? q, int page, int size)
    {
        var (rows, total) = _store.SearchMaterials(q, page, size);
        return new
        {
            ok = true,
            keyword = (q ?? "").Trim(),
            total,
            page = page < 1 ? 1 : page,
            size = size < 1 ? 20 : size,
            items = rows.Select(Shape)
        };
    }

    /// <summary>材料详情 + 出入库流水（倒序，含逐笔结存）。</summary>
    public object Detail(long id)
    {
        var m = _store.GetMaterial(id) ?? throw new InvTailException("材料不存在或已被删除");
        var recs = _store.ListRecords(id);
        return new
        {
            ok = true,
            material = Shape(m),
            records = recs.Select(r => new
            {
                id = r.Id,
                direction = r.Direction,
                directionLabel = InvDirection.ToLabel(r.Direction),
                qty = InvNum.Fmt(r.Qty),
                balance = InvNum.Fmt(r.Balance),
                handler = r.Handler,
                note = r.Note,
                occurredAt = r.OccurredAt
            })
        };
    }

    /// <summary>修改材料主档（库号/编码/材料信息），唯一性同样校验。</summary>
    public object UpdateMaterial(long id, InvCreateRequest req)
    {
        var m = _store.GetMaterial(id) ?? throw new InvTailException("材料不存在或已被删除");
        var binNo = Req(req.BinNo, "库号", 60);
        var code = Req(req.Code, "编码", 60);
        var info = Req(req.MaterialInfo, "材料信息", 500);

        var dupBin = _store.GetByBinNo(binNo);
        if (dupBin != null && dupBin.Id != id)
            throw new InvDuplicateException("binNo", dupBin, $"库号「{binNo}」已被其它材料占用");
        var dupCode = _store.GetByCode(code);
        if (dupCode != null && dupCode.Id != id)
            throw new InvDuplicateException("code", dupCode, $"编码「{code}」已被其它材料占用");

        _store.UpdateMaterial(id, binNo, code, info);
        return new { ok = true, material = Shape(_store.GetMaterial(id)!), hint = CodeHint(code) };
    }

    /// <summary>删除材料（连带流水，误建档纠正用）。</summary>
    public object DeleteMaterial(long id)
    {
        var m = _store.GetMaterial(id) ?? throw new InvTailException("材料不存在或已被删除");
        var n = _store.DeleteMaterial(id);
        return new { ok = true, deleted = n, binNo = m.BinNo, code = m.Code };
    }

    // ---------------- 出入库 ----------------

    /// <summary>出入库记账：记录数量 + 当前时间 + 经手人；出库超量拦截。</summary>
    public object Move(long id, InvMoveRequest req)
    {
        var m = _store.GetMaterial(id) ?? throw new InvTailException("材料不存在或已被删除");
        var dir = InvDirection.Normalize(req.Direction);
        var qty = InvNum.ParsePositive(req.Qty, dir == InvDirection.Out ? "出库数量" : "入库数量");
        var handler = Req(req.Handler, "经手人", 40);
        var now = Now();

        var (rid, stock) = _store.InsertRecord(id, dir, qty, handler, (req.Note ?? "").Trim(), now);
        _store.TouchHandler(handler, now);

        return new
        {
            ok = true,
            recordId = rid,
            direction = dir,
            directionLabel = InvDirection.ToLabel(dir),
            qty = InvNum.Fmt(qty),
            stock = InvNum.Fmt(stock),
            stockValue = stock,
            handler,
            occurredAt = now,
            material = Shape(_store.GetMaterial(id)!)
        };
    }

    /// <summary>删除一笔流水（误录纠正），返回该材料最新库存。</summary>
    public object DeleteRecord(long recordId)
    {
        var mid = _store.DeleteRecord(recordId);
        if (mid == 0) throw new InvTailException("流水不存在或已被删除");
        var m = _store.GetMaterial(mid);
        return new { ok = true, materialId = mid, stock = m == null ? "0" : InvNum.Fmt(m.Stock) };
    }

    // ---------------- 拍照识别（透视矫正 + 白平衡 + 框选） ----------------

    private string SessionDir(string sid)
    {
        if (string.IsNullOrWhiteSpace(sid) || sid.Length > 40 || sid.Any(ch => !char.IsLetterOrDigit(ch)))
            throw new InvTailException("会话标识不合法");
        var d = Path.Combine(_ocrDir, sid);
        if (!Directory.Exists(d)) throw new InvTailException("拍照会话已过期，请重新拍照");
        return d;
    }

    /// <summary>清理 3 小时前的拍照会话，避免磁盘无限增长。</summary>
    private void CleanupSessions()
    {
        try
        {
            var deadline = DateTime.Now.AddHours(-3);
            foreach (var d in Directory.GetDirectories(_ocrDir))
            {
                try { if (Directory.GetLastWriteTime(d) < deadline) Directory.Delete(d, true); } catch { }
            }
            foreach (var f in Directory.GetFiles(_exportDir))
            {
                try { if (File.GetLastWriteTime(f) < DateTime.Now.AddDays(-2)) File.Delete(f); } catch { }
            }
        }
        catch { }
    }

    /// <summary>
    /// 第一步：上传照片 → 自动检测文档四角做透视畸变矫正 + 白平衡 → 返回处理后图供 H5 展示与框选。
    /// 不直接识别，识别只在用户框选后针对 ROI 进行，符合"只识别框选内容"的要求。
    /// </summary>
    public object OcrPrepare(IFormFile file, string? whiteBalance, bool autoWarp)
    {
        if (file == null || file.Length == 0) throw new InvTailException("未收到照片");
        if (file.Length > 25 * 1024 * 1024) throw new InvTailException("照片过大（超过 25MB）");
        var ext = Path.GetExtension(file.FileName).ToLowerInvariant();
        if (ext != ".jpg" && ext != ".jpeg" && ext != ".png" && ext != ".bmp" && ext != ".webp")
            ext = ".jpg"; // H5 canvas 直传可能无扩展名，默认按 jpg 落盘（Python 用 imdecode 按内容解析）

        CleanupSessions();
        var sid = Guid.NewGuid().ToString("N")[..16];
        var dir = Path.Combine(_ocrDir, sid);
        Directory.CreateDirectory(dir);
        var src = Path.Combine(dir, "src" + ext);
        using (var fs = File.Create(src)) file.CopyTo(fs);

        var args = new List<string>
        {
            "prepare", "--src", src, "--dir", dir,
            "--wb", NormalizeWb(whiteBalance),
            "--auto-warp", autoWarp ? "1" : "0"
        };
        using var doc = _py.Run("inv_ocr.py", args, timeoutMs: 120_000);
        var root = doc.RootElement;
        return new
        {
            ok = true,
            sessionId = sid,
            width = GetInt(root, "width"),
            height = GetInt(root, "height"),
            srcWidth = GetInt(root, "srcWidth"),
            srcHeight = GetInt(root, "srcHeight"),
            warped = GetBool(root, "warped"),
            corners = GetCorners(root),
            whiteBalance = GetStr(root, "wb"),
            imageUrl = $"/api/invtail/ocr/image/{sid}?v={DateTime.Now.Ticks}"
        };
    }

    /// <summary>
    /// 手动微调四角 / 切换白平衡后重新矫正（原图仍在会话目录，无需重新上传，手机流量友好）。
    /// corners 为原图坐标系下 4 点：左上、右上、右下、左下。传空则取消矫正只做白平衡。
    /// </summary>
    public object OcrRewarp(JsonElement body)
    {
        var sid = GetStr(body, "sessionId");
        var dir = SessionDir(sid);
        var wb = NormalizeWb(GetStr(body, "whiteBalance"));
        var args = new List<string> { "rewarp", "--dir", dir, "--wb", wb };
        if (body.TryGetProperty("corners", out var cs) && cs.ValueKind == JsonValueKind.Array && cs.GetArrayLength() == 4)
        {
            // 角点基于当前显示图（warp.jpg）坐标系：与前端点击/框选坐标一致，避免落到原图坐标系产生错位
            args.AddRange(new[] { "--corners", cs.GetRawText(), "--from-warp", "1" });
        }
        else
            args.AddRange(new[] { "--no-warp", "1" });

        using var doc = _py.Run("inv_ocr.py", args, timeoutMs: 120_000);
        var root = doc.RootElement;
        return new
        {
            ok = true,
            sessionId = sid,
            width = GetInt(root, "width"),
            height = GetInt(root, "height"),
            warped = GetBool(root, "warped"),
            whiteBalance = GetStr(root, "wb"),
            imageUrl = $"/api/invtail/ocr/image/{sid}?v={DateTime.Now.Ticks}"
        };
    }

    /// <summary>返回会话处理后的图片（矫正+白平衡结果），供 H5 画布加载做框选。</summary>
    public IResult OcrImage(HttpContext ctx, string sid)
    {
        var dir = SessionDir(sid);
        var warp = Path.Combine(dir, "warp.jpg");
        if (!File.Exists(warp)) return Results.NotFound(new { error = "图片尚未生成" });
        ctx.Response.Headers["Cache-Control"] = "private, max-age=600";
        return Results.File(warp, "image/jpeg");
    }

    /// <summary>
    /// 第二步：只识别框选区域。rois = [[x,y,w,h], ...]（矫正后图坐标系）。
    /// 每个框独立解析为一条：10 位数字 → 编码；其余文字 → 材料信息。
    /// 连续框选多条即返回多条，交由前端批量录入。
    /// </summary>
    public object OcrRecognize(JsonElement body)
    {
        var sid = GetStr(body, "sessionId");
        var dir = SessionDir(sid);
        if (!body.TryGetProperty("rois", out var rois) || rois.ValueKind != JsonValueKind.Array || rois.GetArrayLength() == 0)
            throw new InvTailException("请先在照片上框选要识别的区域");
        if (rois.GetArrayLength() > 20) throw new InvTailException("单次最多框选 20 个区域");

        var args = new List<string> { "recognize", "--dir", dir, "--rois", rois.GetRawText() };
        using var doc = _py.Run("inv_ocr.py", args, timeoutMs: 240_000, heavy: true);
        var root = doc.RootElement;

        var items = new List<object>();
        if (root.TryGetProperty("items", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var it in arr.EnumerateArray())
            {
                var code = GetStr(it, "code");
                var info = GetStr(it, "materialInfo");
                var lines = new List<string>();
                if (it.TryGetProperty("lines", out var la) && la.ValueKind == JsonValueKind.Array)
                    foreach (var l in la.EnumerateArray()) lines.Add(l.GetString() ?? "");
                // 该编码是否已建档：前端可直接提示"已存在，去出入库"
                InvMaterial? exist = string.IsNullOrEmpty(code) ? null : _store.GetByCode(code);
                items.Add(new
                {
                    roiIndex = GetInt(it, "roiIndex"),
                    code,
                    materialInfo = info,
                    lines,
                    codeHint = string.IsNullOrEmpty(code) ? "未识别到 10 位数字编码，请手工补填" : CodeHint(code),
                    existing = exist == null ? null : Shape(exist)
                });
            }
        }
        return new { ok = true, sessionId = sid, count = items.Count, items };
    }

    private static string NormalizeWb(string? wb)
    {
        var s = (wb ?? "").Trim().ToLowerInvariant();
        return s switch
        {
            "none" or "off" => "none",
            "white" or "whitepatch" => "white",
            "gray" => "gray",
            "auto" => "auto",
            _ => "auto"   // 空 / 未知 → 默认 auto：按图像偏色自动选 white/gray（白底文档明显且稳）
        };
    }

    // ---------------- 导出 ----------------

    /// <summary>
    /// 导出出入库明细流水（xlsx）：一行一笔，含 库号/编码/材料信息/方向/数量/结存/时间/经手人，用于追溯核账。
    /// 支持与查询一致的模糊条件 + 经手人 / 方向 / 日期区间过滤。
    /// </summary>
    public IResult Export(string? q, string? handler, string? direction, string? dateFrom, string? dateTo)
    {
        var rows = _store.ExportRecords(q, handler, direction, dateFrom, dateTo);
        if (rows.Count == 0) throw new InvTailException("当前条件下没有出入库流水可导出");

        CleanupSessions();
        var stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        var jsonPath = Path.Combine(_exportDir, $"data_{stamp}_{Guid.NewGuid().ToString("N")[..6]}.json");
        var outPath = Path.Combine(_exportDir, $"库存尾数出入库明细_{stamp}.xlsx");

        var payload = new
        {
            title = "库存尾数 · 出入库明细流水",
            generatedAt = Now(),
            filter = new
            {
                keyword = (q ?? "").Trim(),
                handler = (handler ?? "").Trim(),
                direction = (direction ?? "").Trim(),
                dateFrom = (dateFrom ?? "").Trim(),
                dateTo = (dateTo ?? "").Trim()
            },
            rows = rows.Select(r => new
            {
                binNo = r.BinNo,
                code = r.Code,
                materialInfo = r.MaterialInfo,
                direction = InvDirection.ToLabel(r.Direction),
                qty = r.Qty,
                balance = r.Balance,
                occurredAt = r.OccurredAt,
                handler = r.Handler,
                note = r.Note
            })
        };
        File.WriteAllText(jsonPath, JsonSerializer.Serialize(payload,
            new JsonSerializerOptions { WriteIndented = false }),
            new System.Text.UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        try
        {
            using var doc = _py.Run("inv_export.py", new[] { "--data", jsonPath, "--out", outPath }, timeoutMs: 120_000);
            if (!File.Exists(outPath)) throw new InvTailException("导出文件生成失败");
            return Results.File(outPath,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                $"库存尾数出入库明细_{stamp}.xlsx");
        }
        finally
        {
            try { if (File.Exists(jsonPath)) File.Delete(jsonPath); } catch { }
        }
    }

    // ---------------- JSON 小工具 ----------------

    private static string GetStr(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return "";
        return v.ValueKind switch
        {
            JsonValueKind.String => v.GetString() ?? "",
            JsonValueKind.Number => v.GetRawText(),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            _ => ""
        };
    }

    private static int GetInt(JsonElement el, string name)
        => el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v)
           && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var i) ? i : 0;

    private static bool GetBool(JsonElement el, string name)
        => el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    private static List<double[]> GetCorners(JsonElement root)
    {
        var list = new List<double[]>();
        if (root.TryGetProperty("corners", out var cs) && cs.ValueKind == JsonValueKind.Array)
        {
            foreach (var p in cs.EnumerateArray())
            {
                if (p.ValueKind == JsonValueKind.Array && p.GetArrayLength() >= 2)
                {
                    var a = p.EnumerateArray().ToArray();
                    list.Add(new[] { a[0].GetDouble(), a[1].GetDouble() });
                }
            }
        }
        return list;
    }
}
