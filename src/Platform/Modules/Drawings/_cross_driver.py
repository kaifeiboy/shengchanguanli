#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
_cross_driver.py - 跨图批量验证（方向A纪律模式）
==============================================
对 生产打标效果图/*.pdf 逐一调用 segment_blocks.py（Paddle 自动切图+宽高比舍弃规则），
产出：每块标注原图(page_annotated.png) + manifest + 汇总 summary.json。

纪律铁律：绝不强杀子进程。
  - 用 subprocess.Popen + communicate()（无 timeout），worker 自身 100s 硬看门狗
    保证必然自行退出（os._exit 兜底），driver 只等待、不杀。
  - 强杀会毒化 Paddle 会话（见 MEMORY.md 续14），故此处严格不用 timeout/kill。
"""
import subprocess, sys, os, json, glob
from PIL import Image, ImageDraw

PADDLE_PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\paddle\Scripts\python.exe"
SCRIPT = r"E:\workaaa\shengchanguanli\src\Platform\Modules\Drawings\segment_blocks.py"
PDF_DIR = r"E:\生产打标效果图"
OUT = r"E:\workaaa\shengchanguanli\data\seg\_cross"
os.makedirs(OUT, exist_ok=True)

pdfs = sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf")))
print(f"[driver] {len(pdfs)} PDFs found", flush=True)

summary = []
for i, pdf in enumerate(pdfs):
    name = os.path.splitext(os.path.basename(pdf))[0]
    sub = os.path.join(OUT, name)
    os.makedirs(sub, exist_ok=True)
    print(f"[{i}/{len(pdfs)}] {name}", flush=True)
    try:
        env = dict(os.environ)
        env.update({"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                    "KMP_DUPLICATE_LIB_OK": "TRUE"})
        p = subprocess.Popen(
            [PADDLE_PY, SCRIPT, "segment", pdf, str(i), sub, "200"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        # 纪律：communicate() 无 timeout → 等待 worker 自然退出（≤100s 看门狗）
        out, err = p.communicate()
        rc = p.returncode
        out = (out or b"").decode("utf-8", "replace")
        man = None
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    man = json.loads(line)
                    break
                except Exception:
                    pass
        if man and "blocks" in man:
            blocks = man["blocks"]
            full = os.path.join(sub, man.get("source") or f"{i}_full.png")
            annotated = os.path.join(sub, "page_annotated.png")
            if os.path.exists(full):
                im = Image.open(full).convert("RGB")
                d = ImageDraw.Draw(im)
                colors = ["red", "lime", "cyan", "yellow", "orange", "magenta", "springgreen"]
                for k, b in enumerate(blocks):
                    x, y, w, h = b["x"], b["y"], b["w"], b["h"]
                    d.rectangle([x, y, x + w, y + h], outline=colors[k % len(colors)], width=3)
                    d.text((x, max(0, y - 14)), f"#{k}", fill=colors[k % len(colors)])
                im.save(annotated)
            summary.append({
                "pdf": name, "rc": rc, "algo": man.get("algorithm"),
                "mode": man.get("mode"), "n_blocks": len(blocks),
                "blocks": [{"idx": b["idx"], "w": b["w"], "h": b["h"],
                           "ar": round(max(b["w"], b["h"]) / max(1, min(b["w"], b["h"])), 2)}
                          for b in blocks],
            })
        else:
            summary.append({"pdf": name, "rc": rc, "error": "no manifest",
                            "stderr_tail": (err or b"").decode("utf-8", "replace")[-240:]})
    except Exception as e:
        summary.append({"pdf": name, "rc": "ERR", "error": str(e)[:200]})

with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print("=== SUMMARY ===")
print(json.dumps(summary, ensure_ascii=False, indent=2))
