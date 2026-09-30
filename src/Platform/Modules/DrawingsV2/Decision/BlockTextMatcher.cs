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
        bool degraded = photoUsable.Count < MinPhotoTextsForRed;
        res.Degraded = degraded;
        if (degraded)
            res.DegradeReason = $"照片侧仅 OCR 到 {photoUsable.Count} 条有效文本（<{MinPhotoTextsForRed}）；文本差异仍判红（用户口径：文本一律绿或红）";

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

        // 照片多出：Top 块内找不到的内容 → 红（多标，属文本差异）
        var consumed = new HashSet<string>(StringComparer.Ordinal);
        foreach (var ev in top.Elements)
            if (ev.PhotoText is { Length: > 0 } pt) consumed.Add(TextNormalizer.LooseKey(pt));

        foreach (var p in photoUsable)
        {
            if (consumed.Contains(p.Key)) continue;
            res.Extra.Add(new ElementVerdict
            {
                Kind = "photo_extra",
                Text = p.Text,
                Color = Red,
                Reason = "照片多出：Top 块内没有此内容（多标，文本差异→红）",
                PhotoText = p.Text,
                Norm = null,
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
        if (exact is not null) return Hit(ev, exact, lowConf, blockLowConf, el.OcrConf, "命中：文本一致");

        // 2) 【实证修正一】照片整行串包含块内内容（needle ≥4）→ 命中
        //    场景：照片 OCR 把整条铭牌读成一行，图纸块内是逐条元素。
        if (key.Length >= MinContainLen)
        {
            var host = photo.FirstOrDefault(p => p.Key.Length > key.Length
                                                  && p.Key.Contains(key, StringComparison.Ordinal));
            if (host is not null)
                return Hit(ev, host, lowConf, blockLowConf, el.OcrConf, "命中：照片文本包含该内容（整行粘连场景）");
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
                ev.PhotoNorm = best.Norm;   // 最近照片文本框（P4 在差异附近画红）
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
                ev.PhotoNorm = p.Norm;     // 最近照片文本框（P4 落点）
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

    private static ElementVerdict Hit(ElementVerdict ev, PhotoText p, bool lowConf, bool blockLowConf,
                                      double? ocrConf, string reason)
    {
        ev.PhotoText = p.Text;
        ev.PhotoNorm = p.Norm;          // 照片侧直接检出框（L2）
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
}
