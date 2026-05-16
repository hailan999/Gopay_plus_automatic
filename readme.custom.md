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

- `session_token`：可以填 ChatGPT `accessToken`，也可以填 Web cookie 里的 `__Secure-next-auth.session-token` 值
- `phone_number`：GoPay 手机号，不含国家码 `62`
- `pin`：GoPay PIN
- `sms_activation_id`：HeroSMS activation id
- `Bearer 123`：`config.json` 里的 `orchestrator.auth_token`

```powershell
$body = @{
  session_token      = "你的ChatGPT accessToken 或 __Secure-next-auth.session-token 的值"
  phone_number       = "85836075711"
  pin                = "你的GoPay PIN"
  sms_activation_id  = "378456663"
  registered_account_id = "registered_accounts 表里的 id，可选但推荐"
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

如果 `session_token` 来自 `output/webui.db` 的 `registered_accounts` 表，建议同时传 `registered_account_id` 或 `email`。支付结束后程序会自动把状态写回：

- 成功且有 `refresh_token`：`SUCCESS`
- 成功但没有 `refresh_token`：`UN_OAUTHED`
- 错误文本包含 `ADD_PHONE`：`ADD_PHONE`
- 错误文本包含 `NO_TRIAL` / due 异常：`NO_TRIAL`
- 其他失败：`FAILED`

如果数据库里当前状态已经是 `ADD_PHONE`，即使支付侧返回成功，也不会被覆盖成 `SUCCESS` / `UN_OAUTHED`。

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

说明 `session_token` 不是有效的 ChatGPT `accessToken` / Web session token，或者还是占位文本。

如果使用 `accessToken`，重新登录 ChatGPT 后访问：

```text
https://chatgpt.com/api/auth/session
```

复制里面的 `accessToken`。

如果使用 Web session token，复制浏览器 Cookie 里的：

```text
__Secure-next-auth.session-token
```

程序会自动用它请求 `/api/auth/session` 刷新出新的 `accessToken`，再继续后续流程。

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

`gopay_register_adb.py` 是独立的 GoPay 注册脚本。它通过 ADB 操作雷电模拟器里的 GoPay App，按 `gopay-steps/1.png` 到 `19.png` 的页面顺序完成：输入手机号、选择 SMS OTP、填写姓名、设置 6 位 PIN。它不调用 ChatGPT 订阅接口。首次打开 GoPay 时如果先出现定位说明页，脚本会点击 `Nanti aja` 跳过定位授权，再继续后面的语言/手机号流程。

PIN 设置成功后，脚本会读取 `config.json` 里的 `gopay.get_rp_link`，用模拟器自带浏览器打开该链接。这个打开方式不是普通输入地址栏，而是直接指定 Android 浏览器组件打开完整 URL，可以避免长链接里的 `&` 被截断。链接跳回 GoPay 红包页后，脚本会等待并点击 `Open gift`。

前提：

- 雷电模拟器已启动
- GoPay App 已安装
- 已在 HeroSMS 买好印尼号，并拿到 `activation id`
- `config.json` 里已有 HeroSMS API key
- 如需注册后自动打开 RP/红包链接，配置 `gopay.get_rp_link`

### 9.1 准备新模拟器

`gopay_prepare_emulator.py` 用来做注册前置准备：新建/复用一个雷电模拟器，设置分辨率 `1080x1920x480`，安装 `MT2.26.4.apk`，把 `GoPay_2.7.0.apks` 放到 `/sdcard/Pictures/`，并把 `.apks` 里的 split APK 直接通过 `adb install-multiple` 安装好。这样不需要在 MT 管理器里手动点安装。

新建一个模拟器并准备：

```powershell
.\.venv\Scripts\python.exe .\gopay_prepare_emulator.py --create --name gopay-auto-1 --open-mt
```

复用已有模拟器 index：

```powershell
.\.venv\Scripts\python.exe .\gopay_prepare_emulator.py --index 9 --open-mt
```

默认文件路径：

- MT 管理器：`C:\Users\Administrator\Downloads\MT2.26.4.apk`
- GoPay APKS：`C:\Users\Administrator\Downloads\GoPay_2.7.0.apks`
- 雷电目录：`E:\leidian\LDPlayer9`

如果文件放在别处，用 `--mt-apk` / `--gopay-apks` 指定。

相关配置示例：

```json
{
  "gopay": {
    "phone_number": "85836075711",
    "pin": "123456",
    "name": "smith",
    "get_rp_link": "https://app.gopay.co.id/...",
    "auto_buy_number": true,
    "post_gift_subscribe": {
      "enabled": true,
      "url": "http://localhost:8800/subscribe",
      "session_token": "sample",
      "auth_token": "你的 orchestrator auth_token"
    },
    "account_claim": {
      "db_path": "",
      "target_email": ""
    }
  },
  "otp": {
    "sms_api": {
      "service": "gopay",
      "country_id": "6",
      "maxPrice": 0.05,
      "fixedPrice": "true"
    }
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

如果不传手机号和 activation id，可以让脚本先向 HeroSMS 买号，再继续后面的注册流程。买号请求会带 `maxPrice=0.05`：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py --auto-buy-number --pin 123456 --name smith
```

如果 `config.json` 已经配置好 `gopay.auto_buy_number=true`、`gopay.pin`、`otp.sms_api.service=ni`、`otp.sms_api.country_id=6`、`otp.sms_api.maxPrice=0.05`，可以直接运行：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py
```

默认情况下，注册脚本会先调用 `gopay_prepare_emulator.py`，准备/启动 `gopay-auto-1` 模拟器，并把返回的 ADB device 用于后续注册。这样不会随机选中其他已经打开的模拟器。如果不想跑前置准备，用：

如果 `gopay-auto-1` 已存在且配置里 `unique_name=true`，脚本会自动顺延成 `gopay-auto-2`、`gopay-auto-3`，不会创建重名实例。显式传 `--prepare-index` 时会复用指定 index。

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py --skip-prepare-emulator
```

也可以指定前置模拟器：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py --prepare-index 10
```

`--name` 不传且 `gopay.name` 没有固定值时，脚本会自动随机生成英文姓名；如果想固定姓名，在命令行传 `--name jaime`，或者在 `config.json` 写 `"name": "jaime"`。

单独测试 SQLite 账号领取逻辑，不碰 GoPay、不买号、不改真实库。脚本会复制一份 DB 到 `logs\webui_claim_test.db`，然后在副本上测试 `INITIAL -> PROCESSING`：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py --test-claim-account
```

如需指定邮箱：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py --test-claim-account --claim-account-email "user@example.com"
```

单独测试最后一步 `/subscribe` 请求。这个会复制一份 DB 到 `logs\webui_subscribe_test.db` 后从副本领取账号，再请求本机 orchestrator；不会改真实 DB：

```powershell
.\.venv\Scripts\python.exe .\gopay_register_adb.py --test-post-gift-subscribe --phone-number 85729659763 --pin 211314 --sms-activation-id 381423394
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

# 设置 PIN 后的 OTP 长时间没新码时，默认 60 秒点一次 Resend；也可以手动调短/调长
.\.venv\Scripts\python.exe .\gopay_register_adb.py --sms-activation-id 378456663 --pin-otp-resend-after 45

# 第一次手机号注册 OTP 长时间没新码时，默认 60 秒点一次 Resend；也可以手动调短/调长
.\.venv\Scripts\python.exe .\gopay_register_adb.py --sms-activation-id 378456663 --otp-resend-after 45
```

HeroSMS 相关手动操作：

```powershell
# 请求 HeroSMS 新增一条短信。不会在主流程里自动调用。
.\.venv\Scripts\python.exe .\gopay_register_adb.py --request-extra-sms --sms-activation-id 378456663

# 手动调 HeroSMS setStatus status=3。
.\.venv\Scripts\python.exe .\gopay_register_adb.py --request-retry-status --sms-activation-id 378456663
```

主流程每次拿到一个新的 `STATUS_OK:验证码` 后，会自动调用一次 HeroSMS `setStatus status=3`，用于让当前 activation 准备接收下一条短信。这个动作不会把 activation 标记完成，也不会取消 activation；如果 `setStatus=3` 失败，只记录 warning，当前验证码仍会继续输入。

第一次手机号注册 OTP、以及设置 PIN 后进入第二次 SMS OTP 页面时，脚本默认等待 60 秒还没有新验证码就会点击 GoPay 页面上的 `Resend`，并继续轮询 HeroSMS。可以分别用 `--otp-resend-after` 和 `--pin-otp-resend-after` 调整。

注册 OTP 的总等待时间是 `--otp-timeout`。如果命令行不传，脚本会优先读取 `gopay.otp_timeout`，其次读取 `otp.sms_api.poll_timeout_sec`，再其次读取 `orchestrator.otp_timeout`，最后默认 180 秒。

红包页点击 `Open gift` 成功后，脚本会调用 `gopay.post_gift_subscribe.url`，默认是本机 `http://localhost:8800/subscribe`。主注册流程只负责把请求发出去，不等待支付结果；该请求失败只记录 warning，不会回滚注册结果。单独运行 `--test-post-gift-subscribe` 时仍会等待响应，方便调试服务返回。

实际支付用的 ChatGPT 登录凭证会从 SQLite 里领取，默认数据库是 `E:\development\git_projects\Gpt-Agreement-Payment\output\webui.db`；如果设置了环境变量 `WEBUI_DATA_DIR`，则使用 `%WEBUI_DATA_DIR%\webui.db`。领取逻辑会在同一个 SQLite 写事务里把 `registered_accounts.status` 从 `INITIAL` 改成 `PROCESSING`，并跳过已经支付成功或报过 `User is already paid` 的邮箱。领取到的 `session_token/access_token/device_id/cookie_header` 会传给本机 `/subscribe`；`proxy_add` 只用于日志判断，不会随注册脚本的 `/subscribe` 请求传入。支付链路是否走代理由支付服务自己的 `config.json` 里的 `proxy` 决定。如果没有可领取账号，不会用 sample token 强行发起支付。

支付链路的 HTTP 请求默认会对临时网络问题自动重试 3 次，包括代理断开、超时、以及 `429/502/503/504/520/522/524`。可以在 `config.json` 的 `gopay` 里调整：

```json
"http_retry_limit": 3,
"http_retry_base_sleep_s": 2,
"chatgpt_checkout_timeout_s": 45
```

如果日志里是 `chatgpt checkout create` 连续 `curl: (28)`，通常是代理或 ChatGPT 边缘节点短暂无响应。可以优先把 `http_retry_limit` 调到 `5`，并把 `chatgpt_checkout_timeout_s` 调到 `60`。

HeroSMS 的买号、查码、setStatus 请求也会对临时网络问题自动重试 3 次，包括 `curl: (35)` TLS 握手失败。可以在 `config.json` 的 `otp.sms_api` 里调整同名参数。

状态文件和日志：

- `logs\gopay_register_steps\`：每一步截图和页面 XML，失败时优先看这里。
- `logs\gopay_register_adb.log`：注册脚本日志。
- `logs\gopay_number_usage.json`：号码使用总账。每轮注册开始会记录 `started`，后续会追加 `registered_success`、`otp_timeout`、`phone_already_registered`、`failed`、`interrupted` 等状态。
- `logs\herosms_used_otps.json`：记录已经用过的 OTP，避免二次验证码误用旧码。
- `logs\gopay_pin_setup.json`：记录哪些手机号已经成功设置 PIN。
- `logs\gopay_unusable_numbers.json`：记录不能继续注册的号码，比如输入手机号后出现 `Other ways to log in`，或直接要求 `Enter your GoPay PIN to log in`，说明该号码已经注册过。
- `logs\gopay_device_exceptions.json`：记录设备级异常，比如出现 `Try logging in after 12 hours`，说明当前模拟器/设备需要更换。
- RP 链接打开后如果没有看到 `Open gift`，脚本只记录 warning，不会自动取消 HeroSMS activation。

说明：

- `--phone-number` 可以填 `858...`、`0858...` 或 `62858...`，脚本会自动转成 GoPay 需要的本地号。
- 默认 ADB 路径是 `E:\leidian\LDPlayer9\adb.exe`，如果雷电装在别处，用 `--adb-path` 指定。
- 脚本会自动尝试连接 `127.0.0.1:5555/5557/5559/5561/7555`。
- 脚本不会自动把 HeroSMS activation 标记完成，也不会自动取消 activation。
- PowerShell 多行命令里的反引号 `` ` `` 必须放在行尾，后面不能有空格；不确定时可以把命令写成一行。

## 10. 批量持续注册

`gopay_batch_register.py` 会启动固定数量 worker。每个 worker 每一轮都会创建独立雷电实例、准备环境、调用 `gopay_register_adb.py --skip-prepare-emulator --device ...` 注册，结束后删除本轮模拟器，等待一段时间再继续下一轮。

先小规模跑 2 个 worker，各跑 1 轮：

```powershell
.\.venv\Scripts\python.exe .\gopay_batch_register.py --workers 2
```

持续跑 10 个 worker，每轮结束后等 10 秒：

```powershell
.\.venv\Scripts\python.exe .\gopay_batch_register.py --workers 10 --loop --delay-between-runs 10
```

每个 worker 跑 5 轮：

```powershell
.\.venv\Scripts\python.exe .\gopay_batch_register.py --workers 10 --runs-per-worker 5 --delay-between-runs 10
```

失败时保留模拟器方便排查：

```powershell
.\.venv\Scripts\python.exe .\gopay_batch_register.py --workers 2 --keep-failed
```

每轮结束后先等 5 秒，让你决定是否保留当前模拟器窗口。看到提示后按 `K` 会保留；不按会继续关闭并删除：

```powershell
.\.venv\Scripts\python.exe .\gopay_batch_register.py --workers 2 --confirm-keep-window
```

也可以改等待时间：

```powershell
.\.venv\Scripts\python.exe .\gopay_batch_register.py --workers 2 --confirm-keep-window --confirm-keep-timeout 10
```

停止持续模式：新建这个文件，worker 会在当前轮结束清理后停止：

```powershell
New-Item .\logs\batch_register\stop_batch.txt -ItemType File -Force
```

下次重新启动批量任务时，脚本会自动清理上一次留下的 `stop_batch.txt`。如果确实想保留“有停止文件就不启动”的旧行为，加 `--honor-existing-stop-file`。

日志位置：

- 总日志：`logs\batch_register\batch.log`
- 每轮日志：`logs\batch_register\worker-01\w01_r00001_...\`
- 每轮截图/XML：对应轮目录下的 `steps\`

TODO：

- 注册流程结束并打开 `gopay.get_rp_link` 后，再调用一个后续接口，自动进入下一步业务流程。
