#!/usr/bin/env bash
# 批量验证 bash 版：每张图独立进程 + timeout 兜底 + 重试。
# 输出丢弃(>/dev/null)以避开 Paddle GBK stderr 的管道问题；结果靠 result.json 落盘。
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/paddle/Scripts/python.exe"
WORKER="E:/workaaa/shengchanguanli/src/Platform/Modules/Drawings/_verify_worker.py"
SRC="E:/生产打标效果图"
OUT="E:/workaaa/shengchanguanli/data/seg/_batch_verify"
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 KMP_DUPLICATE_LIB_OK=TRUE
mkdir -p "$OUT"
n=0
for pdf in "$SRC"/*.pdf; do
  n=$((n+1))
  base=$(basename "$pdf")
  sname=$(echo "$base" | tr ' /()' '____')
  sub="$OUT/$sname"
  mkdir -p "$sub"
  ok=0
  for att in 1 2 3 4 5; do
    timeout 120 "$PY" "$WORKER" "$pdf" "$sub" >/dev/null 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then ok=1; break; fi
    # rc=124 timeout, 139 segfault, 其他错误 -> 重试
  done
  if [ $ok -eq 1 ]; then echo "OK   [$n] $base"; else echo "FAIL [$n] $base (all retry failed)"; fi
done
echo "=== batch bash done ==="
