Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Python\pythoncore-3.14-64\pythonw.exe"

' Keep the desktop shortcut icon aligned with the local application.
desktopDir = shell.SpecialFolders("Desktop")
shortcutPath = desktopDir & "\基金买点观察.lnk"
On Error Resume Next
Set appShortcut = shell.CreateShortcut(shortcutPath)
appShortcut.TargetPath = shell.ExpandEnvironmentStrings("%WINDIR%") & "\System32\wscript.exe"
appShortcut.Arguments = Chr(34) & WScript.ScriptFullName & Chr(34)
appShortcut.WorkingDirectory = appDir
appShortcut.Description = "启动基金买点观察本地应用"
appShortcut.IconLocation = shell.ExpandEnvironmentStrings("%WINDIR%") & "\System32\imageres.dll,-102"
appShortcut.Save
Err.Clear
On Error GoTo 0

Function DashboardIsRunning()
  On Error Resume Next
  Set request = CreateObject("WinHttp.WinHttpRequest.5.1")
  request.SetTimeouts 800, 800, 800, 800
  request.Open "GET", "http://127.0.0.1:8767/api/portfolio", False
  request.Send
  DashboardIsRunning = (Err.Number = 0 And request.Status = 200)
  Err.Clear
  On Error GoTo 0
End Function

Function FindChrome()
  candidates = Array( _
    "C:\Program Files\Google\Chrome\Application\chrome.exe", _
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
  FindChrome = ""
  For Each candidate In candidates
    If fso.FileExists(candidate) Then
      FindChrome = candidate
      Exit Function
    End If
  Next
End Function

Sub OpenDashboardWindow()
  chrome = FindChrome()
  If chrome <> "" Then
    profileDir = appDir & "\reports\.chrome_dashboard_profile"
    shell.Run Chr(34) & chrome & Chr(34) & _
      " " & Chr(34) & "--user-data-dir=" & profileDir & Chr(34) & _
      " --no-first-run --no-default-browser-check --new-window --app=http://127.0.0.1:8767/latest_dashboard.html", 0, False
  Else
    shell.Run "http://127.0.0.1:8767/latest_dashboard.html", 1, False
  End If
End Sub

On Error Resume Next
ifindToken = shell.RegRead("HKCU\Environment\FUND_MONITOR_IFIND_AUTH_TOKEN")
If Err.Number = 0 Then shell.Environment("PROCESS")("FUND_MONITOR_IFIND_AUTH_TOKEN") = ifindToken
Err.Clear
investodayKey = shell.RegRead("HKCU\Environment\FUND_MONITOR_INVESTODAY_MCP_API_KEY")
If Err.Number = 0 Then shell.Environment("PROCESS")("FUND_MONITOR_INVESTODAY_MCP_API_KEY") = investodayKey
Err.Clear
On Error GoTo 0

shell.CurrentDirectory = appDir
If DashboardIsRunning() Then
  OpenDashboardWindow
Else
  shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & appDir & "\app.py" & Chr(34) & " --desktop", 0, False
End If
