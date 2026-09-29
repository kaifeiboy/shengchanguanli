import io

# ---------- 1) V2Store.cs : 新增 AddMark / UpdateMark / DeleteMark ----------
p_store = r"e:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2\Decision\V2Store.cs"
s = open(p_store, encoding="utf-8").read()
anchor_store = "    // ---------------- 人工复核闸门（G1） ----------------"
assert anchor_store in s, "STORE_ANCHOR_NOT_FOUND"
assert s.count(anchor_store) == 1
store_block = '''    // ---------------- 人工复核·增删改 mark（设计§3） ----------------
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

''' + anchor_store
s = s.replace(anchor_store, store_block, 1)
open(p_store, "w", encoding="utf-8").write(s)
print("V2Store.cs PATCHED_OK")

# ---------- 2) V2Service.cs : 包装方法 ----------
p_svc = r"e:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2\Runtime\V2Service.cs"
sv = open(p_svc, encoding="utf-8").read()
anchor_svc = "        => _store.SetMarkExpectedText(profileId, markId, text);"
assert anchor_svc in sv, "SVC_ANCHOR_NOT_FOUND"
assert sv.count(anchor_svc) == 1
svc_block = anchor_svc + '''

    public long AddMark(long profileId, string markType, string view, string? text, bool required, string? conditionText, double? nx, double? ny, double? nw, double? nh)
        => _store.AddMark(profileId, markType, view, text, required, conditionText, nx, ny, nw, nh);
    public bool UpdateMark(long profileId, long markId, string markType, string view, string? text, bool required, string? conditionText, double? nx, double? ny, double? nw, double? nh)
        => _store.UpdateMark(profileId, markId, markType, view, text, required, conditionText, nx, ny, nw, nh);
    public bool DeleteMark(long profileId, long markId)
        => _store.DeleteMark(profileId, markId);

'''
sv = sv.replace(anchor_svc, svc_block, 1)
open(p_svc, "w", encoding="utf-8").write(sv)
print("V2Service.cs PATCHED_OK")

# ---------- 3) DrawingsV2Module.cs : 端点 + ParseNorm ----------
p_mod = r"e:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2\DrawingsV2Module.cs"
m = open(p_mod, encoding="utf-8").read()
anchor_mod_ep = "        // 【B 项·capture】为 QR mark 录入/清除预期解码内容（供 VerifyQr 内容身份比对）"
assert anchor_mod_ep in m, "MOD_EP_ANCHOR_NOT_FOUND"
assert m.count(anchor_mod_ep) == 1
ep_block = '''        // 【P1b 设计§3】人工复核·增删改 mark
        g.MapPost("/profiles/{id:long}/marks", async (long id, JsonElement body, V2Service s) =>
            await Safe(async () =>
            {
                var type = ReadString(body, "type") ?? ReadString(body, "markType");
                if (string.IsNullOrWhiteSpace(type) || type is not ("Text" or "Icon" or "Qr" or "Group"))
                    return Results.BadRequest(new { error = "type 必须属于 Text/Icon/Qr/Group", code = "bad_request" });
                var view = ReadString(body, "view") ?? "Unspecified";
                var text = ReadString(body, "text");
                var required = !body.TryGetProperty("required", out var rq) || rq.ValueKind != JsonValueKind.False;
                var condition = ReadString(body, "condition") ?? ReadString(body, "conditionText");
                var (nx, ny, nw, nh) = ParseNorm(body);
                if (nx is null || ny is null || nw is null || nh is null)
                    return Results.BadRequest(new { error = "缺少有效的 norm 坐标 (x,y,w,h)", code = "bad_request" });
                var newId = s.AddMark(id, type, view, text, required, condition, nx, ny, nw, nh);
                return Results.Ok(new { ok = true, profileId = id, markId = newId });
            }));
        g.MapPut("/profiles/{id:long}/marks/{markId:long}", async (long id, long markId, JsonElement body, V2Service s) =>
            await Safe(async () =>
            {
                var type = ReadString(body, "type") ?? ReadString(body, "markType");
                if (string.IsNullOrWhiteSpace(type) || type is not ("Text" or "Icon" or "Qr" or "Group"))
                    return Results.BadRequest(new { error = "type 必须属于 Text/Icon/Qr/Group", code = "bad_request" });
                var view = ReadString(body, "view") ?? "Unspecified";
                var text = ReadString(body, "text");
                var required = !body.TryGetProperty("required", out var rq) || rq.ValueKind != JsonValueKind.False;
                var condition = ReadString(body, "condition") ?? ReadString(body, "conditionText");
                var (nx, ny, nw, nh) = ParseNorm(body);
                if (nx is null || ny is null || nw is null || nh is null)
                    return Results.BadRequest(new { error = "缺少有效的 norm 坐标 (x,y,w,h)", code = "bad_request" });
                var ok = s.UpdateMark(id, markId, type, view, text, required, condition, nx, ny, nw, nh);
                return ok ? Results.Ok(new { ok = true, markId })
                         : Results.NotFound(new { error = $"未找到 mark：{id}/{markId}", code = "not_found" });
            }));
        g.MapDelete("/profiles/{id:long}/marks/{markId:long}", async (long id, long markId, V2Service s) =>
            await Safe(async () =>
            {
                var ok = s.DeleteMark(id, markId);
                return ok ? Results.Ok(new { ok = true, markId })
                         : Results.NotFound(new { error = $"未找到 mark：{id}/{markId}", code = "not_found" });
            }));

''' + anchor_mod_ep
m = m.replace(anchor_mod_ep, ep_block, 1)
anchor_mod_helper = "    private static async Task<IResult> Safe(Func<Task<IResult>> act)"
assert anchor_mod_helper in m, "MOD_HELPER_ANCHOR_NOT_FOUND"
assert m.count(anchor_mod_helper) == 1
helper_block = '''    private static (double?, double?, double?, double?) ParseNorm(JsonElement body)
    {
        double? G(string n) => body.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;
        double? x = G("normX") ?? G("x");
        double? y = G("normY") ?? G("y");
        double? w = G("normW") ?? G("w");
        double? h = G("normH") ?? G("h");
        if (x is null && body.TryGetProperty("norm", out var nm) && nm.ValueKind == JsonValueKind.Object)
        {
            x = nm.TryGetProperty("x", out var a) && a.ValueKind == JsonValueKind.Number ? a.GetDouble() : null;
            y = nm.TryGetProperty("y", out var b) && b.ValueKind == JsonValueKind.Number ? b.GetDouble() : null;
            w = nm.TryGetProperty("w", out var c) && c.ValueKind == JsonValueKind.Number ? c.GetDouble() : null;
            h = nm.TryGetProperty("h", out var d) && d.ValueKind == JsonValueKind.Number ? d.GetDouble() : null;
        }
        return (x, y, w, h);
    }

''' + anchor_mod_helper
m = m.replace(anchor_mod_helper, helper_block, 1)
open(p_mod, "w", encoding="utf-8").write(m)
print("DrawingsV2Module.cs PATCHED_OK")
