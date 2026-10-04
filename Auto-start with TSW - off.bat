@echo off
rem Stops the TSW7 Joystick Bridge window opening by itself when the game starts.
python "%~dp0tsw_autostart.py" --uninstall
pause
