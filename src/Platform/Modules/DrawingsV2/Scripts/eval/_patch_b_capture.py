# -*- coding: utf-8 -*-
"""B 项 capture：新增 v2 内「录入 QR 预期解码内容」端点。
仅改 v2 模块：V2Store.cs / V2Service.cs / DrawingsV2Module.cs（Python 落盘 + 回读验证）。
"""
import io

BASE = r"E:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2"
STORE = BASE + r"\Decision\V2Store.cs"
SVC = BASE + r"\Runtime\V2Service.cs"
MOD = BASE + r"\DrawingsV2Module.cs"

def patch(path, old, new):
    with io.open(path, "r", encoding="utf-8") as f:
        s = f.read()
    cnt = s.count(old)
    if cnt != 1:
        raise SystemExit("FAIL %s count=%d" % (path, cnt))
    s = s.replace(old, new, 1)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(s)
    print("OK  ", path)

# ---- V2Store：新增 SetMarkExpectedText ----
old_store = "    // ---------------- 人工复核闸门（G1） ----------------"
new_store = """    /// <summary>
    /// 【B 项·capture】为 QR mark 录入/清除预期解码内容（m.Text）。
    /// 仅对 mark_type='Qr' 生效，其余类型忽略（返回 false）。清空传 null/空串。
    /// </summary>
    public bool SetMarkExpectedText(long profileId, string markKey, string? text)
    {
        using var c = Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = @"
UPDATE v2_drawing_marks SET text=$t
WHERE profile_id=$pid AND mark_key=$mk AND mark_type='Qr'";
        cmd.Parameters.AddWithValue("$t", (object?)text ?? DBNull.Value);
        cmd.Parameters.AddWithValue("$pid", profileId);
        cmd.Parameters.AddWithValue("$mk", markKey);
        return cmd.ExecuteNonQuery() > 0;
    }

    // ---------------- 人工复核闸门（G1） ----------------"""

# ---- V2Service：包装 ----
old_svc = """    /// <summary>② 保存判定级人工确认（实物有标 / 实物缺标 / 拍错部位）。</summary>
    public int SaveVerdictReviews(long recordId,
        IEnumerable<(string MarkKey, string HumanState, string? Note)> items, string? by)
        => _store.SaveVerdictReviews(recordId, items, by);"""
new_svc = """    /// <summary>② 保存判定级人工确认（实物有标 / 实物缺标 / 拍错部位）。</summary>
    public int SaveVerdictReviews(long recordId,
        IEnumerable<(string MarkKey, string HumanState, string? Note)> items, string? by)
        => _store.SaveVerdictReviews(recordId, items, by);

    /// <summary>【B 项·capture】为 QR mark 录入/清除预期解码内容（供 VerifyQr 做内容身份比对）。</summary>
    public bool SetMarkExpectedText(long profileId, string markKey, string? text)
        => _store.SetMarkExpectedText(profileId, markKey, text);"""

# ---- DrawingsV2Module：端点 ----
old_mod = """                return Results.Ok(new { ok = true, profileId = id, status = "reviewed" });
            }));

        // G2 会话合并：取一次多部位检验会话的聚合结果"""
new_mod = """                return Results.Ok(new { ok = true, profileId = id, status = "reviewed" });
            }));

        // 【B 项·capture】为 QR mark 录入/清除预期解码内容（供 VerifyQr 内容身份比对）
        g.MapPost("/profiles/{id:long}/marks/{markId}/expected-text",
            async (long id, string markId, JsonElement body, V2Service s) =>
            await Safe(async () =>
            {
                var text = ReadString(body, "text") ?? ReadString(body, "expectedText");
                var ok = s.SetMarkExpectedText(id, markId, text);
                return ok
                    ? Results.Ok(new { ok = true, profileId = id, markId, text })
                    : Results.NotFound(new { error = $"未找到 QR mark：{id}/{markId}", code = "not_found" });
            }));

        // G2 会话合并：取一次多部位检验会话的聚合结果"""

patch(STORE, old_store, new_store)
patch(SVC, old_svc, new_svc)
patch(MOD, old_mod, new_mod)
print("DONE")
