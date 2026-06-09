@echo off
chcp 65001 >nul
cd /d D:\codesoice-input
python test_e2e.py > test_output.txt 2>&1
type test_output.txt
