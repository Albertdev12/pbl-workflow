@echo off
rem 注册Windows计划任务：交易日2次（14:00盘中风控 + 16:00收盘全流程）+ 周六周报
chcp 65001 >nul
set WORKDIR=%~dp0
set P=cmd /c cd /d %WORKDIR% ^&^& python main.py

schtasks /Create /F /TN "模拟炒股工作流_1_盘中风险监控" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 14:00 /TR "%P% riskwatch >> run.log 2>&1"
schtasks /Create /F /TN "模拟炒股工作流_2_收盘全流程"   /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:00 /TR "%P% eod >> run.log 2>&1"
schtasks /Create /F /TN "模拟炒股工作流_3_周六周报"     /SC WEEKLY /D SAT /ST 10:00 /TR "%P% weekly >> run.log 2>&1"

echo.
echo 已注册 3 个计划任务（交易日14:00风控、16:00全流程，周六10:00周报），日志: %WORKDIR%run.log
echo 卸载请运行 uninstall_scheduler.bat
pause
