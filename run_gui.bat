@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d %~dp0
echo 正在启动 QiMind 棋思图形界面...
python -m qimind gui %*
