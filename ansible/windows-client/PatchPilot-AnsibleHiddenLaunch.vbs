' Launch a .ps1 with no visible window (style 0). Extra args go to PowerShell after -File (e.g. Check / Apply).
Option Explicit
Dim sh, ps1, cmd, i
If WScript.Arguments.Count < 1 Then WScript.Quit 1
ps1 = WScript.Arguments(0)
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & ps1 & """"
For i = 1 To WScript.Arguments.Count - 1
  cmd = cmd & " " & WScript.Arguments(i)
Next
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run(cmd, 0, True)
