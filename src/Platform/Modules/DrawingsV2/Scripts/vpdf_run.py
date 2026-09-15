# -*- coding: utf-8 -*-
"""vpdf 的 C# 桥接入口。

为什么需要这一个薄壳，而不是直接 `python -m vpdf.cli`：

  1. 平台 PythonProcessFactory 把工作目录固定到**脚本所在目录**，并清空继承的环境变量。
     直接 `-m vpdf.cli` 依赖调用方的 sys.path / cwd，在计划任务拉起时并不可靠。
     本脚本位于 Scripts/ 下，因此 `import vpdf` 必然可解析（Python 把脚本目录放进 sys.path[0]）。
  2. 让 C# 侧只需定位**一个**文件（vpdf_run.py），不需要感知 vpdf 包内部结构。

用法：
    python vpdf_run.py parse <file.pdf> --json-only
"""
from __future__ import annotations

import os
import sys

# 双保险：显式把 Scripts/ 目录放进 sys.path，任何 cwd 下都能 import vpdf
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from vpdf.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
