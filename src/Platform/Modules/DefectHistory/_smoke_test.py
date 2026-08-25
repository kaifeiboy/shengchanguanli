# -*- coding: utf-8 -*-
"""V-1 冒烟：在 data/defecthistory_smoke（defecthistory_test_xlsx 的副本）上以
subprocess.run(生产方式) 验证 defect_history.py 全部命令。"""
import sys, os, json, shutil, subprocess

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

VENV_PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
SCRIPT = r"E:\workaaa\shengchanguanli\src\Platform\Modules\DefectHistory\defect_history.py"
SRC_COPY = r"E:\workaaa\shengchanguanli\data\defecthistory_test_xlsx"
SMOKE = r"E:\workaaa\shengchanguanli\data\defecthistory_smoke"
OUT = r"E:\workaaa\shengchanguanli\data\defecthistory_smoke_out"
IMG = r"E:\workaaa\shengchanguanli\data\defecthistory_smoke\sample.png"

fail = []


def run(args, cwd=None):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([VENV_PY, SCRIPT] + args, cwd=cwd or os.path.dirname(SCRIPT),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"success": False, "error": "非JSON输出: " + p.stdout[:200] + " | stderr: " + p.stderr[:200], "rc": p.returncode}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        fail.append(name)


# 0) 准备副本
if os.path.exists(SMOKE):
    shutil.rmtree(SMOKE, ignore_errors=True)
os.makedirs(SMOKE, exist_ok=True)
for f in os.listdir(SRC_COPY):
    if f.endswith(".xlsx"):
        shutil.copy2(os.path.join(SRC_COPY, f), os.path.join(SMOKE, f))
# 样例图片（PIL 生成 40x30 红图）
from PIL import Image as PILImage
PILImage.new("RGB", (40, 30), (200, 30, 30)).save(IMG, "PNG")

# 1) meta
r = run(["meta", "--root", SMOKE])
check("meta", r.get("success") and len(r.get("months", [])) == 5,
      json.dumps(r, ensure_ascii=False)[:200])

# 2) query FD01
r = run(["query", "--root", SMOKE, "--model", "FD01"])
check("query FD01", r.get("success") and r.get("total", 0) >= 1,
      "total=%s" % r.get("total"))

# 3) query rd504 + 月份过滤
r = run(["query", "--root", SMOKE, "--model", "rd504", "--month", "2025-11"])
check("query rd504 2025-11", r.get("success") and r.get("total", 0) == 1,
      "total=%s" % r.get("total"))
if r.get("success"):
    row0 = r["groups"][0]["rows"][0]
    check("query 行含 hasImage/imgCount", "hasImage" in row0 and "imgCount" in row0)

# 4) add_single（自动建 2026-06 + 带图）
r = run(["add_single", "--root", SMOKE, "--date", "2026.6.3", "--sheet", "组装",
         "--model", "TEST-MODEL", "--reason", "V1冒烟测试", "--line", "A1-1",
         "--qty", "2台", "--stage", "QA", "--handle", "作业不良",
         "--image", IMG])
check("add_single 建月+带图", r.get("success") and r.get("createdMonth") is True and r.get("images") == 1,
      json.dumps(r, ensure_ascii=False)[:200])

# 5) 查询新加的 TEST-MODEL 且该行有图
r = run(["query", "--root", SMOKE, "--model", "TEST-MODEL"])
hit = None
if r.get("success"):
    for g in r["groups"]:
        for row in g["rows"]:
            if row["model"] == "TEST-MODEL":
                hit = row
check("query TEST-MODEL 命中且 hasImage", hit is not None and hit.get("hasImage"),
      json.dumps(hit, ensure_ascii=False)[:200] if hit else "未命中")

# 6) create_month 2026-07
r = run(["create_month", "--root", SMOKE, "--year", "2026", "--month", "7"])
check("create_month 2026-07", r.get("success") and r.get("exists") is False,
      json.dumps(r, ensure_ascii=False)[:120])

# 7) delete_row：删除 2026-06 组装 TEST-MODEL 行（row 为数据行索引）
r = run(["delete_row", "--root", SMOKE, "--file", "2026年6月份IPQC及QA发现检验问题点.xlsx",
         "--sheet", "组装", "--row", "0"])
check("delete_row", r.get("success") and r.get("remainingRows", -1) >= 0,
      json.dumps(r, ensure_ascii=False)[:150])
r = run(["query", "--root", SMOKE, "--model", "TEST-MODEL"])
check("delete 后查不到", r.get("success") and r.get("total") == 0)

# 8) 批量上传：构造 .xlsx 上传（表头 + 2 行 + 1 图，含 2026-05 缺失月）
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
up = os.path.join(SMOKE, "_upload_test.xlsx")
wb = Workbook()
ws = wb.active
ws.title = "组装"
ws.append(["日期", "线别", "型号", "问题原因", "数量", "发生工程", "不良图片", "处理方式"])
ws.append(["2026.5.10", "A2-2", "BATCH-M1", "批量上传测试1", "3台", "QA", "", "来料不良"])
ws.append(["2026.5.11", "A3-1", "BATCH-M2", "批量上传测试2", "5台", "QA", "", "作业不良"])
ws.append(["2026.7.1", "A1-1", "BATCH-M3", "缺失月2026-07", "1台", "IPQC", "", "作业不良"])
ws.add_image(XlImage(IMG), "G2")
wb.save(up)

r = run(["batch_import", "--root", SMOKE, "--upload", up, "--dry-run"])
check("batch dry-run", r.get("success") and r.get("dry_run") and r.get("imported") == 0,
      json.dumps(r, ensure_ascii=False)[:250])
r = run(["batch_import", "--root", SMOKE, "--upload", up])
check("batch 真实导入", r.get("success") and r.get("imported") == 3,
      json.dumps(r, ensure_ascii=False)[:250])
check("batch 建缺失月", set(["2026-05"]) <= set(r.get("createdMonths", [])),
      str(r.get("createdMonths")))
r = run(["query", "--root", SMOKE, "--model", "BATCH-M1"])
hit = None
if r.get("success"):
    for g in r["groups"]:
        for row in g["rows"]:
            if row["model"] == "BATCH-M1":
                hit = row
check("batch 行带图", hit is not None and hit.get("hasImage"),
      json.dumps(hit, ensure_ascii=False)[:200] if hit else "未命中")

# 9) 删除前重建 2026-07（create_month 已建）→ batch 的 BATCH-M3 进了 2026-07
r = run(["query", "--root", SMOKE, "--model", "BATCH-M3"])
check("batch 进 2026-07 文件", r.get("success") and r.get("total") == 1 and r["groups"][0]["month"] == "2026-07")

# 10) export full
r = run(["export", "--root", SMOKE, "--out", OUT, "--full", "1"])
check("export full", r.get("success") and os.path.exists(os.path.join(OUT, r["file"])),
      json.dumps(r, ensure_ascii=False)[:150])
r = run(["export", "--root", SMOKE, "--out", OUT, "--model", "FD01"])
check("export 查询结果", r.get("success") and os.path.exists(os.path.join(OUT, r["file"])),
      json.dumps(r, ensure_ascii=False)[:150])

# 11) template
tpl = os.path.join(OUT, "_template.xlsx")
r = run(["template", "--out", tpl])
check("template", r.get("success") and os.path.exists(tpl))

# 12) extract_images
r = run(["extract_images", "--root", SMOKE])
n = 0
if r.get("success"):
    for f in r["files"]:
        for s in f.get("sheets", []):
            n += len(s["images"])
check("extract_images 幂等总数>=24", r.get("success") and n >= 24, "n=%s" % n)
r2 = run(["extract_images", "--root", SMOKE])
fresh = sum(1 for f in r2.get("files", []) if f.get("fresh"))
check("extract_images 二次 fresh", fresh == len(r2.get("files", [])), "fresh=%d" % fresh)

print("----")
if fail:
    print("FAILED:", fail)
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
