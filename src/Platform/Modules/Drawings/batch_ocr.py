#!/usr/bin/env python3
# 批量 OCR v2 — 经 ocr_cli.py 统一引擎
# ================================================
# 对多个图片在同一 Python 进程内「顺序」各跑一次 ocr_cli.py（含v6图纸块检测），
# 结果以 {图片路径: 识别文本} 写入 out_json（UTF-8）。
#
# v1 直接裸调 tesseract.exe（零预处理）→ 图纸块OCR全乱码。
# v2 改为经 ocr_cli.py 统一引擎，自动获得:
#   - 图纸块检测(is_drawing_block) + 形态学去线 + ROI裁剪 + 放大
#   - 照片的多策略光照鲁棒性(保留)
#
# 用法: python batch_ocr.py <out_json> <img1> <img2> ...
import sys, os, json, subprocess

PYTHON = sys.executable  # 当前 Python 解释器


def norm_path(p):
    """把 Git-Bash 风格路径 /e/work/... 归一成 Windows 盘符 e:/work/...，
    避免 Python 子进程把前导 /e/ 当成 POSIX 根目录 -> FileNotFoundError。
    已是正确 Windows 路径则原样返回。"""
    if not p:
        return p
    if len(p) > 2 and p[0:1] == "/" and p[2:3] == "/":
        drive = p[1:2]
        if drive.isalpha():
            return drive + ":/" + p[3:]
    return p


def _find_script(name):
    """向上查找脚本文件（与 OcrService.FindScript 同逻辑）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, name),
        name,
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    # 向上搜索
    cur = base
    for _ in range(6):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        p = os.path.join(parent, name)
        if os.path.exists(p):
            return p
        cur = parent
    return name  # fallback: 让 subprocess 报错


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: batch_ocr.py <out_json> <img1> [img2...]\n")
        sys.exit(2)

    out_json = norm_path(sys.argv[1])
    images = [norm_path(x) for x in sys.argv[2:]]
    result = {}
    tmpdir = os.path.dirname(out_json) or "."
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except OSError:
        pass

    ocr_cli = _find_script("ocr_cli.py")

    import json as _json
    THRESH = float(os.environ.get("OCR_CONF_THRES", "70"))  # 百分比阈值（0~100）；RapidOCR conf 是 0~1 小数，需 *100 后比较
    # T-D2 标定结论：55 会让 conf=69.8% 的噪声块漏网

    for img in images:
        try:
            r = subprocess.run(
                [PYTHON, ocr_cli, img],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "OCR_JSON": "1"},
            )
            raw = (r.stdout or "").strip()
            text = raw
            if raw.startswith("{"):
                try:
                    obj = _json.loads(raw)
                    t = obj.get("text", "")
                    mc = bool(obj.get("model_candidate", False))
                    conf = float(obj.get("conf", 0.0))
                    # 闸门: 像型号 但 置信度过低 且 非 QR 来源 -> 判为噪声清空
                    # 修复: conf(0~1小数) * 100 转为百分比后再与 THRESH 比较
                    if mc and conf * 100 < THRESH and "[QR:]" not in t:
                        t = ""
                    text = t
                except Exception:
                    text = raw  # 解析失败则保留原文，不丢数据
            result[img] = text
        except Exception:
            result[img] = ""

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
