param(
    [Parameter(Mandatory = $true)]
    [string]$ActivationId,

    [int]$TimeoutSeconds = 90,
    [int]$IntervalSeconds = 3
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "config.json"

if (-not (Test-Path $ConfigPath)) {
    Write-Host "找不到 config.json"
    exit 1
}

$cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$apiKey = [string]$cfg.otp.sms_api.api_key
$baseUrl = ([string]$cfg.otp.sms_api.base_url).TrimEnd("/")
$proxy = [string]$cfg.proxy

if ([string]::IsNullOrWhiteSpace($apiKey)) {
    Write-Host "config.json 里缺少 otp.sms_api.api_key"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($baseUrl)) {
    $baseUrl = "https://hero-sms.com"
}

$url = "$baseUrl/stubs/handler_api.php?api_key=$apiKey&action=getStatus&id=$ActivationId"
$safeUrl = "$baseUrl/stubs/handler_api.php?api_key=***&action=getStatus&id=$ActivationId"

Write-Host "测试 HeroSMS activation id: $ActivationId"
Write-Host "URL: $safeUrl"
if ($proxy) {
    Write-Host "Proxy: $proxy"
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

while ((Get-Date) -lt $deadline) {
    try {
        $args = @("-s", "-S")
        if ($proxy) {
            $args += @("-x", $proxy)
        }
        $args += @($url)

        $body = & curl.exe @args
        $text = ($body | Out-String).Trim()
        $now = Get-Date -Format "HH:mm:ss"

        if ($LASTEXITCODE -ne 0) {
            Write-Host "[$now] curl 失败，exit=$LASTEXITCODE"
        } elseif ($text -match "STATUS_OK:(\d{4,8})") {
            Write-Host "[$now] 成功拿到验证码: $($Matches[1])"
            exit 0
        } else {
            Write-Host "[$now] 返回: $text"
        }
    } catch {
        $now = Get-Date -Format "HH:mm:ss"
        Write-Host "[$now] 请求失败: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds $IntervalSeconds
}

Write-Host "超时：$TimeoutSeconds 秒内没有拿到 STATUS_OK"
exit 2
