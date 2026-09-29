import io, sys

BASE = r"E:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2"

def patch(path, old, new, label):
    with io.open(path, "r", encoding="utf-8") as f:
        s = f.read()
    if old not in s:
        print("SKIP (not found): %s in %s" % (label, path)); return
    s = s.replace(old, new, 1)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(s)
    print("PATCHED: %s in %s" % (label, path))

# 1) V2Store.SetMarkExpectedText: string markKey -> long markId, match id column
store = BASE + r"\Decision\V2Store.cs"
patch(store,
      "public bool SetMarkExpectedText(long profileId, string markKey, string? text)\n    {\n        using var c = Open();\n        using var cmd = c.CreateCommand();\n        cmd.CommandText = @\"\nUPDATE v2_drawing_marks SET text=$t\nWHERE profile_id=$pid AND mark_key=$mk AND mark_type='Qr'\";\n        cmd.Parameters.AddWithValue(\"$t\", (object?)text ?? DBNull.Value);\n        cmd.Parameters.AddWithValue(\"$pid\", profileId);\n        cmd.Parameters.AddWithValue(\"$mk\", markKey);\n        return cmd.ExecuteNonQuery() > 0;",
      "public bool SetMarkExpectedText(long profileId, long markId, string? text)\n    {\n        using var c = Open();\n        using var cmd = c.CreateCommand();\n        cmd.CommandText = @\"\nUPDATE v2_drawing_marks SET text=$t\nWHERE profile_id=$pid AND id=$mid AND mark_type='Qr'\";\n        cmd.Parameters.AddWithValue(\"$t\", (object?)text ?? DBNull.Value);\n        cmd.Parameters.AddWithValue(\"$pid\", profileId);\n        cmd.Parameters.AddWithValue(\"$mid\", markId);\n        return cmd.ExecuteNonQuery() > 0;",
      "V2Store.SetMarkExpectedText")

# 2) V2Service wrapper
svc = BASE + r"\Runtime\V2Service.cs"
patch(svc,
      "    public bool SetMarkExpectedText(long profileId, string markKey, string? text)\n        => _store.SetMarkExpectedText(profileId, markKey, text);",
      "    public bool SetMarkExpectedText(long profileId, long markId, string? text)\n        => _store.SetMarkExpectedText(profileId, markId, text);",
      "V2Service.SetMarkExpectedText")

# 3) DrawingsV2Module endpoint: route + handler long markId
mod = BASE + r"\DrawingsV2Module.cs"
patch(mod,
      "        g.MapPost(\"/profiles/{id:long}/marks/{markId}/expected-text\",\n            async (long id, string markId, JsonElement body, V2Service s) =>\n            await Safe(async () =>\n            {\n                var text = ReadString(body, \"text\") ?? ReadString(body, \"expectedText\");\n                var ok = s.SetMarkExpectedText(id, markId, text);",
      "        g.MapPost(\"/profiles/{id:long}/marks/{markId:long}/expected-text\",\n            async (long id, long markId, JsonElement body, V2Service s) =>\n            await Safe(async () =>\n            {\n                var text = ReadString(body, \"text\") ?? ReadString(body, \"expectedText\");\n                var ok = s.SetMarkExpectedText(id, markId, text);",
      "DrawingsV2Module.endpoint")

print("DONE")
