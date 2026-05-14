$ErrorActionPreference = "Continue"

$ports = @(8800, 50051, 50056)
$pids = @()

foreach ($port in $ports) {
    $lines = netstat -ano | Select-String ":$port\s+.*LISTENING\s+(\d+)"
    foreach ($line in $lines) {
        $pidText = $line.Matches[0].Groups[1].Value
        if ($pidText) {
            $pids += [int]$pidText
        }
    }
}

$pids = $pids | Sort-Object -Unique

if (-not $pids -or $pids.Count -eq 0) {
    Write-Host "没有发现 8800 / 50051 / 50056 上的监听服务。"
    exit 0
}

foreach ($processId in $pids) {
    try {
        Stop-Process -Id $processId -Force -ErrorAction Stop
        Write-Host "已停止 PID $processId"
    } catch {
        Write-Host "停止 PID $processId 失败：$($_.Exception.Message)"
    }
}
