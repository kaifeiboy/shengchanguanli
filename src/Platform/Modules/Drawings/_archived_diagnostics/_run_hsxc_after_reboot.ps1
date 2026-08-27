# 一次性启动任务：重启后自动跑 HSXC 图纸 PP-DocLayoutV3 切图（flag 控制只跑一次）
$flag = "E:/workaaa/shengchanguanli/data/seg/_v3final_hsxc/run_after_reboot.flag"
$log  = "E:/workaaa/shengchanguanli/data/seg/_v3final_hsxc/after_reboot.log"
if (-not (Test-Path $flag)) { exit 0 }   # 无 flag 不跑（保证一次性）
Remove-Item $flag -Force -ErrorAction SilentlyContinue
$py     = "C:\Users\Administrator\.workbuddy\binaries\python\envs\paddle\Scripts\python.exe"
$script = "E:/workaaa/shengchanguanli/src/Platform/Modules/Drawings/_demo_hsxc.py"
Set-Content -Path $log -Value "HSXC auto-seg started $(Get-Date)`r`n"
$env:OMP_NUM_THREADS=1; $env:MKL_NUM_THREADS=1; $env:KMP_DUPLICATE_LIB_OK=TRUE
& $py $script >> $log 2>&1
Add-Content -Path $log -Value "HSXC auto-seg exit $($LASTEXITCODE) $(Get-Date)`r`n"
