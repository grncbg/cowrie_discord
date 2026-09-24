Option Explicit

Dim shell, filesystem, project, executable, command, result
If WScript.Arguments.Count < 1 Or WScript.Arguments.Count > 2 Then WScript.Quit 2
If WScript.Arguments.Count = 2 Then
    If WScript.Arguments(1) <> "--dry-run" Then WScript.Quit 2
End If

Set shell = CreateObject("WScript.Shell")
Set filesystem = CreateObject("Scripting.FileSystemObject")
project = filesystem.GetParentFolderName(filesystem.GetParentFolderName(WScript.ScriptFullName))
executable = WScript.Arguments(0)
If Not filesystem.FileExists(executable) Then WScript.Quit 2
If InStr(executable, Chr(34)) > 0 Then WScript.Quit 2

shell.CurrentDirectory = project
command = Chr(34) & executable & Chr(34) & " run --locked cowrie_monitor.py"
If WScript.Arguments.Count = 2 Then command = command & " --dry-run"
' 0 hides the console; True waits so Task Scheduler receives uv's exit code.
result = shell.Run(command, 0, True)
WScript.Quit result
