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

## 9. ADB 自动注册 GoPay 账号

`gopay_register_adb.py` 是独立的 GoPay 注册脚本。它通过 ADB 操作雷电模拟器里的 GoPay App，按 `gopay-steps/1.png` 到 `19.png` 的页面顺序完成：输入手机号、选择 SMS OTP、填写姓名、设置 6 位 PIN。它不调用 ChatGPT 订阅接口。

PIN 设置成功后，脚本会读取 `config.json` 里的 `gopay.get_rp_link`，用模拟器自带浏览器打开该链接。这个打开方式不是普通输入地址栏，而是直接指定 Android 浏览器组件打开完整 URL，可以避免长链接里的 `&` 被截断。

前提：

- 雷电模拟器已启动
- GoPay App 已安装
- 已在 HeroSMS 买好印尼号，并拿到 `activation id`
- `config.json` 里已有 HeroSMS API key
- 如需注册后自动打开 RP/红包链接，配置 `gopay.get_rp_link`

相关配置示例：

```json
{
  "gopay": {
    "phone_number": "85836075711",
    "pin": "123456",
    "name": "smith",
    "get_rp_link": "https://app.gopay.co.id/..."
  }
}
```

完整注册：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py `
  --phone-number 85836075711 `
  --sms-activation-id 378456663 `
  --pin 123456 `
  --name smith
```

也可以把 `phone_number`、`pin`、`name`、`get_rp_link` 放到 `config.json`，命令行只传 activation id：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py --sms-activation-id 378456663
```

常用测试入口：

```powershell
# 干跑：只连接模拟器、启动 GoPay、截图并识别当前页面，不输入手机号和 PIN
.\.venv\Scripts\python.exe .\gopay_register_adb.py --dry-run

# 单独测试 HeroSMS 是否已经收到 OTP
.\.venv\Scripts\python.exe .\gopay_register_adb.py --test-otp --sms-activation-id 378456663

# 测试下一条新 OTP：会跳过 logs\herosms_used_otps.json 里记录过的旧验证码
.\.venv\Scripts\python.exe .\gopay_register_adb.py --test-next-otp --sms-activation-id 378456663

# 单独测试打开 config 或命令行传入的 RP 链接
.\.venv\Scripts\python.exe .\gopay_register_adb.py --open-rp-link-only --get-rp-link "https://完整链接"

# 当前手机号已经记录为 PIN 设置完成，但实际没有设置好时，强制重新走 PIN 流程
.\.venv\Scripts\python.exe .\gopay_register_adb.py --sms-activation-id 378456663 --force-pin-setup
```

HeroSMS 相关手动操作：

```powershell
# 请求 HeroSMS 新增一条短信。不会在主流程里自动调用。
.\.venv\Scripts\python.exe .\gopay_register_adb.py --request-extra-sms --sms-activation-id 378456663

# 调 HeroSMS setStatus status=3。不会在主流程里自动调用。
.\.venv\Scripts\python.exe .\gopay_register_adb.py --request-retry-status --sms-activation-id 378456663
```

状态文件和日志：

- `logs\gopay_register_steps\`：每一步截图和页面 XML，失败时优先看这里。
- `logs\gopay_register_adb.log`：注册脚本日志。
- `logs\herosms_used_otps.json`：记录已经用过的 OTP，避免二次验证码误用旧码。
- `logs\gopay_pin_setup.json`：记录哪些手机号已经成功设置 PIN。

说明：

- `--phone-number` 可以填 `858...`、`0858...` 或 `62858...`，脚本会自动转成 GoPay 需要的本地号。
- 默认 ADB 路径是 `E:\leidian\LDPlayer9\adb.exe`，如果雷电装在别处，用 `--adb-path` 指定。
- 脚本会自动尝试连接 `127.0.0.1:5555/5557/5559/5561/7555`。
- 脚本不会自动把 HeroSMS activation 标记完成，也不会自动取消 activation。
- PowerShell 多行命令里的反引号 `` ` `` 必须放在行尾，后面不能有空格；不确定时可以把命令写成一行。
