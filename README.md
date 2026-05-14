# GoPay Plus 自动订阅机

[![GitHub](https://img.shields.io/badge/GitHub-Gopay__plus__automatic-blue?logo=github)](https://github.com/ywnd1144/Gopay_plus_automatic)
[![Stars](https://img.shields.io/github/stars/ywnd1144/Gopay_plus_automatic?style=social)](https://github.com/ywnd1144/Gopay_plus_automatic)

> 项目地址：<https://github.com/ywnd1144/Gopay_plus_automatic>

全自动 ChatGPT Plus 订阅工具。给定一个 ChatGPT `access_token`，本项目会在 **约 20 秒** 内通过 Stripe → Midtrans → GoPay 的 tokenization 支付链路自动完成 0 元首月订阅。

> ⚠️ **此项目将不会再进行更新，仅供研究、娱乐、学习，有能力者自行二开。**

**声明**：项目作者与任何渠道方无关，也不从事相关的服务行为。此项目仅为开源共享交流，研究仅为个人兴趣爱好。本项目为**免费开源**，收费售卖者自重。问题请发邮箱 `links-to@outlook.com`。作者不对任何使用者的行为负责，该项目仅供学习交流。

**不建议没有基础的用户自己部署**，请使用 GPT / Claude 的高级模型辅助部署，并根据具体场景调整。如果你只是想看看效果，推荐先用 `manual` 模式跑一次单号，确认链路打通再谈批量。

---

## 目录

1. [它能做什么](#它能做什么)
2. [当前风控现状（必读）](#当前风控现状必读)
3. [使用前要准备什么](#使用前要准备什么)
4. [架构说明](#架构说明)
5. [安装步骤（从零开始）](#安装步骤从零开始)
6. [配置说明](#配置说明)
7. [使用方法](#使用方法)
8. [三种 OTP 接收方案](#三种-otp-接收方案)
9. [生产部署（systemd 自启动）](#生产部署systemd-自启动)
10. [常见问题](#常见问题)
11. [项目结构](#项目结构)
12. [免责声明](#免责声明)

---

## 它能做什么

- 输入一个 ChatGPT `access_token`
- 自动创建 IDR（印尼盾）订阅订单
- 自动通过 Stripe + Midtrans + GoPay tokenization 完成付款
- 自动接收并填写 OTP 验证码
- 自动输入 GoPay PIN
- 自动验证订阅状态
- 最终结果：该账号变成 ChatGPT Plus（0 元首月试用）

整个过程约 20 秒，全程无人工干预（配置好之后）。支持单号调试、多号批量、并发订阅。

---

## 当前风控现状（必读）

这部分必须先看，否则跑起来碰到风控你会以为是代码 bug。

### 1. CDN 层面的 "There's a technical error"

遇到 `There's a technical error. Don't worry, we're working on it. Please try again.` 时，这是 Cloudflare 对 Midtrans linking 端点的限流。

**绕过方式**：项目根目录 `429/` 文件夹提供了基于 Chrome 指纹浏览器的绕过脚本（通过浏览器直接跑 linking，避免 SDK 指纹），多数情况下也可以通过**多次点击重试**触发 CDN 放行。

> 注：该脚本不在本仓库主流程中，仅作备用工具。

### 2. Midtrans 反欺诈（fraud_status=deny）

当订阅返回 `charge: fraud_status=deny` 或出现 `Failed to proceed to GoPay. Please place your order again`：

- 这是 **Midtrans 对虚拟号/同一用户短时间内多次 linking 的反欺诈拦截**
- 触发后**该号已无法再用于 GoPay 支付**，请换号
- 正常使用（一号一订阅）不会触发
- 调试阶段反复跑同一个号会触发，等几小时 ~ 1 天可恢复

### 3. 一号多绑已受限

截止到 2026-05-12，单个 GoPay 号多绑 Plus 已行不通，目前实测最多能绑 1~3 个账号。两种策略：

- **一号一绑（推荐）**：每个 GoPay 号只绑一个 ChatGPT 账号，无需 WhatsApp，用 SMS 接码即可
- **一号多绑**：在虚拟手机号期限内（一般接码平台号能保 10~60 分钟）多次接码多次绑定，或注册 WhatsApp 使用 WhatsApp OTP，但 WhatsApp 封号风险较大

### 4. IP 出口要求

- **必须**是日本出口 IP（实测 100% 可通过 ChatGPT 地区校验）或中国台湾地区 IP
- 其他地区的代理拿不到 Plus 订阅资格

### 5. 账号邮箱要求

目前已知可获取 Plus 资格的邮箱类型：

- Outlook / Hotmail
- 域名邮箱（前提：需要给子域名加 `edu` 前缀，例如原域名 `abc.com`，需用 `edu.abc.com` 的邮箱）

### 6. GoPay / Gojek 账号需要自己注册

本项目**不**自动化 GoPay/Gojek 的注册（自动化注册难度过高）。你需要：

1. 用印尼虚拟号在接码平台买号
2. 手动注册 Gojek / GoPay（或用模拟器）
3. 设置 6 位 PIN
4. 拿到 "手机号 + PIN" 作为本项目的输入

### 7. 支付链路状态

- **支付链路百分百可行**，已在生产环境多次验证。
- 支付失败（不是脚本错误）几乎都是：号状态异常、IP 被风控、账号侧触发反欺诈。

---

## 使用前要准备什么

| 项目 | 说明 | 如何获取 |
|---|---|---|
| Linux 服务器 | 推荐 Debian / Ubuntu，1 核 1G 即可 | 任意云服务商 |
| Python | 3.10 及以上 | `apt install python3 python3-pip` |
| Node.js | 18+（**仅 `whatsapp` 模式需要**） | NodeSource 源 |
| SOCKS5 代理 | **日本**出口 IP | 自建 / 购买 |
| GoPay 账号 | 印尼号 + 6 位 PIN（**必须已开启 PIN**，否则无法支付） | 虚拟号 + Gojek APP 注册 |
| ChatGPT access_token | 要订阅的账号凭证 | 见下文 |

### 如何获取 access_token

1. 用浏览器登录 <https://chatgpt.com>
2. 地址栏访问 <https://chatgpt.com/api/auth/session>
3. 页面返回 JSON，找到 `accessToken` 字段
4. 复制它的值（以 `eyJ` 开头的一长串，通常 1000+ 字符）
5. 这就是 `access_token`

> `access_token` 有效期约 24 小时，过期需重新获取。

### 如何注册 GoPay 账号

1. 在接码平台（HeroSMS / 5sim / sms-activate 等）买一个印尼手机号
2. 下载 Gojek APP（或用模拟器 MuMu / 雷电等）
3. 用该印尼号注册 Gojek 账号
4. 注册过程会收到 SMS 验证码（从接码平台获取）
5. 在 APP 内设置 GoPay PIN（6 位数字；**强烈建议所有号统一用同一个 PIN**，方便批量）
6. 记录：`手机号 + PIN`

批量订阅就重复上面步骤，准备多个 `手机号 + PIN` 对。

---

## 架构说明

项目由 3 个服务组成：

```
                    用户请求
                       |
                       v
+--------------------------------------------------+
|  orchestrator（编排器）        监听 :8800         |
|  接收 /subscribe 请求，协调整个流程              |
+--------------------------------------------------+
           |                           |
           v                           v
+-------------------+        +-------------------+
| plus_gopay_links  |        |  OTP 来源          |
| 支付核心（gRPC）   |        |  (三选一)         |
| 监听 :50051       |        |                   |
| 执行完整支付流程   |        |  1. manual        |
|                   |        |  2. sms_api       |
|                   |        |  3. whatsapp      |
+-------------------+        +-------------------+
```

你不需要关心内部流程，只需：

1. 配好 `config.json`
2. 启动两个服务（WhatsApp 模式多一个）
3. 通过 HTTP 调 `/subscribe`

---

## 安装步骤（从零开始）

### 第一步：准备服务器

```bash
# 以 root 登录 Linux 服务器
apt update && apt upgrade -y

# 安装 Python
apt install -y python3 python3-pip curl git

# 可选：仅 whatsapp 模式需要
curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
apt install -y nodejs
```

### 第二步：拉取项目

```bash
cd /opt
git clone https://github.com/ywnd1144/Gopay_plus_automatic.git gopay-plus
cd gopay-plus
```

### 第三步：安装 Python 依赖

```bash
pip install -r requirements.txt
```

报错 `externally-managed-environment` 时：

```bash
pip install --break-system-packages -r requirements.txt
```

### 第四步：安装 Node.js 依赖（仅 WhatsApp 模式）

```bash
cd to_whatsapp && npm install && cd ..
```

### 第五步：复制配置模板

```bash
cp config.example.json config.json
nano config.json     # 或 vim / vi
```

字段说明见下一章。

### 第六步：启动

一键脚本（Linux）：

```bash
chmod +x start.sh
./start.sh
```

或手动启动（方便调试）：

```bash
# 终端 1：支付核心
cd plus_gopay_links
python3 payment_server.py --config ../config.json --listen :50051

# 终端 2：编排器
cd /opt/gopay-plus
python3 orchestrator.py

# 终端 3（仅 whatsapp 模式）：WhatsApp Relay
cd to_whatsapp
WA_PAIRING_PHONE=62xxxxxxxxxx WA_PROXY_URL=socks5://127.0.0.1:1080 WA_GRPC_PORT=50056 node index.js
```

### 第七步：自检

```bash
curl http://localhost:8800/health
# {"ok": true, "service": "gopay-plus", "otp_mode": "manual"}
```

---

## 配置说明

打开 `config.example.json`，**复制成 `config.json`** 再改（改 example 跑不起）。

> JSON 不支持注释，实际 `config.json` 里不要留下面的 `//` 说明行。

```jsonc
{
  "gopay": {
    "country_code": "62",
    // 印尼国家码，固定 62

    "phone_number": "81234567890",
    // 默认 GoPay 手机号（不含国家码）
    // 批量模式时这里填一个占位号，每次 /subscribe 可传 phone_number 覆盖

    "pin": "123456",
    // 默认 6 位 PIN，批量建议统一

    "browser_locale": "zh-CN",
    "pin_locale": "id",

    "otp_channel": "whatsapp",
    // "whatsapp"(默认) | "sms"
    // 选 "sms" 时，脚本会在 consent 后等倒计时再切换到 SMS 通道
    // 用 sms_api 接码平台时必须设成 "sms"，否则接码平台收不到

    "sms_switch_countdown_sec": 30,
    // 切换到 SMS 前的等待秒数（GoPay web 端的按钮倒计时）

    "sms_switch_endpoint": "",
    // 切换通道的 HTTP endpoint，留空用内置默认
    // 若 GoPay 接口变了，抓一次 HAR（点"改用短信"按钮时的请求）把 URL 填这里

    "sms_switch_body_extra": {}
    // 切换请求需要的额外 body 字段，留空用内置默认
  },

  "proxy": "socks5://127.0.0.1:1080",
  // SOCKS5 代理，必须日本出口

  "orchestrator": {
    "port": 8800,
    "otp_timeout": 90,
    // 等 OTP 的最长秒数；sms_api 模式建议 ≥ 120
    "auth_token": "my-secret-token-123"
    // 自定义随机字符串，调用 /subscribe 时需 Authorization: Bearer 该值
  },

  "otp": {
    "mode": "manual",
    // "manual" | "sms_api" | "whatsapp"

    "sms_api": {
      "provider": "herosms",
      "api_key": "",
      "base_url": "https://api.herosms.com",
      "country": "id",
      "service": "gopay",
      "poll_interval_sec": 3,
      "poll_timeout_sec": 90
    },

    "whatsapp": {
      "grpc_addr": "127.0.0.1:50056"
    }
  }
}
```

---

## 使用方法

### 单次订阅（基本）

```bash
curl -X POST http://localhost:8800/subscribe \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-secret-token-123" \
  -d '{"session_token": "eyJhbGciOiJSUzI1NiIs..."}'
```

成功：

```json
{"ok": true, "charge_ref": "A1xxxxxxxxxxxxxxxxxxxxxxx", "elapsed_ms": 19928}
```

失败：

```json
{"ok": false, "error": "otp_timeout", "detail": "timeout waiting for OTP after 90s", "elapsed_ms": 91000}
```

### 多号订阅（每次指定不同手机号 / PIN）

```bash
curl -X POST http://localhost:8800/subscribe \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-secret-token-123" \
  -d '{
    "session_token": "eyJ...",
    "phone_number": "82222222222",
    "pin": "123456"
  }'
```

`phone_number` / `pin` 都是可选字段，不传就使用 `config.json` 里的默认值。

### 并发订阅

orchestrator 支持并发，直接并发发多个 `/subscribe` 请求即可（每个传不同的 `session_token` + `phone_number`）。

> 只用 `manual` 模式并发时，多个请求的 OTP 会共用一个 inbox，容易串号。批量场景请用 `sms_api`。

---

## 三种 OTP 接收方案

GoPay linking 会发一个 6 位验证码到号主手机。如何把这个 OTP 回填给本工具，有三种方式。

### 方案一：`manual` 模式（手动 / ADB）

最简单，适合新手调试和少量使用。

原理：你自己（或脚本）看到验证码后，手动发 HTTP 把验证码告诉编排器。

配置：`config.json` 中 `otp.mode` = `"manual"`。

使用流程：

```
1. POST /subscribe
2. 约 10 秒后 GoPay 会发验证码到号主手机（默认 WhatsApp）
3. 看到验证码后，90 秒内执行：
   curl -X POST http://服务器:8800/otp \
     -H "Content-Type: application/json" \
     -d '{"otp": "123456"}'
4. orchestrator 收到后继续流程
5. 返回订阅结果
```

如果你有 Android 模拟器（MuMu / 雷电等），可以用 `otp_forwarder.py` 自动化转发：

```bash
# 编辑脚本顶部：
#   OTP_URL = "http://你的服务器:8800/otp"
#   AUTH    = "Bearer my-secret-token-123"

adb connect 127.0.0.1:7555    # 或你的模拟器 ADB 端口
adb devices

python3 otp_forwarder.py      # 保持窗口开着
```

> **重要**：不要点开 WhatsApp 的通知消息，否则 ADB 抓不到。

### 方案二：`sms_api` 模式（接码平台自动获取）

适合批量生产，全自动无人值守。

原理：GoPay 把验证码通过 **SMS** 发到虚拟手机号，编排器自动调接码平台 API 查询并取回验证码。

**重要前提：GoPay linking 默认把 OTP 发到 WhatsApp，接码平台收不到。** 必须同时配置：

1. `otp.mode` = `"sms_api"`
2. `gopay.otp_channel` = `"sms"`（让脚本在 consent 后切换 SMS 通道）
3. `orchestrator.otp_timeout` ≥ **120**（因为要等 30 秒倒计时 + SMS 送达）

关键字段（合并摘录）：

```json
{
  "gopay": {
    "otp_channel": "sms",
    "sms_switch_countdown_sec": 30,
    "sms_switch_endpoint": ""
  },
  "orchestrator": { "otp_timeout": 120 },
  "otp": {
    "mode": "sms_api",
    "sms_api": {
      "api_key": "你的key",
      "base_url": "https://api.你的平台.com"
    }
  }
}
```

> `sms_switch_endpoint` 留空时使用内置默认。若 GoPay 调整了接口，抓一次 HAR（点 GoPay web 页"改用短信接收"时发出的请求），把 URL 填到 `sms_switch_endpoint`，附加字段填到 `sms_switch_body_extra`。

接码平台对接：

编排器默认请求：

```
GET {base_url}?action=get_sms&api_key={你的key}&phone={手机号}&country=id
```

再从响应文本里自动提取 6 位数字（`\b\d{6}\b`）。

如果你的平台 URL 格式不同，直接改 `orchestrator.py` 里 `_wait_sms_api_otp` 函数的 URL 构造即可，响应解析是通用的。

常见平台参考：

```
HeroSMS:
  GET https://api.herosms.com/api/get_sms?api_key=KEY&phone=PHONE
  返回：{"sms": "Your verification code is 123456"}

5sim:
  GET https://5sim.net/v1/user/check/{order_id}
  Header: Authorization: Bearer KEY
  返回：{"sms": [{"text": "123456 is your code"}]}

sms-activate:
  GET https://api.sms-activate.org/stubs/handler_api.php?api_key=KEY&action=getStatus&id=ORDER_ID
  返回：STATUS_OK:123456
```

批量流程示例：

```
1. 接码平台买一个印尼号（假设 81234567890）
2. 用这个号在 Gojek 注册 GoPay，设 PIN 为 123456
3. curl -X POST http://localhost:8800/subscribe \
     -H "Authorization: Bearer my-secret-token-123" \
     -d '{"session_token":"eyJ...","phone_number":"81234567890","pin":"123456"}'
4. 编排器自动从接码平台取验证码，完成订阅
5. 换下一组 号 + access_token，继续
```

> 作者本人未逐一实测过每个接码平台，但原理一致（拉短信、提取 6 位码），用户需根据自己买的平台做小幅改造。

### 方案三：`whatsapp` 模式（WhatsApp 自动接收）

适合**固定一个** GoPay 号长期使用的场景。

原理：在服务器上用 Baileys 登录 WhatsApp，监听 GoPay 发来的消息。

配置：`config.json` 中 `otp.mode` = `"whatsapp"`。

首次配对：

```bash
cd to_whatsapp
export WA_PAIRING_PHONE=62xxxxxxxxxx   # 你的 WhatsApp 主号（含 62）
export WA_PROXY_URL=socks5://127.0.0.1:1080
export WA_GRPC_PORT=50056
node index.js
```

终端会显示一个 8 位配对码，例如 `WN2XQNLB`。

在你的手机上操作：

1. 打开 WhatsApp
2. 右上角三点 → 已关联设备 → 关联设备
3. 输入那个 8 位配对码

配对成功后服务常驻，自动接收 GoPay 验证码。

**已知问题**：WhatsApp 的**关联设备**可能对金融类消息（如 GoPay 验证码）做屏蔽（`MASK_LINKED_DEVICES`），导致服务端收不到。遇到这个情况请改用 `manual` 或 `sms_api`。

### 三种方案对比

|  | manual | sms_api | whatsapp |
|---|---|---|---|
| 全自动 | 需手动 / ADB | 完全自动 | 完全自动 |
| 多号支持 | 支持 | 支持 | 仅单号 |
| 额外成本 | 无 | 接码平台费用 | 无 |
| 稳定性 | 取决于人 / ADB | 高 | 可能被屏蔽 |
| 适合场景 | 调试 / 少量 | 批量生产 | 固定号长期 |

---

## 生产部署（systemd 自启动）

让服务开机自启、崩溃自动拉起：

```bash
# 1. 支付核心
cat > /etc/systemd/system/plus-gopay-links.service << 'EOF'
[Unit]
Description=GoPay Payment Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/gopay-plus
ExecStart=/usr/bin/python3 plus_gopay_links/payment_server.py --config config.json --listen :50051
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# 2. 编排器
cat > /etc/systemd/system/gopay-orchestrator.service << 'EOF'
[Unit]
Description=GoPay Orchestrator
After=plus-gopay-links.service

[Service]
Type=simple
WorkingDirectory=/opt/gopay-plus
ExecStart=/usr/bin/python3 orchestrator.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# 启用并启动
systemctl daemon-reload
systemctl enable --now plus-gopay-links gopay-orchestrator

# 查看状态 / 日志
systemctl status plus-gopay-links
systemctl status gopay-orchestrator
journalctl -u gopay-orchestrator -f
```

---

## 常见问题

### Q: 返回 `otp_timeout`

在 `otp_timeout` 内没收到验证码。排查：

- `manual`：是否在超时内 POST 了 `/otp`？
- `sms_api`：`api_key` / `base_url` 配置对不对？`gopay.otp_channel` 是否设成 `"sms"`？号是否仍在接码平台的"激活"窗口里？
- `whatsapp`：Relay 是否在跑？是否被 `MASK_LINKED_DEVICES` 屏蔽？

### Q: 返回 `start_gopay_failed`

通常是 `access_token` 无效或已过期。从 `https://chatgpt.com/api/auth/session` 重新拿一个。

少数情况是 Stripe confirm 阶段被风控（同 IP / 账号特征触发），换号或换 IP 试。

### Q: PIN 验证失败 / 被限流

- PIN 不是 6 位数字 → 改 `config.json`
- 同号短时间内多次错 PIN → 被 GoPay 临时限流，冷却期约 1 小时
- 确保 config 里的 PIN 位数正确（曾见把 6 位 PIN 误抄成 7 位导致反复失败的案例）

### Q: Midtrans charge 返回 `fraud_status=deny`

同号短时间内 linking 次数过多，触发 Midtrans 反欺诈。这一号作废，换号即可。一号一订阅场景不会遇到。

### Q: 返回 `midtrans linking exhausted retries: account already linked`

这个 GoPay 号最近已绑过其他 ChatGPT 账号。根据一号多绑限制，可能该号已达上限；换号重试。

### Q: 同时订阅多个账号怎么做

并发发多个 `/subscribe` 即可。注意 `manual` 模式的 OTP inbox 是共享的，并发时容易串号，批量请用 `sms_api`。

### Q: 代理要求

- SOCKS5 协议
- 日本出口（或中国台湾），其他地区拿不到 Plus 资格
- 不能是已被 GoPay / Midtrans 黑名单的 IP
- 推荐自建 / 住宅代理

### Q: Windows 本地能跑吗

可以。核心代码是 Python + Node.js，主流程没有强依赖 Linux；原来的 Linux 限制主要在 `start.sh`、`systemd` 自启动示例和 README 命令写法。

推荐两种方式：

1. **WSL 方式**：按 Linux 文档走，最省心。
2. **原生 Windows 方式**：用 PowerShell 启动，适合本地调试。

Windows 原生准备：

```powershell
# 1. 安装 Python 3.10+ / Node.js 18+ / Git 后，在项目根目录执行
Copy-Item config.example.json config.json

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 仅 whatsapp 模式需要
cd to_whatsapp
npm install
cd ..
```

启动：

```powershell
# 如果当前策略不允许运行脚本，可只对本次窗口放开
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\start.ps1
```

健康检查：

```powershell
Invoke-RestMethod http://localhost:8800/health
```

手动启动也可以：

```powershell
# 终端 1：支付核心
cd plus_gopay_links
python payment_server.py --config ..\config.json --listen :50051

# 终端 2：编排器
cd ..
python orchestrator.py
```

如果使用 `whatsapp` 模式，Windows 下环境变量写法是：

```powershell
cd to_whatsapp
$env:WA_PAIRING_PHONE="62xxxxxxxxxx"
$env:WA_PROXY_URL="socks5://127.0.0.1:1080"
$env:WA_GRPC_PORT="50056"
node index.js
```

注意点：

- PowerShell 里的 `curl` 可能是 `Invoke-WebRequest` 的别名，建议直接用 `Invoke-RestMethod`，或显式使用 `curl.exe`。
- `start.ps1` 会把日志写到 `logs/` 目录。
- `otp_forwarder.py` 依赖 `adb`，Windows 也能用，但需要把 Android Platform Tools 加到 PATH。
- `systemd` 自启动只适用于 Linux；Windows 如需常驻，可改用“任务计划程序”或 NSSM。
- `to_whatsapp/wa_relay.py` 里的进程管理偏 Linux，Windows 原生建议直接跑 `to_whatsapp/index.js` 或用 `start.ps1`。

---

## 项目结构

```
Gopay_plus_automatic/
├── README.md                # 本文件
├── config.example.json      # 配置模板（复制为 config.json 使用）
├── requirements.txt         # Python 顶层依赖
├── start.sh                 # 一键启动（Linux / WSL）
├── orchestrator.py          # 编排器 HTTP API + 三种 OTP 模式
├── otp_forwarder.py         # ADB OTP 自动转发（manual 模式辅助脚本）
├── .gitignore
├── plus_gopay_links/        # 支付核心
│   ├── gopay.py             # Stripe / Midtrans / GoPay 完整支付流程
│   ├── payment_server.py    # gRPC 封装
│   ├── requirements.txt
│   ├── proto/
│   │   ├── payment.proto
│   │   └── otp.proto
│   ├── payment_pb2.py / payment_pb2_grpc.py
│   └── otp_pb2.py / otp_pb2_grpc.py
└── to_whatsapp/             # WhatsApp OTP 接收（可选模块）
    ├── index.js             # Baileys 客户端
    ├── package.json
    ├── wa_relay.py          # Node 进程 wrapper
    └── proto/otp.proto
```

---

## 免责声明

本项目仅供学习研究使用。使用者需自行承担风险，遵守相关服务条款，不得违反 OpenAI 条款和相关法律法规。使用本项目即默认用户知情并同意：一切后果由用户个人承担，与作者无关。
