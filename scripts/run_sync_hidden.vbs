' run_sync_hidden.vbs -- windowless launcher for the Aether\SyncHaIp scheduled task.
' Run style 0 hides the bash console; wscript.exe is a GUI host, so no console is
' allocated at all. Keep this file ASCII-only (wscript parses it as ANSI/GBK).
Option Explicit
Dim sh, fso, root
Set sh = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run """C:\Program Files\Git\bin\bash.exe"" -l """ & root & "\scripts\sync_job.sh""", 0, True
