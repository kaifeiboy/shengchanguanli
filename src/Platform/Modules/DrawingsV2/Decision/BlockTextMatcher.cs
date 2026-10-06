using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace Platform.Modules.DrawingsV2.Decision;

/// <summary>
/// P3 · 文本命中选块 + 四色判定（docs/逻辑图块方案_2026-09-26.md §0.5.1 / §0.5.3 / §0.5.3.1）。
///
/// <para>【范式】判定 = 照片 OCR 文本去「图块内的打标内容」里查是否存在：
/// 命中 → 🟢绿；文本差异（块有照片无 / 照片多出 / 相似或包含但非全等）→ 🔴红；图标·QR 任一方缺 → 🟡黄；d 类不参与 → ⚪灰。
/// **★ 2026-09-27 用户最终口径：黄色只给图标/QR，文本差异一律红（不再有"文本黄"）。**
/// **判定不依赖坐标**（坐标只服务 P4 的红框落点，见 §0.5.5）。</para>
///
/// <para>【输入隔离（防干扰的两道闸门，方案 §2.1 / §2.3）】
/// ① 只检索 <c>has_marking=1</c> 的块 —— 空块不进候选，天然不可能产生假绿/假红；
/// ② 只比对 <c>participate=1</c> 的元素 —— d 类（尺寸/标注/说明）与 c 类（图标/QR）不参与文本命中。</para>
///
/// <para>【零回归】本类结果通过 compare 响应的 <c>blockMatch</c> 附加字段下传（P3-b 已并入），
/// 既有 <c>verdicts</c> 口径一字不改；两套判定并列共存、互不影响。</para>
///
/// <para>【文本不黄·只绿或红】命中（全等 / 照片整行包含 / 低置信命中）一律 🟢绿；
/// 文本差异（相似编辑距离 1、包含但非全等、块有照片无、照片多出）一律 🔴红。
/// 黄色仅保留给图标/QR 的"任一方缺"（见 <see cref="JudgeCodeExistence"/>）。</para>
///
/// <para>★ 2026-09-27 实证修正一（照片整行粘连）：照片 OCR 常把整条铭牌读成一行
/// 「PC-P1HVQ服务热线400-860-1111 NNFC」，而图纸块内是逐条元素「服务热线400-860-1111」。
/// 二者既不全等、长度比也 &lt;0.6 → 按原 §0.5.3.1 判据会全部漏命（假红）。
/// 故新增判据 2：**照片串包含块内元素（needle ≥4）即命中**，与既有
/// <see cref="TextNormalizer.Contains"/>「打标项在实例文本中定位」的语义一致。</para>
///
/// <para>★ 2026-09-27 实证修正二（召回不足仍判红）：照片只 OCR 到 1~2 条文本时，
/// 块内未命中的元素按用户最终口径仍判 🔴红（文本差异一律红），但记 <c>degraded</c> 供诊断；
/// 不再降黄（黄色仅图标/QR）。</para>
/// </summary>
public static class BlockTextMatcher
{
    // —— 阈值（方案 §0.5.3.1 用户拍板 + 2026-09-27 实证修正）——
    private const int FuzzyMinLen = 6;        // 长度 ≥6 才允许 ≤1 处差异
    private const int FuzzyMaxDist = 1;       // 允许的编辑距离
    private const double ContainRatio = 0.6;  // 包含关系：短/长 ≥0.6 才算相似命中
    private const int MinTextLen = 2;         // 过短噪声串不参与检索（与 Python _is_valid_marking 同口径）
    private const int MinContainLen = 4;      // 实证修正一：包含判据的最短 needle（低于此必须全等，防短串假命中）
    private const double LowConfOcr = 0.60;   // 元素 OCR 置信低于此 → 命中仍判绿（仅标注低置信，文本命中不降黄；黄仅图标/QR）
    private const int MinPhotoTextsForRed = 3;// 召回不足诊断阈值：照片有效文本少于此记 degraded（文本差异仍判红）

    // —— 四色（与既有 MarkState 语义对齐的展示口径）——
    public const string Green = "green";     // 一致
    public const string Red = "red";         // 缺标
    public const string Yellow = "yellow";   // 仅图标/QR：任一方缺（文本永不黄）
    public const string Gray = "gray";       // 跳过（图标/QR/d 类/空文本）

    // ================= 照片侧输入 =================

    public sealed class PhotoText
    {
        public string Text { get; set; } = "";
        public string Key { get; set; } = "";
        public double? Conf { get; set; }
        public double[]? Norm { get; set; }
    }

    /// <summary>照片侧检出的码/图标（observe/1 的 codes[]：type / data / bbox / conf）。
    /// 用于 §0.5.3.2 规则 3 的**存在性核对**：只比「有没有」，绝不读 content。</summary>
    public sealed class PhotoCode
    {
        public string Type { get; set; } = "";
        public string? Data { get; set; }
        public double[]? Norm { get; set; }
    }

    /// <summary>取照片侧 codes[]（zxingcpp → cv2-QR → 分块扫描 三级召回的结果）。</summary>
    public static List<PhotoCode> ExtractPhotoCodes(JsonElement? observeRoot)
    {
        var list = new List<PhotoCode>();
        if (observeRoot is not { ValueKind: JsonValueKind.Object } obs) return list;
        if (!obs.TryGetProperty("codes", out var arr) || arr.ValueKind != JsonValueKind.Array) return list;

        foreach (var c in arr.EnumerateArray())
        {
            if (c.ValueKind != JsonValueKind.Object) continue;
            string type = "";
            if (c.TryGetProperty("type", out var tp) && tp.ValueKind == JsonValueKind.String)
                type = tp.GetString() ?? "";
            string? data = null;
            if (c.TryGetProperty("data", out var dd) && dd.ValueKind == JsonValueKind.String)
                data = dd.GetString();
            double[]? norm = null;
            if (c.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array
                && nb.GetArrayLength() >= 4)
            {
                norm = new double[4];
                for (int i = 0; i < 4; i++)
                    norm[i] = nb[i].ValueKind == JsonValueKind.Number ? nb[i].GetDouble() : 0.0;
            }
            list.Add(new PhotoCode { Type = type, Data = data, Norm = norm });
        }
        return list;
    }

    public sealed class PhotoQuality
    {
        public bool Usable { get; set; } = true;
        public List<string> Reasons { get; set; } = new();
        public int OcrTexts { get; set; }
        public double Sharpness { get; set; }
    }

    /// <summary>从 observe/1 文档取照片 OCR 文本（texts[]：text / conf / norm_bbox）。</summary>
    public static List<PhotoText> ExtractPhotoTexts(JsonElement? observeRoot)
    {
        var list = new List<PhotoText>();
        if (observeRoot is not { ValueKind: JsonValueKind.Object } obs) return list;
        if (!obs.TryGetProperty("texts", out var arr) || arr.ValueKind != JsonValueKind.Array) return list;

        foreach (var t in arr.EnumerateArray())
        {
            if (t.ValueKind != JsonValueKind.Object) continue;
            if (!t.TryGetProperty("text", out var tx) || tx.ValueKind != JsonValueKind.String) continue;
            var text = tx.GetString() ?? "";
            if (text.Trim().Length == 0) continue;

            double? conf = null;
            if (t.TryGetProperty("conf", out var cf) && cf.ValueKind == JsonValueKind.Number)
                conf = cf.GetDouble();

            double[]? norm = null;
            if (t.TryGetProperty("norm_bbox", out var nb) && nb.ValueKind == JsonValueKind.Array
                && nb.GetArrayLength() >= 4)
            {
                norm = new double[4];
                for (int i = 0; i < 4; i++)
                    norm[i] = nb[i].ValueKind == JsonValueKind.Number ? nb[i].GetDouble() : 0.0;
            }

            list.Add(new PhotoText { Text = text, Key = TextNormalizer.LooseKey(text), Conf = conf, Norm = norm });
        }
        return list;
    }

    /// <summary>从 observe/1 文档取照片质量（quality：usable / reasons / ocr_texts / sharpness）。</summary>
    public static PhotoQuality ExtractQuality(JsonElement? observeRoot)
    {
        var q = new PhotoQuality();
        if (observeRoot is not { ValueKind: JsonValueKind.Object } obs) return q;
        if (!obs.TryGetProperty("quality", out var qj) || qj.ValueKind != JsonValueKind.Object) return q;

        if (qj.TryGetProperty("usable", out var u))
            q.Usable = u.ValueKind != JsonValueKind.False;
        if (qj.TryGetProperty("ocr_texts", out var ot) && ot.ValueKind == JsonValueKind.Number)
            q.OcrTexts = ot.GetInt32();
        if (qj.TryGetProperty("sharpness", out var sh) && sh.ValueKind == JsonValueKind.Number)
            q.Sharpness = sh.GetDouble();
        if (qj.TryGetProperty("reasons", out var rs) && rs.ValueKind == JsonValueKind.Array)
            foreach (var r in rs.EnumerateArray())
                if (r.ValueKind == JsonValueKind.String) q.Reasons.Add(r.GetString() ?? "");
        return q;
    }

    // ================= 输出 =================

    public sealed class ElementVerdict
    {
        public string Kind { get; set; } = "";
        public string? Text { get; set; }
        public string Color { get; set; } = Gray;
        public string Reason { get; set; } = "";
        public string? PhotoText { get; set; }     // 命中的照片文本（红/灰/未检出为 null）
        /// <summary>true = 照片侧确实找到了对应文本（命中即绿）；false = 未检出 / 红 / 灰。
        /// ★ 仅用于区分「真正命中(绿)」与「未命中」——绿才计入块得分(Score=NGreen)，
        ///   否则零命中块会靠其它颜色数当选 Top1（文本差异一律红、黄仅图标/QR，故不存在"命中类黄"）。</summary>
        public bool Hit { get; set; }
        public double[]? Norm { get; set; }        // 块内元素归一化框（图纸侧，P4 映射输入）
        public double? OcrConf { get; set; }
        // —— P4 照片侧落点 ——
        public double[]? PhotoNorm { get; set; }   // 照片侧归一化框（[x,y,w,h]）；null = 无照片坐标（L3 不画框）
        public string MappingLevel { get; set; } = "";  // L2=照片侧直接检出框；L3=无照片坐标（块有照片无/码缺失）
    }

    public sealed class BlockVerdict
    {
        public long Id { get; set; }
        public int BlockIndex { get; set; }
        public string BlockKey { get; set; } = "";
        public string Name { get; set; } = "";
        public string ViewHint { get; set; } = "Unspecified";
        /// <summary>命中块的图纸页级归一化框 [x,y,w,h]（型号驱动：H5 图纸画布高亮命中块整体区域；逐元素四色明细在结论卡片）。取自 <c>LogicalBlockView.Norm</c>。</summary>
        public double[]? BlockNorm { get; set; }
        /// <summary>命中块所在图纸页（供图纸预览定位）。</summary>
        public int PageIndex { get; set; }
        public double Score { get; set; }
        public int NGreen { get; set; }
        public int NRed { get; set; }
        public int NYellow { get; set; }
        public int NGray { get; set; }
        public int NHit { get; set; }              // 真正命中数（绿），块排序与 Top 选择的唯一依据
        public int NCodeOk { get; set; }           // c 类：图纸与照片双方都具备 → 绿
        public int NCodeMissing { get; set; }      // c 类：任一方不具备 → 黄
        public List<ElementVerdict> Elements { get; set; } = new();
    }

    public sealed class MatchResult
    {
        public bool Enabled { get; set; }         // false = 未产出结论（无块 / 照片不可用 / 零命中），调用方走原有兜底
        public string Reason { get; set; } = "";
        public bool Degraded { get; set; }        // true = 照片侧召回不足（仅诊断用；文本差异仍判红）
        public string? DegradeReason { get; set; }
        public int NBlocks { get; set; }          // 该图纸块总数（含空块）
        public int NCandidates { get; set; }      // 参与检索的块数（has_marking=1）
        public BlockVerdict? Top { get; set; }    // 命中条目最多的块
        public List<BlockVerdict> Candidates { get; set; } = new();  // Top-3（H5 可切换）
        public List<ElementVerdict> Extra { get; set; } = new();     // 照片有、Top 块内没有 → 红（多标，文本差异）
        public List<string> PhotoTexts { get; set; } = new();        // 照片侧文本快照（审计用，截断 40 条）
        public int NPhotoCodes { get; set; }                         // 照片侧检出的码/图标条数（审计）
    }

    // ================= 主入口 =================

    /// <summary>
    /// 文本命中：照片 OCR 文本 ↔ 块内打标内容。
    /// </summary>
    /// <param name="blocks">该档案全部逻辑块（含空块，由本方法自行隔离）。</param>
    /// <param name="photo">照片 OCR 文本（<see cref="ExtractPhotoTexts"/>）。</param>
    /// <param name="quality">照片质量（<see cref="ExtractQuality"/>）；usable=false → 直接不产出结论。</param>
    public static MatchResult Match(IReadOnlyList<V2Store.LogicalBlockView> blocks,
                                    IReadOnlyList<PhotoText> photo, PhotoQuality? quality = null,
                                    IReadOnlyList<PhotoCode>? codes = null)
    {
        var codesList = codes ?? Array.Empty<PhotoCode>();
        var res = new MatchResult { NBlocks = blocks.Count, NPhotoCodes = codesList.Count };
        var photoUsable = photo.Where(p => p.Key.Length >= MinTextLen).ToList();
        res.PhotoTexts = photoUsable.Take(40).Select(p => p.Text).ToList();

        var cands = blocks.Where(b => b.HasMarking).ToList();
        res.NCandidates = cands.Count;
        if (cands.Count == 0)
        {
            res.Reason = "该图纸没有参与匹配的图块（需先跑 blocks 提取，或全部块均无打标内容）";
            return res;
        }

        // 质量闸门：照片不可用 → 不谎报缺标（与既有 NotComparable 口径一致）
        if (quality is { Usable: false })
        {
            res.Reason = "照片质量不可用：" + string.Join("; ", quality.Reasons) + " → 不作缺标判定，请重拍";
            return res;
        }

        // 召回闸门：照片侧证据不足 → 仅记 degraded 供诊断；文本差异仍判红（黄色仅图标/QR）
        // 【2026-09-30 准确度修复】OCR 对模糊字形常输出「口」占位（实例：记录 1000「昆日」→「口口」），
        //   条数够但内容残缺，缺标红可信度低 → 同样记 degraded，结论卡片提示重拍/人工复核。
        bool degraded = photoUsable.Count < MinPhotoTextsForRed;
        int placeholderCnt = photoUsable.Count(p => !string.IsNullOrEmpty(p.Text)
                                && p.Text.Length - p.Text.Replace("口", "").Length >= 2);
        if (!degraded && placeholderCnt > 0) degraded = true;
        res.Degraded = degraded;
        if (degraded)
            res.DegradeReason = placeholderCnt > 0
                ? $"照片 OCR 含 {placeholderCnt} 条「口」占位（模糊字形识别失败），内容可能残缺；文本差异仍判红，建议重拍或人工复核"
                : $"照片侧仅 OCR 到 {photoUsable.Count} 条有效文本（<{MinPhotoTextsForRed}）；文本差异仍判红（用户口径：文本一律绿或红）";

        if (photoUsable.Count == 0)
        {
            res.Reason = "照片侧未 OCR 到有效文本，无法做文本命中";
            return res;
        }

        foreach (var b in cands)
        {
            var bv = new BlockVerdict
            {
                Id = b.Id,
                BlockIndex = b.BlockIndex,
                BlockKey = b.BlockKey,
                Name = b.Name,
                ViewHint = b.ViewHint,
                PageIndex = b.PageIndex,
                BlockNorm = ToDouble(b.Norm)   // 命中块图纸页级框：H5 图纸画布高亮整体区域
            };

            foreach (var el in b.Elements)
            {
                var ev = JudgeElement(el, photoUsable, degraded, b.LowConf, codesList);
                bv.Elements.Add(ev);
                bool isCode = ev.Kind is "qr" or "icon";
                if (isCode)
                {
                    // c 类单独统计，且**不进 NHit / Score**：
                    // 粗判下只要照片有任意码就算「具备」，若计分会让含 QR 的块莫名胜过真正命中文本的块。
                    if (ev.Color == Green) bv.NCodeOk++; else bv.NCodeMissing++;
                }
                else
                {
                    switch (ev.Color)
                    {
                        case Green: bv.NGreen++; break;
                        case Red: bv.NRed++; break;
                        case Yellow: bv.NYellow++; break;
                        default: bv.NGray++; break;
                    }
                    if (ev.Color == Green) bv.NHit++;   // 命中=绿才计入块得分（文本差异红、icon/QR黄均不计）
                }
            }

            // ★ 块内 OCR 重复变体中和（2026-09-29 真实照片复验驱动）：同一物理条码被 OCR 双检出为
            //   不同读法（如 YCWA15NCWQ ↔ YCWA15NCWO，单字符误读），使「同一处文本」既绿又红、
            //   虚增「缺标」红。凡红元素 LooseKey 与块内某绿元素 LooseKey 编辑距离≤1，判定为同一处
            //   文本的 OCR 变体 → 该红降级为灰、不计入 NRed。仅作用于单字符差异，避免误杀真实数字
            //   差异（如两段不同电话号码）。提取期无照片上下文，故放到选块阶段用命中信息中和。
            var greenKeys = bv.Elements
                .Where(e => e.Color == Green && !string.IsNullOrEmpty(e.Text))
                .Select(e => TextNormalizer.LooseKey(e.Text))
                .ToHashSet();
            foreach (var ev in bv.Elements)
            {
                if (ev.Color != Red || string.IsNullOrEmpty(ev.Text)) continue;
                var rk = TextNormalizer.LooseKey(ev.Text);
                if (greenKeys.Any(gk => Levenshtein(rk, gk) <= 1))
                {
                    ev.Color = Gray;
                    ev.Reason = "同块内与命中文本为 OCR 单字符变体（如 Q↔O），判为同一处文本，不计缺标";
                    bv.NRed--;
                    bv.NGray++;
                }
            }

            // ★ 得分只认「真正命中」：绿 1.0（文本差异已归红，不再有"命中类黄"计入得分）。
            // 图标/QR 的绿/黄不计入 Score（见上方 isCode 分支）。
            bv.Score = bv.NGreen;
            res.Candidates.Add(bv);
        }

        // ★ 选块改「命中率优先」（2026-09-30 用户拍板，修复 66 YCWA15NCBQ 铭牌照片选中
        //   25 元素整机块而非干净铭牌条的问题）：主排序仍看命中数（保召回，端子区照片
        //   命中整机块的正确场景不受影响），同命中数时以绿命中率（绿/参与文本数）排序——
        //   大块靠"未命中元素多"不再占优；再按红少、最后黄少。Score 字段保留 =NGreen 供对外展示。
        res.Candidates = res.Candidates
            .OrderByDescending(c => c.NGreen)
            .ThenByDescending(c => c.NGreen / (double)Math.Max(1, c.NGreen + c.NRed + c.NYellow))
            .ThenBy(c => c.NRed)          // 命中率同档时，缺标条数更少者优先
            .ThenBy(c => c.NYellow)
            .ToList();

        var top = res.Candidates.FirstOrDefault();
        if (top is null || top.Score <= 0)
        {
            res.Reason = "照片文本未命中任何图块（保持既有整图兜底，不引入新失败模式）";
            return res;
        }

        res.Top = top;
        res.Candidates = res.Candidates.Take(3).ToList();
        res.Enabled = true;

        // 【P4+ 坐标映射加强（2026-09-30）】未命中元素（缺标红）此前 PhotoNorm=null → L3 不画框，
        //   现场看不出「缺在照片哪个位置」。用已命中元素的「块内相对位置 ↔ 照片框」锚点对拟合
        //   仿射（≥3 对）或并集粗配（2 对），把缺标元素映射到照片坐标 → 可画框落点。
        MapMissingElements(top);

        // 【2026-09-30 修复·照片多出不再一律判红】
        //   现场反馈「开/关」「AB」等真实存在的打标被标红：根因是图纸侧提取漏了这些内容，
        //   照片 OCR 读到后落进 extra，被旧规则一律判红，与「缺标/文本差异」混淆不清。
        //   现按三类处理：
        //   ① 与块内某参与元素互为 OCR 变体（编辑距离≤1 / 包含关系）→ 同一处文本，不再重复标；
        //   ② 疑似变量数据（机身编号/序列号：无汉字、长度≥5、数字占比≥50%）→ 灰、不画框；
        //   ③ 其余真实文本（图纸块内未列出）→ 黄「多标」；红只保留给「块内有而照片缺失/不一致」。
        var consumed = new HashSet<string>(StringComparer.Ordinal);
        foreach (var ev in top.Elements)
            if (ev.PhotoText is { Length: > 0 } pt) consumed.Add(TextNormalizer.LooseKey(pt));

        // 【2026-09-30 准确度修复·单字母豁免】端子/按键的单字母标识（A、B…）因 MinTextLen=2
        //   在元素侧被判灰跳过；照片 OCR 常把它们读成连串（如 AB）。若变体比对只看非灰元素，
        //   连串会漏配而误标「照片多出」（实例：记录 1004，块内 A|B vs 照片 AB）。
        //   故灰元素中的**单字母**仍参与 extra 变体比对（仅用于消重，不改变元素判定）。
        var blockKeys = top.Elements
            .Where(e => !string.IsNullOrEmpty(e.Text) && (e.Color != Gray || IsSingleLetter(e.Text)))
            .Select(e => TextNormalizer.LooseKey(e.Text!))
            .Where(k => k.Length > 0)
            .ToList();

        foreach (var p in photoUsable)
        {
            if (consumed.Contains(p.Key)) continue;
            // ① OCR 变体：与块内元素编辑距离≤1 或互相包含 → 已是同一处文本
            bool variant = blockKeys.Any(bk =>
                Levenshtein(p.Key, bk) <= 1
                || (p.Key.Length >= MinContainLen && (bk.Contains(p.Key, StringComparison.Ordinal)
                                                      || p.Key.Contains(bk, StringComparison.Ordinal))));
            if (variant) continue;
            // ② 变量数据：机身编号/序列号 → 灰，不画框（每台机器不同，不参与打标比对）
            if (IsSerialLike(p.Text))
            {
                res.Extra.Add(new ElementVerdict
                {
                    Kind = "photo_extra",
                    Text = p.Text,
                    Color = Gray,
                    Reason = "疑似变量数据/机身编号（每台不同），不参与打标比对",
                    PhotoText = p.Text,
                    PhotoNorm = null,
                    MappingLevel = "L3",
                    OcrConf = p.Conf
                });
                continue;
            }
            // ③ 其余 → 黄（多标：照片上有、图纸块内未列出）
            res.Extra.Add(new ElementVerdict
            {
                Kind = "photo_extra",
                Text = p.Text,
                Color = Yellow,
                Reason = "照片多出：Top 块内没有此内容（多标→黄；红仅表示块内有而照片缺失/不一致）",
                PhotoText = p.Text,
                PhotoNorm = p.Norm,
                MappingLevel = "L2",
                OcrConf = p.Conf
            });
        }

        // 照片侧有码/图标，而 Top 块内没有任何 c 类元素 → 任一方不具备 → 黄（多标）
        if (codesList.Count > 0 && !top.Elements.Any(e => e.Kind is "qr" or "icon"))
        {
            foreach (var c in codesList.Take(3))
                res.Extra.Add(new ElementVerdict
                {
                    Kind = "photo_extra_code",
                    Text = null,
                    Color = Yellow,
                    Reason = "照片检出码/图标，但 Top 块内没有对应 c 类元素（任一方不具备）→ 黄",
                    Norm = null,
                    PhotoNorm = c.Norm,
                    MappingLevel = "L2"
                });
        }
        return res;
    }

    // ================= 单元素判定 =================

    /// <summary>
    /// §0.5.3.2 规则 3：QR / 图标**只核「双方是否都具备」**，不读内容、不比图案。
    /// 都具备 → 🟢 绿；任一方不具备 → 🟡 黄。
    /// <para>当前为**粗判**（P3-b）：仅看照片侧 codes 是否非空。P4 接入 L1 映射后升级为
    /// 「c 类元素 bbox 投影到照片后，照片码的 bbox 中心是否落在投影区内」。</para>
    /// </summary>
    private static ElementVerdict JudgeCodeExistence(ElementVerdict ev, V2Store.LogicalElementView el,
                                                     IReadOnlyList<PhotoCode> codes)
    {
        bool bothSides = codes.Count > 0;          // 图纸侧有（元素本身即在），照片侧看 codes
        if (bothSides)
        {
            ev.Color = Green;
            ev.Hit = true;
            ev.PhotoText = el.Kind == "qr" ? "二维码" : "图标";
            var codeNorm = codes.FirstOrDefault(c => c.Norm is { Length: 4 })?.Norm;
            ev.PhotoNorm = codeNorm;               // 照片检出码框（L2）
            ev.MappingLevel = codeNorm is null ? "L3" : "L2";
            ev.Reason = el.Kind == "qr"
                ? "二维码：图纸与照片两侧均具备（粗判，仅验存在性，不读内容）"
                : "图标：图纸与照片两侧均具备（粗判，仅验存在性，不比图案）";
        }
        else
        {
            ev.Color = Yellow;
            ev.Hit = false;
            ev.PhotoNorm = null;
            ev.MappingLevel = "L3";
            ev.Reason = el.Kind == "qr"
                ? "二维码：照片侧未检出任何码 → 任一方不具备 → 黄"
                : "图标：照片侧未检出对应图标 → 任一方不具备 → 黄";
        }
        return ev;
    }

    private static ElementVerdict JudgeElement(V2Store.LogicalElementView el,
                                               List<PhotoText> photo, bool degraded, bool blockLowConf,
                                               IReadOnlyList<PhotoCode> codes)
    {
        var ev = new ElementVerdict
        {
            Kind = el.Kind,
            Text = el.Text,
            Norm = ToDouble(el.Norm),
            OcrConf = el.OcrConf
        };

        // 闸门：不参与匹配的元素 → 灰，永不产生红/绿
        //   d 类 = dim(线性尺寸) / meta(图纸元信息：日期·版本·序号) / note(说明文字)  ← 2026-09-27 L1 拆分
        //   ⚠ c 类（图标/QR）按用户在 2026-09-27 重申的口径【不再走灰】，
        //     改判「双方是否都具备」→ 都具备绿 / 任一方缺黄（§0.5.3.2 规则 3）。
        //     本闸门仅拦截 d 类；c 类的存在性核对在 JudgeCodeExistence 中处理。
        bool isCode = el.Kind is "qr" or "icon";
        if (!el.Participate && !isCode)
        {
            ev.Color = Gray;
            ev.Reason = el.Kind switch
            {
                "qr" => "二维码：只验存在性与位置，不参与文本命中",
                "icon" => "图标：只验存在性与位置，不参与文本命中",
                "dim" => "线性尺寸标注：d 类不参与匹配（有量纲证据）",
                "meta" => "图纸元信息（日期/版本/标注序号）：d 类不参与匹配（2026-09-27 L1 新增）",
                "note" => "说明文字：d 类不参与匹配",
                _ => "不参与匹配"
            };
            return ev;
        }

        if (isCode) return JudgeCodeExistence(ev, el, codes);

        var key = TextNormalizer.LooseKey(el.Text);
        if (key.Length < MinTextLen)
        {
            ev.Color = Gray;
            ev.Reason = "内容过短/噪声串，不参与命中";
            return ev;
        }

        bool lowConf = blockLowConf || (el.OcrConf is { } c && c < LowConfOcr);

        // 1) 全等 → 绿（低置信仍判绿，仅标注低置信）
        var exact = photo.FirstOrDefault(p => string.Equals(p.Key, key, StringComparison.Ordinal));
        if (exact is not null) return Hit(ev, exact, lowConf, blockLowConf, el.OcrConf, "命中：文本一致", codes);

        // 2) 【实证修正一】照片整行串包含块内内容（needle ≥4）→ 命中
        //    场景：照片 OCR 把整条铭牌读成一行，图纸块内是逐条元素。
        if (key.Length >= MinContainLen)
        {
            var host = photo.FirstOrDefault(p => p.Key.Length > key.Length
                                                  && p.Key.Contains(key, StringComparison.Ordinal));
            if (host is not null)
                return Hit(ev, host, lowConf, blockLowConf, el.OcrConf, "命中：照片文本包含该内容（整行粘连场景）", codes);
        }

        // 3) 相似：长度 ≥6 且编辑距离 ≤1 → 文本差异（部分不同）→ 红（记最近照片文本供 P4 字符级对齐）
        if (key.Length >= FuzzyMinLen)
        {
            PhotoText? best = null;
            int bestDist = int.MaxValue;
            foreach (var p in photo)
            {
                if (Math.Abs(p.Key.Length - key.Length) > FuzzyMaxDist) continue;
                var d = Levenshtein(key, p.Key);
                if (d <= FuzzyMaxDist && d < bestDist) { bestDist = d; best = p; }
            }
            if (best is not null)
            {
                ev.Color = Red;
                ev.PhotoText = best.Text;
                ev.PhotoNorm = TrimCodeOverlap(best.Norm, codes);   // 最近照片文本框（P4 在差异附近画红）；2026-10-01 压码裁剪
                ev.MappingLevel = "L2";
                ev.Reason = $"相似但不一致：编辑距离 {bestDist}（部分不同→红）";
                return ev;
            }
        }

        // 4) 包含关系且短/长 ≥0.6 但非全等 → 文本差异（部分不同）→ 红
        //    （"块文本被照片整行包含"已在规则 2 判绿；此处是照片⊂块等反向部分包含）
        foreach (var p in photo)
        {
            var (s, l) = p.Key.Length <= key.Length ? (p.Key.Length, key.Length) : (key.Length, p.Key.Length);
            if (l == 0 || (double)s / l < ContainRatio) continue;
            if (key.Contains(p.Key, StringComparison.Ordinal) || p.Key.Contains(key, StringComparison.Ordinal))
            {
                ev.Color = Red;
                ev.PhotoText = p.Text;
                ev.PhotoNorm = TrimCodeOverlap(p.Norm, codes);     // 最近照片文本框（P4 落点）；2026-10-01 压码裁剪
                ev.MappingLevel = "L2";
                ev.Reason = "包含关系但不一致（部分不同→红）";
                return ev;
            }
        }

        // 5) 块内有、照片侧一条都没命中 → 文本差异一律红（黄仅图标/QR）
        ev.Color = Red;
        ev.PhotoNorm = null;          // 照片侧无对应坐标 → L3（P4 不画框，仅列表标注）
        ev.MappingLevel = "L3";
        ev.Reason = degraded
            ? "未检出但判红：照片侧 OCR 证据不足，按文本差异口径仍标红（degraded）"
            : "缺标：块内有此打标内容，照片侧未 OCR 到";
        return ev;
    }

    /// <summary>【2026-10-01 绿框压码修正】照片 OCR 检测框与照片侧码框（QR）重叠时，
    /// 裁掉重叠侧、保留较大剩余部分。场景：OCR 把 QR 图案误读为「口」并与相邻文本
    /// 合并成一行检测框（如「口QHRLA」），框左端落在 QR 上 → 文本绿框盖住码区。
    /// 仅影响画框坐标与映射锚点质量，不改任何四色结论。
    /// 裁后过小（宽&lt;0.02 或高&lt;0.005）则放弃裁剪维持原框（宁缺勿错）。</summary>
    private static double[]? TrimCodeOverlap(double[]? norm, IReadOnlyList<PhotoCode> codes)
    {
        if (norm is not { Length: 4 } || codes.Count == 0) return norm;
        double x0 = norm[0], y0 = norm[1], x1 = norm[0] + norm[2], y1 = norm[1] + norm[3];
        foreach (var c in codes)
        {
            if (c.Norm is not { Length: 4 } cn) continue;
            double cx1 = cn[0] + cn[2], cy1 = cn[1] + cn[3];
            double ix0 = Math.Max(x0, cn[0]), ix1 = Math.Min(x1, cx1);
            double iy0 = Math.Max(y0, cn[1]), iy1 = Math.Min(y1, cy1);
            if (ix1 <= ix0 || iy1 <= iy0) continue;
            // 明显重叠才裁：横向 ≥15% 文本宽 且 纵向 ≥50% 较矮者（码与文本同行场景）
            if (ix1 - ix0 < 0.15 * norm[2]) continue;
            if (iy1 - iy0 < 0.5 * Math.Min(norm[3], cn[3])) continue;
            double leftW = ix0 - x0, rightW = x1 - ix1;
            if (rightW >= leftW) x0 = ix1; else x1 = ix0;
        }
        double w = x1 - x0, h = y1 - y0;
        if (w < 0.02 || h < 0.005) return norm;
        return new[] { x0, y0, w, h };
    }

    private static ElementVerdict Hit(ElementVerdict ev, PhotoText p, bool lowConf, bool blockLowConf,
                                      double? ocrConf, string reason, IReadOnlyList<PhotoCode> codes)
    {
        ev.PhotoText = p.Text;
        ev.PhotoNorm = TrimCodeOverlap(p.Norm, codes);   // 照片侧直接检出框（L2）；2026-10-01 裁掉与照片码框重叠
                                                         //（OCR 把 QR 图案误读为「口」并入文本行 → 框起点压码）
        ev.MappingLevel = "L2";
        // 文本命中即绿（含低置信命中）：黄色仅图标/QR，文本差异才红、匹配即绿。
        ev.Color = Green;
        ev.Hit = true;
        ev.Reason = lowConf
            ? (blockLowConf
                ? $"块级低置信命中（仍判绿）：{reason}"
                : $"元素 OCR 低置信命中（conf={ocrConf:0.00}，仍判绿）：{reason}")
            : reason;
        return ev;
    }

    private static double[]? ToDouble(double?[]? a)
    {
        if (a is null || a.Length < 4) return null;
        return new[] { a[0] ?? 0, a[1] ?? 0, a[2] ?? 0, a[3] ?? 0 };
    }

    private static int Levenshtein(string a, string b)
    {
        if (a.Length == 0) return b.Length;
        if (b.Length == 0) return a.Length;
        var prev = new int[b.Length + 1];
        var cur = new int[b.Length + 1];
        for (int j = 0; j <= b.Length; j++) prev[j] = j;
        for (int i = 1; i <= a.Length; i++)
        {
            cur[0] = i;
            for (int j = 1; j <= b.Length; j++)
            {
                int cost = a[i - 1] == b[j - 1] ? 0 : 1;
                cur[j] = Math.Min(Math.Min(cur[j - 1] + 1, prev[j] + 1), prev[j - 1] + cost);
            }
            (prev, cur) = (cur, prev);
        }
        return prev[b.Length];
    }

    // ================= 坐标映射加强 + 多出降噪（2026-09-30） =================

    /// <summary>单字母标识（A/B 等端子、按键丝印）。元素侧因 MinTextLen=2 判灰跳过，
    /// 但照片 OCR 常把它们读成连串（如 AB），extra 变体比对需要它们兜底（2026-09-30 记录 1004）。</summary>
    private static bool IsSingleLetter(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return false;
        var t = text.Trim();
        return t.Length == 1 && char.IsLetter(t[0]);
    }
    /// <summary>疑似变量数据（机身编号/序列号/条码数字串）：每台机器不同，不参与打标比对。
    /// 判据：无汉字 + 长度 ≥5 + 数字字符占比 ≥50%。
    /// 反例不判灰：字母主导的型号串（QHSGK / QHR45）→ 仍走「多标黄」，型号不符不会被掩盖。</summary>
    private static bool IsSerialLike(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return false;
        var s = text.Trim();
        if (s.Length < 5) return false;
        foreach (var ch in s)
            if (ch >= '\u4e00' && ch <= '\u9fff') return false;   // 含汉字 → 真实文本
        int digits = 0;
        foreach (var ch in s)
            if (char.IsDigit(ch)) digits++;
        return (double)digits / s.Length >= 0.5;
    }

    /// <summary>二维仿射（块内相对坐标 → 照片归一化坐标）。</summary>
    private sealed class Affine2
    {
        public double A0, A1, A2, B0, B1, B2;   // x=A0+A1*u+A2*v ; y=B0+B1*u+B2*v
        public double[] Apply(double[] p) => new[] { A0 + A1 * p[0] + A2 * p[1], B0 + B1 * p[0] + B2 * p[1] };
        public double ScaleX => Math.Sqrt(A1 * A1 + B1 * B1);
        public double ScaleY => Math.Sqrt(A2 * A2 + B2 * B2);
    }

    /// <summary>最小二乘拟合仿射（基 [1,u,v]；≥3 点）。奇异/点数不足返回 null。</summary>
    private static Affine2? FitAffine(List<(double[] src, double[] dst)> pts)
    {
        if (pts.Count < 3) return null;
        // 正规方程：M^T M c = M^T y（3×3）
        var ata = new double[3, 3];
        var atx = new double[3];
        var aty = new double[3];
        foreach (var (s, d) in pts)
        {
            var b = new[] { 1.0, s[0], s[1] };
            for (int r = 0; r < 3; r++)
            {
                for (int c = 0; c < 3; c++) ata[r, c] += b[r] * b[c];
                atx[r] += b[r] * d[0];
                aty[r] += b[r] * d[1];
            }
        }
        var cx = Solve3(ata, atx);
        var cy = Solve3(ata, aty);
        if (cx is null || cy is null) return null;
        return new Affine2 { A0 = cx[0], A1 = cx[1], A2 = cx[2], B0 = cy[0], B1 = cy[1], B2 = cy[2] };
    }

    /// <summary>3×3 线性方程组求解（高斯消元，带主元）。奇异返回 null。</summary>
    private static double[]? Solve3(double[,] a, double[] b)
    {
        var m = new double[3, 4];
        for (int r = 0; r < 3; r++)
        {
            for (int c = 0; c < 3; c++) m[r, c] = a[r, c];
            m[r, 3] = b[r];
        }
        for (int k = 0; k < 3; k++)
        {
            int piv = k;
            for (int r = k + 1; r < 3; r++)
                if (Math.Abs(m[r, k]) > Math.Abs(m[piv, k])) piv = r;
            if (Math.Abs(m[piv, k]) < 1e-12) return null;
            if (piv != k)
                for (int c = 0; c < 4; c++) (m[k, c], m[piv, c]) = (m[piv, c], m[k, c]);
            for (int r = 0; r < 3; r++)
            {
                if (r == k) continue;
                double f = m[r, k] / m[k, k];
                for (int c = k; c < 4; c++) m[r, c] -= f * m[k, c];
            }
        }
        return new[] { m[0, 3] / m[0, 0], m[1, 3] / m[1, 1], m[2, 3] / m[2, 2] };
    }

    /// <summary>【P4+ 坐标映射加强】为缺标（红且无照片坐标）元素计算照片侧落点。
    /// 锚点 = 已命中元素的「块内相对位置 → 照片框中心」；≥3 对拟合仿射（L2），
    /// 2 对走并集粗配（L1），&lt;2 或无残差可信度则维持 L3 不画框（宁缺勿错）。
    /// <para>不改变任何四色结论，只补 PhotoNorm 落点。</para></summary>
    private static void MapMissingElements(BlockVerdict bv)
    {
        if (bv.BlockNorm is not { Length: 4 } bn || bn[2] <= 0 || bn[3] <= 0) return;

        // 收集锚点：块内相对中心 ↔ 照片归一化中心
        var pts = new List<(double[] src, double[] dst)>();
        foreach (var ev in bv.Elements)
        {
            if (ev.Norm is not { Length: 4 } n) continue;
            if (ev.PhotoNorm is not { Length: 4 } pn) continue;
            pts.Add((new[] { (n[0] + n[2] / 2.0 - bn[0]) / bn[2], (n[1] + n[3] / 2.0 - bn[1]) / bn[3] },
                     new[] { pn[0] + pn[2] / 2.0, pn[1] + pn[3] / 2.0 }));
        }
        if (pts.Count < 2) return;

        // 【2026-10-05 锚点退化守护】整行粘连场景多个元素命中同一 host 框 →
        // 全部锚点照片侧中心几乎重合（跨度 <2%）→ 无缩放/平移信息，映射不可信
        //（1076 实证：APP下载 红框被映射到照片中部无关位置）→ 放弃落点维持 L3。
        double dxSpan = pts.Max(p => p.dst[0]) - pts.Min(p => p.dst[0]);
        double dySpan = pts.Max(p => p.dst[1]) - pts.Min(p => p.dst[1]);
        if (dxSpan < 0.02 && dySpan < 0.02) return;

        Affine2? aff = FitAffine(pts);
        string level = "L2";
        if (aff is null)
        {
            // L1：并集粗配（平移 + 各向缩放，无旋转）
            double su0 = pts.Min(p => p.src[0]), su1 = pts.Max(p => p.src[0]);
            double sv0 = pts.Min(p => p.src[1]), sv1 = pts.Max(p => p.src[1]);
            double du0 = pts.Min(p => p.dst[0]), du1 = pts.Max(p => p.dst[0]);
            double dv0 = pts.Min(p => p.dst[1]), dv1 = pts.Max(p => p.dst[1]);
            double sw = su1 - su0, sh = sv1 - sv0, dw = du1 - du0, dh = dv1 - dv0;
            if (sw <= 1e-6 || sh <= 1e-6) return;
            double sx = dw / sw, sy = dh / sh;
            aff = new Affine2 { A1 = sx, A0 = du0 - su0 * sx, B2 = sy, B0 = dv0 - sv0 * sy };
            level = "L1";
        }
        else
        {
            // 残差校验：锚点自身映射误差过大 → 先剔除最差锚点重拟合一次（≥4 对时），
            // 仍超阈值才放弃落点（宁缺勿错）。2026-10-01：单个坏锚点（如照片 OCR 把 QR
            // 图案并入文本行的偏移框）会带偏仿射 → 红框偏移/只盖一半的根因之一。
            double MeanErr(Affine2 f, List<(double[] src, double[] dst)> ps)
            {
                double e = 0;
                foreach (var (s, d) in ps)
                {
                    var q = f.Apply(s);
                    e += Math.Sqrt((q[0] - d[0]) * (q[0] - d[0]) + (q[1] - d[1]) * (q[1] - d[1]));
                }
                return e / Math.Max(ps.Count, 1);
            }
            double err = MeanErr(aff, pts);
            if (err > 0.10 && pts.Count >= 4)
            {
                int worst = 0; double worstE = -1;
                for (int i = 0; i < pts.Count; i++)
                {
                    var q = aff.Apply(pts[i].src);
                    double e = Math.Sqrt((q[0] - pts[i].dst[0]) * (q[0] - pts[i].dst[0])
                                       + (q[1] - pts[i].dst[1]) * (q[1] - pts[i].dst[1]));
                    if (e > worstE) { worstE = e; worst = i; }
                }
                var pts2 = new List<(double[] src, double[] dst)>();
                for (int i = 0; i < pts.Count; i++) if (i != worst) pts2.Add(pts[i]);
                var aff2 = FitAffine(pts2);
                if (aff2 is not null && MeanErr(aff2, pts2) <= 0.10)
                {
                    aff = aff2;
                    err = MeanErr(aff2, pts2);
                }
            }
            if (err > 0.10) return;   // 归一化残差 >10% → 放弃落点（宁缺勿错）
        }

        foreach (var ev in bv.Elements)
        {
            if (ev.PhotoNorm is { Length: 4 }) continue;   // 已有落点（直接检出）
            if (ev.Color != Red) continue;                  // 只给缺标红元素补落点
            if (ev.Norm is not { Length: 4 } n) continue;
            var rel = new[] { (n[0] + n[2] / 2.0 - bn[0]) / bn[2], (n[1] + n[3] / 2.0 - bn[1]) / bn[3] };
            var c = aff.Apply(rel);
            double w = aff.ScaleX * (n[2] / bn[2]);
            double h = aff.ScaleY * (n[3] / bn[3]);
            // 【2026-10-05 退化守护】锚点近乎共线时最小二乘/并集粗配缩放爆炸
            //（实测 1073：w≈23.16 → Math.Clamp(min=0, max=1-w=-22.16) 抛
            //  "'0' cannot be greater than '-22.16'" → blockMatch 整体失败）。
            //  映射框明显不合理（非正数/超半屏）→ 放弃落点维持 L3（宁缺勿错）。
            if (double.IsNaN(w) || double.IsNaN(h) || w <= 0 || h <= 0 || w > 0.5 || h > 0.5) return;
            // 最小可视尺寸：太小的框看不清，给一个下限
            w = Math.Max(w, 0.02); h = Math.Max(h, 0.015);
            double x = Math.Clamp(c[0] - w / 2.0, 0.0, Math.Max(0.0, 1.0 - w));
            double y = Math.Clamp(c[1] - h / 2.0, 0.0, Math.Max(0.0, 1.0 - h));
            ev.PhotoNorm = new[] { x, y, w, h };
            ev.MappingLevel = level == "L2" ? "L2" : "L1";
            ev.Reason += level == "L2" ? "（落点：块内命中锚点仿射映射）" : "（落点：命中框并集粗配 L1）";
        }
    }
}
