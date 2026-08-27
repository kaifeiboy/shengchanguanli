#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多配置探针：逐一尝试禁用 MKLDNN / 限制 oneDNN 指令集，定位能避开段错误的配置。"""
import subprocess, os
PADDLE = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\paddle\Scripts\python.exe"
WORKER = r"E:\workaaa\shengchanguanli\src\Platform\Modules\Drawings\_diag_one.py"
configs = {
    "baseline": {},
    "no_mkldnn": {"FLAGS_use_mkldnn": "false"},
    "isa_avx2": {"DNNL_MAX_CPU_ISA": "AVX2"},
    "no_mkldnn_avx2": {"FLAGS_use_mkldnn": "false", "DNNL_MAX_CPU_ISA": "AVX2"},
    "isa_sse41": {"DNNL_MAX_CPU_ISA": "SSE4_1"},
}
base_env = dict(os.environ)
base_env.update({"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "KMP_DUPLICATE_LIB_OK": "TRUE"})
for name, extra in configs.items():
    env = dict(base_env)
    env.update(extra)
    print("\n##### CONFIG=%s  %s #####" % (name, extra), flush=True)
    p = subprocess.Popen([PADDLE, WORKER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = p.communicate()
    rc = p.returncode
    txt = (out or b"").decode("utf-8", "replace")
    ok = "OK_PREDICT" in txt
    print("rc=%s  OK_PREDICT=%s" % (rc, ok), flush=True)
    if not ok:
        tail = (err or b"").decode("utf-8", "replace")[-300:]
        print("STDERR_TAIL:", tail, flush=True)
