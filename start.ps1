param(
    [switch]$NoStopExisting,
    [switch]$InstallDeps
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "=== GoPay Plus 自动订阅机 (Windows) ==="

if (-not (Test-Path (Join-Path $Root "config.json"))) {
    Write-Host "config.json 不存在，请先复制并编辑："
    Write-Host "  Copy-Item config.example.json config.json"
    exit 1
}

$PythonExe = $null
$PythonPrefixArgs = @()
$PythonCandidates = @(
    @{ Exe = "py"; Args = @("-3") },
    @{ Exe = "python"; Args = @() },
    @{ Exe = "python3"; Args = @() }
)

foreach ($candidate in $PythonCandidates) {
    if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) {
        continue
    }
    & $candidate.Exe @($candidate.Args) -c "import sys; print(sys.version)" *> $null
    if ($LASTEXITCODE -eq 0) {
        $PythonExe = $candidate.Exe
        $PythonPrefixArgs = @($candidate.Args)
        break
    }
}

if (-not $PythonExe) {
    Write-Host "未找到可用的 Python。请安装 Python 3.10+，并勾选 Add python.exe to PATH。"
    exit 1
}

& $PythonExe @PythonPrefixArgs -c "import curl_cffi, grpc" *> $null
if ($LASTEXITCODE -ne 0) {
    if ($InstallDeps) {
        Write-Host "当前 Python 环境缺少依赖，正在安装 requirements.txt..."
        & $PythonExe @PythonPrefixArgs -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            Write-Host "依赖安装失败，请检查 pip 网络或 Python 环境。"
            exit 1
        }
        & $PythonExe @PythonPrefixArgs -c "import curl_cffi, grpc" *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "依赖安装后仍无法导入 curl_cffi/grpc，请检查当前 Python 环境。"
            exit 1
        }
    } else {
        Write-Host "当前 Python 环境缺少依赖。请执行："
        Write-Host "  $PythonExe $($PythonPrefixArgs -join ' ') -m pip install -r requirements.txt"
        Write-Host "或直接运行："
        Write-Host "  .\start.ps1 -InstallDeps"
        exit 1
    }
}

if (-not $NoStopExisting) {
    $stoppedByCim = $false
    try {
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.CommandLine -and (
                    $_.CommandLine -match "payment_server\.py" -or
                    $_.CommandLine -match "orchestrator\.py" -or
                    $_.CommandLine -match "to_whatsapp.*index\.js"
                )
            } |
            ForEach-Object {
                try {
                    Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                    $stoppedByCim = $true
                } catch {}
            }
    } catch {
        Write-Host "无法自动检查/停止旧进程，可能是当前 PowerShell 权限不足；如端口被占用，请手动关闭旧的 python/node 进程。"
    }

    $portPids = @()
    foreach ($port in @(8800, 50051, 50056)) {
        $lines = netstat -ano | Select-String ":$port\s+.*LISTENING\s+(\d+)"
        foreach ($line in $lines) {
            $pidText = $line.Matches[0].Groups[1].Value
            if ($pidText) {
                $portPids += [int]$pidText
            }
        }
    }
    $portPids |
        Sort-Object -Unique |
        Where-Object { $_ -gt 0 -and $_ -ne $PID } |
        ForEach-Object {
            try {
                Stop-Process -Id $_ -Force -ErrorAction Stop
                $stoppedByCim = $true
            } catch {}
        }

    if ($stoppedByCim) {
        Start-Sleep -Seconds 1
    }
}

$logsDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$paymentArgs = @(
    "payment_server.py",
    "--config", "$Root/config.json",
    "--listen", ":50051"
)
Write-Host "-> 启动 plus_gopay_links (gRPC :50051)..."
$payment = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList ($PythonPrefixArgs + $paymentArgs) `
    -WorkingDirectory (Join-Path $Root "plus_gopay_links") `
    -RedirectStandardOutput (Join-Path $logsDir "payment_server.out.log") `
    -RedirectStandardError (Join-Path $logsDir "payment_server.err.log") `
    -PassThru `
    -WindowStyle Hidden

Start-Sleep -Seconds 2

Write-Host "-> 启动 orchestrator (:8800)..."
$orchestrator = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList ($PythonPrefixArgs + @("orchestrator.py")) `
    -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $logsDir "orchestrator.out.log") `
    -RedirectStandardError (Join-Path $logsDir "orchestrator.err.log") `
    -PassThru `
    -WindowStyle Hidden

Start-Sleep -Seconds 2

if ($payment.HasExited) {
    Write-Host "payment_server 启动后已退出，请查看：$logsDir\payment_server.err.log"
    exit 1
}

if ($orchestrator.HasExited) {
    Write-Host "orchestrator 启动后已退出，请查看：$logsDir\orchestrator.err.log"
    exit 1
}

$config = Get-Content (Join-Path $Root "config.json") -Raw | ConvertFrom-Json
$otpMode = if ($config.otp -and $config.otp.mode) { [string]$config.otp.mode } else { "manual" }
$wa = $null

if ($otpMode -eq "whatsapp") {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-Host "当前配置为 whatsapp 模式，但未找到 Node.js。请安装 Node.js 18+。"
        exit 1
    }

    $waDir = Join-Path $Root "to_whatsapp"
    if (-not (Test-Path (Join-Path $waDir "node_modules"))) {
        Write-Host "-> 安装 WhatsApp Relay 依赖..."
        Push-Location $waDir
        npm install --production
        Pop-Location
    }

    Write-Host "-> 启动 to_whatsapp (gRPC :50056)..."
    $wa = Start-Process `
        -FilePath "node" `
        -ArgumentList @("index.js") `
        -WorkingDirectory $waDir `
        -RedirectStandardOutput (Join-Path $logsDir "to_whatsapp.out.log") `
        -RedirectStandardError (Join-Path $logsDir "to_whatsapp.err.log") `
        -PassThru `
        -WindowStyle Hidden
}

Write-Host ""
Write-Host "已启动："
Write-Host "  payment_server PID: $($payment.Id)"
Write-Host "  orchestrator PID:   $($orchestrator.Id)"
if ($wa) {
    Write-Host "  to_whatsapp PID:    $($wa.Id)"
}
Write-Host ""
Write-Host "健康检查："
Write-Host "  Invoke-RestMethod http://localhost:8800/health"
try {
    $health = Invoke-RestMethod http://localhost:8800/health -TimeoutSec 5
    Write-Host "  当前状态: ok=$($health.ok), otp_mode=$($health.otp_mode)"
} catch {
    Write-Host "  当前状态: 健康检查暂未连通，请稍后重试或查看日志。"
}
Write-Host ""
Write-Host "日志目录："
Write-Host "  $logsDir"
Write-Host ""
Write-Host "停止服务："
Write-Host "  foreach (`$port in 8800,50051,50056) { netstat -ano | Select-String "":`$port\s+.*LISTENING\s+(\d+)"" | % { Stop-Process -Id ([int]`$_.Matches[0].Groups[1].Value) -Force } }"
