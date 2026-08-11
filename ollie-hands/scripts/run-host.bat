@echo off
rem ollie-hands launcher — runs the Python supervisor (single-instance via an
rem exclusive sentinel port + respawn-on-crash) under the venv, in session 1
rem (interactive token) so screen capture + UIA see the real desktop. Duplicate
rem task launches are harmless: the extra supervisor fails to bind the sentinel
rem and exits. Run via the OllieHands scheduled task.
rem
rem stdout/stderr split: engine (subprocess) stdout goes to server.log as
rem before; supervisor's OWN stderr (uncaught exceptions before _entrypoint's
rem catch-all, e.g. venv python missing, import error) goes to supervisor.log
rem so the next boot crash is debuggable instead of silently overwriting
rem server.log.
cd /d C:\ollie-hands
venv\Scripts\python.exe scripts\supervisor.py 1>> C:\ProgramData\ollie-hands\server.log 2>> C:\ProgramData\ollie-hands\supervisor.log
