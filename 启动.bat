@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ============================================================
echo   OKX TradFi 分型策略监控系统 - 快速启动
echo ============================================================
echo.
echo   [1] 跑回测（查看信号质量）
echo   [2] 测试飞书连通
echo   [3] 实时监控（持续运行）
echo   [4] 单轮扫描（调试）
echo.
set /p choice="请选择 (1-4): "

if "%choice%"=="1" (
    echo.
    echo 正在运行回测...
    python -u scripts\run_real_backtest.py
    pause
)

if "%choice%"=="2" (
    echo.
    echo 正在测试飞书连通性...
    python -u scripts\test_feishu.py
    pause
)

if "%choice%"=="3" (
    echo.
    echo 启动实时监控，按 Ctrl+C 停止...
    python -u monitor.py
)

if "%choice%"=="4" (
    echo.
    echo 只扫一轮...
    python -u monitor.py --once
    pause
)
