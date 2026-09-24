param(
    [string]$TaskName = 'cowrie',
    [string]$TaskPath = '\',
    [string]$UvExecutable = ''
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$stateDirectory = Join-Path $project 'state'
$resultPath = Join-Path $stateDirectory 'hidden-task-update.json'
New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null

try {
    if ([string]::IsNullOrWhiteSpace($UvExecutable)) {
        $UvExecutable = (Get-Command uv -CommandType Application -ErrorAction Stop).Source
    }
    if (-not (Test-Path -LiteralPath $UvExecutable -PathType Leaf)) {
        throw 'uv executable does not exist.'
    }
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
    if ([string]$task.Principal.LogonType -ne 'Interactive') {
        throw 'Expected Interactive logon; no changes applied.'
    }
    if ($task.Actions.Count -ne 1) {
        throw 'Expected a single task action; no changes applied.'
    }
    $before = [xml](Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath)
    $backup = Join-Path $stateDirectory ('task-before-hidden-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.xml')
    $before.Save($backup)
    $launcher = Join-Path $PSScriptRoot 'run_hidden.vbs'
    $arguments = '//B //Nologo "{0}" "{1}"' -f $launcher, $UvExecutable
    $action = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\wscript.exe') -Argument $arguments -WorkingDirectory $project
    Set-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action | Out-Null
    $after = [xml](Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath)
    foreach ($section in @('Principals', 'Triggers', 'Settings')) {
        if ($before.Task.$section.OuterXml -ne $after.Task.$section.OuterXml) {
            throw "Unexpected change in $section; inspect saved backup: $backup"
        }
    }
    [pscustomobject]@{ Success = $true; TaskName = $TaskName; LogonType = 'Interactive'; Backup = $backup } |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 0
}
catch {
    [pscustomobject]@{ Success = $false; Error = $_.Exception.Message } |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 1
}
