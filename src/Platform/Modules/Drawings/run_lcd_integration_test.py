# -*- coding: utf-8 -*-
"""did112-b1 屏显灰化集成测试（_compare_block 层，用真实 DB 数据）。

模拟：照片显示了除 LCD(8888) 外的全部文本 → 8888 在块侧"缺失"。
断言：8888 经屏显门控→gray_block，且不进 red_block（红框消失）。
开关运行时强制开启（不改源文件）。
"""
import os, sqlite3
import diff_visualizer as dv

dv.ENABLE_DISPLAY_REGION_GRAY = True  # 仅本进程

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
DB = os.path.join(REPO, "data", "app.db")
SEG = os.path.join(REPO, "data", "seg")

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
r = con.execute("SELECT FileRel,RawText FROM drawing_blocks WHERE DrawingId=112 AND BIdx=1").fetchone()
con.close()

raw = r["RawText"] or ""
block_texts = [t.strip() for t in raw.split("\n") if t.strip()]
blk = os.path.join(SEG, "112", "blocks", os.path.basename(r["FileRel"] or ""))
print(f"did112-b1 块文本({len(block_texts)}): {block_texts}")

block_lines = [{"text": t, "bbox": [[0, 0], [10, 0], [10, 10], [0, 10]], "conf": 1.0} for t in block_texts]
# 照片显示除 8888 外的全部文本
photo_texts = [t for t in block_texts if dv._norm_text(t) != "8888"]
photo_lines = [{"text": t, "bbox": [[0, 0], [10, 0], [10, 10], [0, 10]], "conf": 1.0} for t in photo_texts]

# 计算屏显文本键（与 run_on_photo 同路径：真实块 OCR）
ocr_blk = dv._ocr_image(blk)
_regions, display_texts = dv._detect_display_regions(blk, ocr_blk)
print(f"屏显键={sorted(display_texts)}")

res = dv._compare_block(photo_lines, block_lines, [], [], cross_block_filter=False, display_texts=display_texts)
n_red = len(res["red_block"])
n_gray = len(res["gray_block"])
n_green = len(res["green_block"])
print(f"结果: red_block={n_red} gray_block={n_gray} green_block={n_green}")

# 断言：8888 不应进红框；应被灰化
ok = (n_red == 0) and (n_gray >= 1) and ("8888" in display_texts)
print("PASS ✅ 8888 灰化且红框为空" if ok else "FAIL ❌")
sys_exit = 0 if ok else 1
import sys; sys.exit(sys_exit)
