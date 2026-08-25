"""验证：若新增「纯数字 LCD 示例灰化豁免」，是否会误伤序列号/日期码/电话号码。

做法：
 1) 扫描 data/app.db 全部 drawing_blocks 的 RawText（块参考图 OCR 文本行）。
 2) 复用 diff_visualizer 现有判定函数，标出每条文本当前是否已被灰化
    （序列号 / LCD 屏显词表）——这些是"本来就该跳过"的。
 3) 找出"纯数字 且 当前未被灰化"的文本 = 本应正常比对的真实打标文本。
 4) 对这些真实文本施加候选豁免规则，量化会误伤多少。
"""
import os, re, sqlite3, json, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import diff_visualizer as dv   # 复用现有判定函数

DB = r"E:\workaaa\shengchanguanli\data\app.db"


def parse_lines(raw):
    if raw is None:
        return []
    raw = raw.strip()
    if not raw:
        return []
    # 可能是 JSON 数组或换行文本
    try:
        obj = json.loads(raw)
        if isinstance(obj, list):
            return [str(x) for x in obj if str(x).strip()]
    except Exception:
        pass
    return [l for l in raw.replace("\\n", "\n").split("\n") if l.strip()]


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT DrawingId,BIdx,RawText FROM drawing_blocks ORDER BY DrawingId,BIdx"
    ).fetchall()
    con.close()

    # 收集：纯数字 且 当前未被灰化（即本应正常比对的真实打标文本）
    real_pure = []          # (did, bidx, text, len)
    already_gray_pure = []  # 纯数字但已被灰化（序列号/LCD 词表）
    phone_like = []         # 含数字的电话/热线类（检测是否会被纯数字规则命中）

    for r in rows:
        did, bidx, raw = r["DrawingId"], r["BIdx"], r["RawText"]
        for line in parse_lines(raw):
            t = line.strip()
            if not t:
                continue
            is_pure = bool(re.fullmatch(r"\d+", t))
            is_serial = dv._is_serial_number(t)
            is_lcd = dv._is_lcd_display_text(t)
            is_phone = bool(re.search(r"热线|电话|TEL|400|800|\d{3}-\d{8}|\d{4}-\d{7}", t))
            if is_phone:
                phone_like.append((did, bidx, t))
            if is_pure:
                if is_serial or is_lcd:
                    already_gray_pure.append((did, bidx, t, len(t)))
                else:
                    real_pure.append((did, bidx, t, len(t)))

    print("=" * 70)
    print(f"块总数: {len(rows)}")
    print(f"纯数字文本行(已被灰化=序列号/LCD词表): {len(already_gray_pure)}")
    print(f"纯数字文本行(当前未被灰化=本应正常比对): {len(real_pure)}")
    print(f"电话/热线类文本行(含数字): {len(phone_like)}")
    print("=" * 70)

    print("\n[当前未被灰化的纯数字真实文本] —— 这些是「纯数字 LCD 豁免」会误伤的对象：")
    by_len = {}
    for did, bidx, t, ln in real_pure:
        by_len.setdefault(ln, []).append((did, bidx, t))
    for ln in sorted(by_len):
        items = by_len[ln]
        sample = ", ".join(f"{t!r}(did{did}-b{bidx})" for did, bidx, t in items[:6])
        more = "" if len(items) <= 6 else f" …(+{len(items)-6})"
        print(f"  长度{ln}: {len(items)}条  {sample}{more}")

    # 候选豁免规则对真实文本的命中率
    print("\n[候选豁免规则 对「真实纯数字文本」的误伤量]")
    rules = {
        "纯数字 长度1-4": lambda t: 1 <= len(t) <= 4,
        "纯数字 长度1-6": lambda t: 1 <= len(t) <= 6,
        "纯数字 长度1-8": lambda t: 1 <= len(t) <= 8,
        "全同数字(8888/0000)": lambda t: len(set(t)) == 1,
        "全同数字 或 长度1-4": lambda t: (len(set(t)) == 1) or (1 <= len(t) <= 4),
    }
    for name, fn in rules.items():
        hit = [x for x in real_pure if fn(x[2])]
        print(f"  {name:24s} -> 误伤 {len(hit)}/{len(real_pure)} 条")
        if hit:
            shown = ", ".join(f"{t!r}(did{d}-b{b})" for d, b, t, l in hit[:8])
            print(f"      示例: {shown}")

    # 电话类是否会被纯数字规则命中（去掉非数字后）
    print("\n[电话/热线类 去掉符号后是否变纯数字(会被命中)]")
    for did, bidx, t in phone_like:
        digits = re.sub(r"\D", "", t)
        flag = "⚠纯数字化" if digits and re.fullmatch(r"\d+", digits) and len(digits) >= 6 else "安全(含符号/字母)"
        print(f"  {t!r:30s} did{did}-b{bidx}  去符号={digits!r}  {flag}")


if __name__ == "__main__":
    main()
