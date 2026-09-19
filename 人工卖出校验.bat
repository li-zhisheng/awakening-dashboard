@echo off
rem 人工卖出对账按钮: 人工卖出后双击, 校验当日成交并确认同步系统持仓
cd /d %~dp0
python main.py manual_audit --side sell
pause
