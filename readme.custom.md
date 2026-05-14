# Windows 使用说明（SMS / HeroSMS 模式）

这份说明用于在 Windows PowerShell 下启动、停止和测试本项目。当前方案使用 SMS 接码，不使用 WhatsApp。

## 1. 准备环境

需要安装：

- Python 3.10+
- PowerShell
- 可用代理
- HeroSMS API key
- 已手动注册好的 GoPay/Gojek 账号：手机号 + PIN
- ChatGPT `accessToken`

第一次使用时，在项目根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\start.ps1 -InstallDeps
```

之后正常启动只需要：

```powershell
.\start.ps1
```

启动成功后会显示健康检查结果。也可以手动检查：

```powershell
Invoke-RestMethod http://localhost:8800/health
```

正常应看到 `otp_mode` 为：

```text
sms_api
```

## 2. 停止服务

停止后台服务：

```powershell
.\stop.ps1
```

它会停止这些端口上的服务：

- `8800`：orchestrator HTTP 服务
- `50051`：payment gRPC 服务
- `50056`：WhatsApp Relay，当前 SMS 模式一般不会用到

## 3. HeroSMS Activation ID

手动在 HeroSMS 网站买号后，需要拿到该订单的 activation id。

如果 HeroSMS 页面/接口返回类似：

```json
{
  "data": [
    {
      "id": 378456663,
      "phone": 6285836075711,
      "smsCode": "",
      "otpList": []
    }
  ]
}
```

那么：

- `sms_activation_id` 是 `378456663`
- `phone_number` 填给本项目时通常去掉国家码 `62`，即 `85836075711`

## 4. 单独测试 HeroSMS 是否能取码

只测试 HeroSMS，不触发 GoPay，也不调用 OpenAI：

```powershell
.\test_sms_activation.ps1 -ActivationId 378456663
```

等待更久一点：

```powershell
.\test_sms_activation.ps1 -ActivationId 378456663 -TimeoutSeconds 180 -IntervalSeconds 3
```

常见返回：

```text
STATUS_WAIT_CODE
```

表示接口通了，但短信还没有到。

```text
STATUS_OK:123456
```

表示已经拿到验证码。

## 5. 发起订阅请求

把下面内容里的值换成自己的：

- `session_token`：ChatGPT `accessToken`
- `phone_number`：GoPay 手机号，不含国家码 `62`
- `pin`：GoPay PIN
- `sms_activation_id`：HeroSMS activation id
- `Bearer 123`：`config.json` 里的 `orchestrator.auth_token`

```powershell
$body = @{
  session_token      = "你的ChatGPT accessToken"
  phone_number       = "85836075711"
  pin                = "你的GoPay PIN"
  sms_activation_id  = "378456663"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://localhost:8800/subscribe" `
  -Method Post `
  -ContentType "application/json" `
  -Headers @{ Authorization = "Bearer 123" } `
  -Body $body
```

成功时会返回类似：

```json
{
  "ok": true,
  "charge_ref": "A1xxxxxxxxxxxxxxxxxxxx",
  "elapsed_ms": 20000
}
```

失败时会返回 `ok: false`，看 `error` 和 `detail`。

## 6. 查看日志

编排器日志：

```powershell
Get-Content .\logs\orchestrator.err.log -Tail 120
```

支付核心日志：

```powershell
Get-Content .\logs\payment_server.err.log -Tail 120
```

如果看到：

```text
switched OTP delivery to SMS
```

说明 GoPay 已经成功切到 SMS。

如果看到：

```text
STATUS_WAIT_CODE
```

说明 HeroSMS 还没收到验证码。

如果看到：

```text
STATUS_OK:123456
```

说明验证码已经拿到，程序会继续提交 OTP。

## 7. 常见问题

### 返回 unauthorized

说明请求头里的 Bearer token 和 `config.json` 里的 `orchestrator.auth_token` 不一致。

检查：

```powershell
Get-Content .\config.json
```

然后确保请求里是：

```powershell
-Headers @{ Authorization = "Bearer 你的auth_token" }
```

### 返回 bad_token

说明 `session_token` 不是有效的 ChatGPT `accessToken`，或者还是占位文本。

重新登录 ChatGPT 后访问：

```text
https://chatgpt.com/api/auth/session
```

复制里面的 `accessToken`。

### start.ps1 显示启动了，但 health 不通

先停止旧进程再启动：

```powershell
.\stop.ps1
.\start.ps1
```

再看日志：

```powershell
Get-Content .\logs\orchestrator.err.log -Tail 120
```

### HeroSMS 一直没有验证码

先确认：

- `sms_activation_id` 是 HeroSMS 返回里的 `id`
- `phone_number` 去掉了国家码 `62`
- HeroSMS 页面里该订单没有过期
- GoPay 日志里出现了 `switched OTP delivery to SMS`

## 8. 重要提醒

不要把下面内容发给别人或贴到公开地方：

- ChatGPT `accessToken`
- GoPay PIN
- HeroSMS API key
- 代理账号密码
- `config.json` 完整内容
