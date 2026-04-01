' Launch a .ps1 with no visible window (style 0). Used so Ansible checks do not
' flash conhost on Win11 — powershell.exe is a console app and flashes even with
' -WindowStyle Hidden when started directly from Ansible win_shell.
Option Explicit
Dim sh, ps1, cmd
If WScript.Arguments.Count < 1 Then WScript.Quit 1
ps1 = WScript.Arguments(0)
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & ps1 & """"
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run(cmd, 0, True)
