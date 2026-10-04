@echo off
rem Opens the TSW7 Joystick Bridge window by itself whenever the game starts (and closes it when the game exits).
python "%~dp0tsw_autostart.py" --install
pause
