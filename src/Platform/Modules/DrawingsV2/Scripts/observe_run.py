# -*- coding: utf-8 -*-
"""observe 的 C# 桥接入口（与 vpdf_run.py 同一套路）。

    python observe_run.py observe <image.jpg> --json-only [--no-warp] [-o out.json]
    python observe_run.py verify <image.jpg> --regions regions.json --json-only

为什么需要薄壳：平台 PythonProcessFactory 会把工作目录固定到脚本目录并清空环境，
依赖调用方 sys.path 的 `python -m observe` 在计划任务下不可靠。
verify 模式：按图纸侧 mark 的 normBbox 在照片预期位置做定点感知（observe-verify/1）。
regions 传 JSON 文件路径（避免命令行长度/转义问题），格式：
    [{"id":"m005","kind":"qr","norm_bbox":[0.58,0.62,0.036,0.05]}, ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from observe.pipeline import observe_file  # noqa: E402
from observe.verify import verify_file  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="observe", description="照片侧感知（v2 perception）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("observe", help="感知一张照片")
    p.add_argument("image")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--no-warp", action="store_true", help="跳过透视矫正")
    p.add_argument("--json-only", action="store_true",
                   help="stdout 只输出纯 JSON（供 C# 桥接层消费）")
    p.add_argument("--indent", type=int, default=2)

    v = sub.add_parser("verify", help="按预期位置定点感知一张照片")
    v.add_argument("image")
    v.add_argument("--regions", required=True, help="区域清单 JSON 文件路径")
    v.add_argument("-o", "--out", default=None)
    v.add_argument("--no-warp", action="store_true")
    v.add_argument("--json-only", action="store_true")
    v.add_argument("--indent", type=int, default=2)

    a = ap.parse_args(argv)
    if a.cmd == "observe":
        doc = observe_file(a.image, do_warp=not a.no_warp)
    else:
        with open(a.regions, "r", encoding="utf-8") as f:
            regions = json.load(f)
        doc = verify_file(a.image, regions, do_warp=not a.no_warp)
    text = json.dumps(doc, ensure_ascii=False, indent=a.indent)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
