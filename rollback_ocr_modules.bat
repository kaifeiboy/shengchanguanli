@echo off
REM 一键回滚脚本 - 恢复到原有OCR模块

echo ============================================================
echo OCR Module Rollback Script
echo ============================================================

cd /d "%~dp0src\Platform\Modules\Drawings"

echo.
echo 1. Checking backup files...
if not exist "backup_original\ocr_cli.py" (
    echo ERROR: backup_original\ocr_cli.py not found!
    pause
    exit /b 1
)

if not exist "backup_original\ocr_worker.py" (
    echo ERROR: backup_original\ocr_worker.py not found!
    pause
    exit /b 1
)

if not exist "backup_original\batch_ocr.py" (
    echo ERROR: backup_original\batch_ocr.py not found!
    pause
    exit /b 1
)

echo OK: All backup files found

echo.
echo 2. Restoring original modules...
copy /Y backup_original\ocr_cli.py ocr_cli.py
copy /Y backup_original\ocr_worker.py ocr_worker.py
copy /Y backup_original\batch_ocr.py batch_ocr.py

if errorlevel 1 (
    echo ERROR: Failed to restore modules
    pause
    exit /b 1
)

echo OK: Original modules restored

echo.
echo 3. Verifying restoration...
if exist "ocr_cli_legacy.py" del /F ocr_cli_legacy.py
if exist "ocr_worker_legacy.py" del /F ocr_worker_legacy.py
if exist "batch_ocr_legacy.py" del /F batch_ocr_legacy.py

echo OK: Cleanup completed

echo.
echo ============================================================
echo Rollback Completed Successfully!
echo ============================================================
echo.
echo The following modules have been restored:
echo - ocr_cli.py (original version)
echo - ocr_worker.py (original version)
echo - batch_ocr.py (original version)
echo.
echo Please restart the platform to apply changes.
echo.
pause