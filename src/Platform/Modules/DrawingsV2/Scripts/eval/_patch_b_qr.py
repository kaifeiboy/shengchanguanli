# -*- coding: utf-8 -*-
"""B 项：QR 内容身份比对 —— 原地改写 C# 源码（Python 落盘 + 回读验证）。
仅改 v2 模块：MarkVerifier.cs (VerifyQr) + V2Models.cs (注释)。
"""
import io, sys

BASE = r"E:\workaaa\shengchanguanli\src\Platform\Modules\DrawingsV2"

def patch(path, replacements):
    with io.open(path, "r", encoding="utf-8") as f:
        s = f.read()
    for i, (old, new) in enumerate(replacements):
        cnt = s.count(old)
        if cnt != 1:
            raise SystemExit("FAIL %s repl#%d count=%d" % (path, i, cnt))
        s = s.replace(old, new, 1)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(s)
    print("OK  ", path, "replacements=", len(replacements))

MV = BASE + r"\Decision\MarkVerifier.cs"
VM = BASE + r"\Decision\V2Models.cs"

# ---- MarkVerifier.cs : 定点解码块（加内容身份校验）----
old1 = """        // 1) 定点解码
        if (obs.QrFound)
        {
            v.ObservedData = obs.QrData;
            v.Offset = Offset(ExpectedBbox(m, result), obs.QrNormBbox);
            if (v.Offset is null || v.Offset <= PosTolOf(result) * 2) // 码中心允许更大漂移（裁剪+透视）
            {
                v.State = nameof(MarkState.Matched);
                v.Evidence = $"定点解码成功「{obs.QrData}」（scale={obs.QrScale}）";
            }
            else
            {
                v.State = nameof(MarkState.LowConfidence);
                v.Evidence = $"定点解码「{obs.QrData}」但中心偏移 {v.Offset:F3}";
            }
            return;
        }"""

new1 = """        // 1) 定点解码
        if (obs.QrFound)
        {
            v.ObservedData = obs.QrData;
            v.Offset = Offset(ExpectedBbox(m, result), obs.QrNormBbox);
            var decoded = (obs.QrData ?? "").Trim();
            var expected = (m.Text ?? "").Trim();
            var posOk = v.Offset is null || v.Offset <= PosTolOf(result) * 2; // 码中心允许更大漂移（裁剪+透视）
            if (expected.Length > 0)
            {
                // 【B 项·QR 内容身份校验】预期码内容已知时，比对解码内容以根除「框罩错码」歧义
                var contentOk = decoded.Length > 0
                    && string.Equals(decoded, expected, StringComparison.OrdinalIgnoreCase);
                if (contentOk && posOk)
                {
                    v.State = nameof(MarkState.Matched);
                    v.Evidence = $\"定点解码「{decoded}」与预期一致（scale={obs.QrScale}）\";
                }
                else if (contentOk) // 内容对、位置偏
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.Evidence = $\"定点解码「{decoded}」与预期一致，但中心偏移 {v.Offset:F3}（需人工复核）\";
                }
                else // 解出但内容≠预期：宁黄勿红，交人工复核（可能是相邻码被框入或真错标）
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.Evidence = $\"定点解码「{decoded}」≠预期「{expected}」（内容不符，需人工复核）\";
                }
            }
            else
            {
                // 无预期内容：沿用原纯位置判定（行为不变）
                if (posOk)
                {
                    v.State = nameof(MarkState.Matched);
                    v.Evidence = $\"定点解码成功「{decoded}」（scale={obs.QrScale}）\";
                }
                else
                {
                    v.State = nameof(MarkState.LowConfidence);
                    v.Evidence = $\"定点解码「{decoded}」但中心偏移 {v.Offset:F3}\";
                }
            }
            return;
        }"""

# ---- MarkVerifier.cs : 盲检码落位块（加内容身份校验）----
old2 = """            if (near is not null)
            {
                v.ObservedData = near.Data;
                v.State = nameof(MarkState.Matched);
                v.PhotoBbox = near.NormBbox;   // 盲检码的实际位置（照片标注用）
                v.Evidence = $\"盲检码「{near.Data}」落于预期位置（距离 {nearDist:F3}）\";
                return;
            }"""

new2 = """            if (near is not null)
            {
                v.ObservedData = near.Data;
                var decoded = (near.Data ?? "").Trim();
                var expected = (m.Text ?? "").Trim();
                v.PhotoBbox = near.NormBbox;   // 盲检码的实际位置（照片标注用）
                if (expected.Length > 0 && decoded.Length > 0
                    && !string.Equals(decoded, expected, StringComparison.OrdinalIgnoreCase))
                {
                    // 【B 项·QR 内容身份校验】码落在预期位置但内容不符 → 黄交人工复核
                    v.State = nameof(MarkState.LowConfidence);
                    v.Evidence = $\"盲检码「{decoded}」落于预期位置但与预期「{expected}」不符（距离 {nearDist:F3}，需人工复核）\";
                }
                else
                {
                    v.State = nameof(MarkState.Matched);
                    v.Evidence = $\"盲检码「{decoded}」落于预期位置（距离 {nearDist:F3}）\";
                }
                return;
            }"""

# ---- V2Models.cs : 注释更新（说明 QR 可录入预期内容）----
old3 = "    Qr,     // 二维码：只验存在性与位置，不解码内容"
new3 = "    Qr,     // 二维码：验存在性/位置；若录入预期内容则比对解码内容（B 项）"

old4 = "    /// <summary>标称文本。Qr/Icon 为 null（方案：二维码与图标只验存在性与位置）。</summary>"
new4 = "    /// <summary>标称文本。Text/Icon 一般为 null（只验存在性与位置）；QR 可录入预期解码内容以做身份比对（B 项）。</summary>"

patch(MV, [(old1, new1), (old2, new2)])
patch(VM, [(old3, new3), (old4, new4)])
print("DONE")
