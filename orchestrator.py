#!/usr/bin/env python3
"""
GoPay Plus 编排器 — HTTP API
==============================
POST /subscribe  {"session_token": "eyJ...", "phone_number": "可选，覆盖默认号"}
POST /otp        {"otp": "123456"}  → 手动推送 OTP（manual 模式）
GET  /health

OTP 模式（config.json → otp.mode）：
  - manual:   等待外部 POST /otp（手动输入或 ADB 转发）
  - sms_api:  自动轮询接码平台 API（如 HeroSMS）获取 SMS OTP
  - whatsapp: 通过 gRPC 调 to_whatsapp 模块获取 WhatsApp OTP
"""
import json
import logging
import os
import re
import sqlite3
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path

import grpc

# 加载 proto stubs
sys.path.insert(0, str(Path(__file__).parent / "plus_gopay_links"))
import payment_pb2
import payment_pb2_grpc

# ─── 配置 ───
CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config():
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)

CFG = load_config()
GOPAY_CFG = CFG.get("gopay", {})
ORCH_CFG = CFG.get("orchestrator", {})
OTP_CFG = CFG.get("otp", {})

PAYMENT_ADDR = "127.0.0.1:50051"
HTTP_PORT = int(ORCH_CFG.get("port", 8800))
OTP_TIMEOUT = int(ORCH_CFG.get("otp_timeout", 90))
START_GOPAY_TIMEOUT = int(
    ORCH_CFG.get(
        "start_gopay_timeout",
        GOPAY_CFG.get("start_gopay_timeout", 300),
    )
)
COMPLETE_GOPAY_TIMEOUT = int(
    ORCH_CFG.get(
        "complete_gopay_timeout",
        GOPAY_CFG.get("complete_gopay_timeout", 300),
    )
)
OTP_RESEND_AFTER = int(
    ORCH_CFG.get(
        "otp_resend_after",
        GOPAY_CFG.get("otp_resend_after", 60),
    )
)
AUTH_TOKEN = ORCH_CFG.get("auth_token", "")
OTP_MODE = OTP_CFG.get("mode", "manual")  # manual | sms_api | whatsapp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "orchestrator.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("orchestrator")


def _webui_db_path() -> Path:
    data_dir = os.environ.get("WEBUI_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "webui.db"
    configured = (
        (CFG.get("gopay") or {})
        .get("account_claim", {})
        .get("db_path", "")
    )
    configured = str(configured or "").strip()
    if configured:
        path = Path(configured)
        return path if path.suffix.lower() == ".db" else path / "webui.db"
    return Path(__file__).parent / "output" / "webui.db"


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _normalize_session_cookie_value(value: str) -> str:
    value = str(value or "").strip()
    if "__Secure-next-auth.session-token=" not in value:
        return value
    for raw in value.split(";"):
        part = raw.strip()
        if part.startswith("__Secure-next-auth.session-token="):
            return part.split("=", 1)[1].strip()
    return value


def _parse_auth_payload(value: str) -> dict:
    value = str(value or "").strip()
    if not value.startswith("{"):
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _token_lookup_value(token: str, auth_payload: dict) -> str:
    for key in ("session_token", "access_token", "cookie_header"):
        value = str(auth_payload.get(key) or "").strip()
        if value:
            return _normalize_session_cookie_value(value)
    return _normalize_session_cookie_value(token)


def _classify_account_status(result: dict, refresh_token: str = "") -> str:
    text = " ".join(
        str(result.get(key, ""))
        for key in ("error", "detail", "message", "status")
    ).upper()
    if result.get("ok"):
        return "SUCCESS" if refresh_token else "UN_OAUTHED"
    if "ADD_PHONE" in text:
        return "ADD_PHONE"
    due_match = re.search(r"\b(?:DUE|AMOUNT_DUE|TOTAL_DUE)\D+(\d+)", text)
    if (
        "NO_TRIAL" in text
        or "NO TRIAL" in text
        or "DUE NOT 0" in text
        or "CHECKOUT_AMOUNT_MISMATCH" in text
        or "COMPUTED INVOICE AMOUNT DOES NOT MATCH" in text
        or (due_match and int(due_match.group(1)) != 0)
    ):
        return "NO_TRIAL"
    return "FAILED"


def _find_registered_account(
    conn: sqlite3.Connection,
    *,
    account_id: str = "",
    email: str = "",
    token: str = "",
) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    if account_id:
        row = conn.execute(
            "SELECT id, email, status, refresh_token FROM registered_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row:
            return row
    email_norm = _normalize_email(email)
    if email_norm:
        row = conn.execute(
            """
            SELECT id, email, status, refresh_token
            FROM registered_accounts
            WHERE lower(email) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (email_norm,),
        ).fetchone()
        if row:
            return row
    if token:
        row = conn.execute(
            """
            SELECT id, email, status, refresh_token
            FROM registered_accounts
            WHERE session_token = ?
               OR access_token = ?
               OR instr(coalesce(cookie_header, ''), ?) > 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (token, token, token),
        ).fetchone()
        if row:
            return row
    return None


def update_registered_account_after_payment(
    result: dict,
    *,
    account_id: str = "",
    email: str = "",
    token: str = "",
) -> None:
    db_path = _webui_db_path()
    if not db_path.exists():
        log.info("account status update skipped: db not found at %s", db_path)
        return
    try:
        with sqlite3.connect(str(db_path), timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            row = _find_registered_account(
                conn,
                account_id=account_id,
                email=email,
                token=token,
            )
            if not row:
                log.warning("account status update skipped: registered account not found")
                return
            current = str(row["status"] or "INITIAL").upper()
            new_status = _classify_account_status(result, row["refresh_token"] or "")
            if current == "ADD_PHONE" and new_status in {"SUCCESS", "UN_OAUTHED"}:
                log.info(
                    "account status preserved: id=%s email=%s status=ADD_PHONE",
                    row["id"], row["email"],
                )
                return
            conn.execute(
                "UPDATE registered_accounts SET status = ? WHERE id = ?",
                (new_status, row["id"]),
            )
            conn.commit()
            log.info(
                "account status updated: id=%s email=%s %s -> %s",
                row["id"], row["email"], current, new_status,
            )
    except Exception as e:
        log.warning("account status update failed: %s", e)

# ═══════════════════════════════════════════════════════════
# OTP 提供者（三种模式）
# ═══════════════════════════════════════════════════════════

# ─── Manual 模式：等外部 POST /otp ───
# 支持按手机号隔离 OTP（防止多并发时串号）
_otp_lock = threading.Lock()
_otp_inbox = []  # [{"otp": "123456", "ts": unix, "phone": "可选"}]
_otp_event = threading.Event()

def push_otp(otp_code: str, phone: str = ""):
    with _otp_lock:
        _otp_inbox.append({"otp": otp_code, "ts": int(time.time()), "phone": phone})
        while len(_otp_inbox) > 20:
            _otp_inbox.pop(0)
    _otp_event.set()

def _wait_manual_otp(issued_after: int, timeout: int, phone: str = "") -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _otp_lock:
            for i, item in enumerate(_otp_inbox):
                if item["ts"] >= issued_after:
                    # 如果指定了手机号，优先匹配；否则取最新的
                    if phone and item.get("phone") and item["phone"] != phone:
                        continue
                    _otp_inbox.pop(i)
                    return item["otp"]
        _otp_event.clear()
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        _otp_event.wait(timeout=min(remaining, 2.0))
    return ""


# ─── SMS API 模式：轮询接码平台 ───
def _wait_sms_api_otp(
    phone: str,
    issued_after: int,
    timeout: int,
    activation_id: str = "",
) -> str:
    """轮询接码平台 API 获取 SMS OTP。
    
    通用实现：轮询 {base_url}/get_sms 接口，提取 6 位数字。
    不同平台只需修改 URL 格式和响应解析。
    
    当前支持的 API 格式：
      请求：GET {base_url}?action=get_sms&api_key={key}&phone={phone}
      响应：任意包含 6 位数字的文本/JSON
    
    如果你的平台格式不同，修改下方 url 构造和响应解析即可。
    """
    sms_cfg = OTP_CFG.get("sms_api", {})
    api_key = sms_cfg.get("api_key", "")
    base_url = sms_cfg.get("base_url", "").rstrip("/")
    poll_interval = int(sms_cfg.get("poll_interval_sec", 3))
    use_proxy = bool(sms_cfg.get("use_proxy", True))
    proxy_url = (sms_cfg.get("proxy") or CFG.get("proxy") or "").strip()
    activation_id = activation_id or str(
        sms_cfg.get("activation_id")
        or sms_cfg.get("order_id")
        or sms_cfg.get("id")
        or ""
    ).strip()
    
    if not api_key or not base_url:
        log.error("sms_api 配置不完整（缺少 api_key 或 base_url）")
        return ""
    
    deadline = time.time() + timeout
    log.info("SMS API: polling for phone=***%s timeout=%ds", phone[-4:], timeout)
    sess = None
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore
        sess = cffi_requests.Session(impersonate="chrome136")
        if use_proxy and proxy_url:
            sess.proxies = {"http": proxy_url, "https": proxy_url}
            log.info("SMS API: proxy enabled")
    except Exception as e:
        log.warning("SMS API: curl_cffi unavailable, falling back to urllib: %s", e)
    
    while time.time() < deadline:
        try:
            # ═══ 构造请求 URL ═══
            # 通用格式（根据你的平台修改）：
            base_url_lower = base_url.lower()
            if "hero-sms" in base_url_lower:
                if not activation_id:
                    log.error("SMS API: hero-sms requires activation_id/order_id for getStatus")
                    return ""
                url = f"{base_url}/stubs/handler_api.php?api_key={api_key}&action=getStatus&id={activation_id}"
            elif "herosms" in base_url_lower:
                url = f"{base_url}/api/get_sms?api_key={api_key}&phone={phone}"
            else:
                url = f"{base_url}?action=get_sms&api_key={api_key}&phone={phone}&country=id"

            # 发请求。优先用 curl_cffi，避免部分 Windows/服务端 TLS 握手 EOF。
            if sess is not None:
                resp = sess.get(url, headers={"Accept": "application/json"}, timeout=12)
                if resp.status_code == 404:
                    time.sleep(poll_interval)
                    continue
                if resp.status_code >= 400:
                    log.warning("SMS API HTTP %d: %s", resp.status_code, resp.text[:160])
                    time.sleep(poll_interval)
                    continue
                body = resp.text
            else:
                import urllib.request, urllib.error
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read().decode(errors="replace")
            
            # ═══ 解析响应，提取 6 位 OTP ═══
            # 尝试 JSON 解析
            try:
                data = json.loads(body)
                # 常见字段名：sms, text, code, message, otp
                text = str(
                    data.get("sms") or data.get("text") or 
                    data.get("code") or data.get("message") or
                    data.get("otp") or body
                )
            except (json.JSONDecodeError, ValueError):
                text = body
            
            # 提取 6 位数字
            match = re.search(r"\b(\d{6})\b", text)
            if match:
                otp = match.group(1)
                log.info("SMS API: got OTP %s", otp)
                return otp
            
            # 没拿到，等下一轮
            # 常见"还没收到"的响应：WAITING, NO_SMS, empty
            
        except Exception as e:
            if e.__class__.__name__ == "HTTPError" and getattr(e, "code", None) == 404:
                pass  # 还没收到短信，正常
            elif e.__class__.__name__ == "HTTPError":
                log.warning("SMS API HTTP %s", getattr(e, "code", "?"))
            else:
                log.warning("SMS API error: %s", e)
        
        time.sleep(poll_interval)
    
    log.warning("SMS API: timeout after %ds", timeout)
    return ""


def _request_sms_api_resend(activation_id: str) -> bool:
    sms_cfg = OTP_CFG.get("sms_api", {})
    api_key = sms_cfg.get("api_key", "")
    base_url = sms_cfg.get("base_url", "").rstrip("/")
    use_proxy = bool(sms_cfg.get("use_proxy", True))
    proxy_url = (sms_cfg.get("proxy") or CFG.get("proxy") or "").strip()
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        log.info("SMS API: skip setStatus=3 because activation_id is empty")
        return False
    if not api_key or not base_url:
        log.warning("SMS API: skip setStatus=3 because api_key/base_url is missing")
        return False
    if "hero-sms" not in base_url.lower():
        log.info("SMS API: skip setStatus=3 for unsupported provider base_url=%s", base_url)
        return False

    url = f"{base_url}/stubs/handler_api.php?action=setStatus&id={activation_id}&status=3&api_key={api_key}"
    try:
        try:
            from curl_cffi import requests as cffi_requests  # type: ignore
            sess = cffi_requests.Session(impersonate="chrome145")
            if use_proxy and proxy_url:
                sess.proxies = {"http": proxy_url, "https": proxy_url}
            resp = sess.get(url, headers={"Accept": "text/plain, */*"}, timeout=12)
            body = resp.text[:160]
            ok = resp.status_code < 400
        except Exception:
            import urllib.request
            req = urllib.request.Request(url, headers={"Accept": "text/plain, */*"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode(errors="replace")[:160]
                ok = 200 <= resp.status < 400
        log.info("SMS API: setStatus=3 activation=%s ok=%s response=%s", activation_id, ok, body)
        return ok
    except Exception as e:
        log.warning("SMS API: setStatus=3 failed: %s", e)
        return False


# ─── WhatsApp 模式：gRPC 调 to_whatsapp ───
def _wait_whatsapp_otp(issued_after: int, timeout: int) -> str:
    """通过 gRPC 调用 to_whatsapp 模块的 OtpService.WaitForOtp。"""
    wa_cfg = OTP_CFG.get("whatsapp", {})
    grpc_addr = wa_cfg.get("grpc_addr", "127.0.0.1:50056")
    
    try:
        # 动态加载 OTP proto stubs
        otp_proto_dir = Path(__file__).parent / "to_whatsapp" / "proto"
        stub_dir = Path(__file__).parent / "_otp_stubs"
        stub_dir.mkdir(exist_ok=True)
        
        if not (stub_dir / "otp_pb2.py").exists():
            import subprocess
            subprocess.run([
                sys.executable, "-m", "grpc_tools.protoc",
                f"-I{otp_proto_dir}",
                f"--python_out={stub_dir}",
                f"--grpc_python_out={stub_dir}",
                str(otp_proto_dir / "otp.proto"),
            ], check=True)
        
        sys.path.insert(0, str(stub_dir))
        import otp_pb2
        import otp_pb2_grpc
        
        channel = grpc.insecure_channel(grpc_addr)
        stub = otp_pb2_grpc.OtpServiceStub(channel)
        req = otp_pb2.WaitForOtpRequest(
            purpose="gopay",
            timeout_seconds=timeout,
            issued_after_unix=issued_after,
        )
        resp = stub.WaitForOtp(req, timeout=timeout + 10)
        channel.close()
        
        if resp.found:
            return resp.otp
        return ""
    except Exception as e:
        log.error("WhatsApp gRPC 调用失败: %s", e)
        return ""


# ─── 统一 OTP 获取入口 ───
def get_otp(
    phone: str,
    issued_after: int,
    timeout: int,
    activation_id: str = "",
) -> str:
    """根据 OTP_MODE 选择对应的获取方式。"""
    if OTP_MODE == "sms_api":
        return _wait_sms_api_otp(phone, issued_after, timeout, activation_id=activation_id)
    elif OTP_MODE == "whatsapp":
        return _wait_whatsapp_otp(issued_after, timeout)
    else:
        # manual 模式：等 /otp POST（传 phone 用于多并发隔离）
        return _wait_manual_otp(issued_after, timeout, phone=phone)


# ═══════════════════════════════════════════════════════════
# gRPC 调用
# ═══════════════════════════════════════════════════════════

def call_start_gopay(session_token: str, phone: str = "", pin: str = "", proxy_url: str = "") -> dict:
    channel = grpc.insecure_channel(PAYMENT_ADDR)
    stub = payment_pb2_grpc.PaymentServiceStub(channel)
    req = payment_pb2.StartGoPayRequest(
        session_token=session_token,
        country_code=GOPAY_CFG.get("country_code", "62"),
        phone_number=phone or GOPAY_CFG.get("phone_number", ""),
        pin=pin or GOPAY_CFG.get("pin", ""),
        proxy_url=proxy_url,
    )
    try:
        resp = stub.StartGoPay(req, timeout=START_GOPAY_TIMEOUT)
        return {
            "success": resp.success,
            "error_message": resp.error_message,
            "flow_id": resp.flow_id,
            "snap_token": resp.snap_token,
            "issued_after_unix": resp.issued_after_unix,
        }
    except grpc.RpcError as e:
        return {"success": False, "error_message": f"gRPC: {e.code()} {e.details()}"}
    finally:
        channel.close()

def call_complete_gopay(flow_id: str, otp: str) -> dict:
    channel = grpc.insecure_channel(PAYMENT_ADDR)
    stub = payment_pb2_grpc.PaymentServiceStub(channel)
    req = payment_pb2.CompleteGoPayRequest(flow_id=flow_id, otp=otp)
    try:
        resp = stub.CompleteGoPay(req, timeout=COMPLETE_GOPAY_TIMEOUT)
        return {
            "success": resp.success,
            "error_message": resp.error_message,
            "charge_ref": resp.charge_ref,
        }
    except grpc.RpcError as e:
        return {"success": False, "error_message": f"gRPC: {e.code()} {e.details()}"}
    finally:
        channel.close()

def call_resend_gopay_otp(flow_id: str) -> dict:
    channel = grpc.insecure_channel(PAYMENT_ADDR)
    resend = channel.unary_unary(
        "/payment.PaymentService/ResendGoPayOtp",
        request_serializer=payment_pb2.CancelGoPayRequest.SerializeToString,
        response_deserializer=payment_pb2.GoPayResponse.FromString,
    )
    req = payment_pb2.CancelGoPayRequest(flow_id=flow_id)
    try:
        resp = resend(req, timeout=30)
        return {
            "success": resp.success,
            "error_message": resp.error_message,
        }
    except grpc.RpcError as e:
        return {"success": False, "error_message": f"gRPC: {e.code()} {e.details()}"}
    finally:
        channel.close()

def call_cancel_gopay(flow_id: str):
    try:
        channel = grpc.insecure_channel(PAYMENT_ADDR)
        stub = payment_pb2_grpc.PaymentServiceStub(channel)
        stub.CancelGoPay(payment_pb2.CancelGoPayRequest(flow_id=flow_id), timeout=10)
        channel.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# 订阅流程
# ═══════════════════════════════════════════════════════════

def run_subscribe(
    session_token: str,
    phone: str = "",
    pin: str = "",
    sms_activation_id: str = "",
    proxy_url: str = "",
) -> dict:
    """执行全自动订阅。phone/pin 可选，覆盖 config 默认值。"""
    t0 = time.time()
    use_phone = phone or GOPAY_CFG.get("phone_number", "")
    use_pin = pin or GOPAY_CFG.get("pin", "")
    log.info("subscribe start phone=***%s mode=%s", use_phone[-4:], OTP_MODE)

    # Step 1: StartGoPay
    log.info("step 1: StartGoPay")
    r1 = call_start_gopay(session_token, phone=use_phone, pin=use_pin, proxy_url=proxy_url)
    if not r1["success"]:
        return {"ok": False, "error": "start_gopay_failed",
                "detail": r1["error_message"], "elapsed_ms": int((time.time()-t0)*1000)}

    flow_id = r1["flow_id"]
    issued_after = r1["issued_after_unix"]
    log.info("step 1 done: flow_id=%s", flow_id[:8])

    # Step 2: 获取 OTP（根据模式自动选择）
    first_wait = OTP_TIMEOUT
    if OTP_RESEND_AFTER > 0:
        first_wait = min(OTP_TIMEOUT, OTP_RESEND_AFTER)
    log.info(
        "step 2: get OTP (mode=%s, timeout=%ds, resend_after=%ds, first_wait=%ds)",
        OTP_MODE,
        OTP_TIMEOUT,
        OTP_RESEND_AFTER,
        first_wait,
    )
    otp = get_otp(use_phone, issued_after, first_wait, activation_id=sms_activation_id)
    if not otp:
        log.warning("step 2 no OTP after %ds: requesting one OTP resend", first_wait)
        if OTP_MODE == "sms_api":
            _request_sms_api_resend(sms_activation_id)
        r2 = call_resend_gopay_otp(flow_id)
        if r2.get("success"):
            issued_after = int(time.time() - 5)
            log.info("step 2 retry: OTP resend requested, polling again timeout=%ds", OTP_TIMEOUT)
            otp = get_otp(use_phone, issued_after, OTP_TIMEOUT, activation_id=sms_activation_id)
        else:
            log.warning("step 2 retry: GoPay OTP resend failed: %s", r2.get("error_message", ""))
        if not otp:
            call_cancel_gopay(flow_id)
            return {"ok": False, "error": "otp_timeout",
                    "detail": f"timeout waiting for OTP after resend (mode={OTP_MODE})",
                    "elapsed_ms": int((time.time()-t0)*1000)}

    log.info("step 2 done: otp=%s", otp)

    # Step 3: CompleteGoPay
    log.info("step 3: CompleteGoPay")
    r3 = call_complete_gopay(flow_id, otp)
    elapsed = int((time.time()-t0)*1000)

    if r3["success"]:
        log.info("subscribe SUCCESS in %dms", elapsed)
        return {"ok": True, "charge_ref": r3["charge_ref"], "elapsed_ms": elapsed}
    else:
        log.error("CompleteGoPay failed: %s", r3["error_message"])
        return {"ok": False, "error": "complete_failed",
                "detail": r3["error_message"], "elapsed_ms": elapsed}


# ═══════════════════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════════════════

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        if not AUTH_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {AUTH_TOKEN}"

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "gopay-plus", "otp_mode": OTP_MODE})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/otp":
            # 手动推送 OTP（manual 模式 + 任何模式的备用入口）
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            raw = str(body.get("otp", "") or body.get("text", "") or "")
            phone = str(body.get("phone", "") or body.get("phone_number", "") or "")
            match = re.search(r"\d{6}", raw)
            if match:
                otp_code = match.group(0)
                push_otp(otp_code, phone=phone)
                log.info("OTP received via /otp: %s (phone=%s)", otp_code, phone[-4:] if phone else "*")
                self._json(200, {"ok": True, "otp": otp_code})
            else:
                self._json(400, {"ok": False, "error": "no 6-digit OTP found"})
            return

        if self.path == "/subscribe":
            if not self._auth_ok():
                self._json(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            token = body.get("session_token", "").strip()
            if not token or len(token) < 100:
                self._json(400, {"ok": False, "error": "bad_token"})
                return
            auth_payload = _parse_auth_payload(token)
            # 可选参数：覆盖默认手机号和 PIN
            phone = body.get("phone_number", "").strip()
            pin = body.get("pin", "").strip()
            sms_activation_id = (
                body.get("sms_activation_id", "")
                or body.get("activation_id", "")
                or body.get("sms_order_id", "")
            ).strip()
            proxy_url = (
                body.get("proxy_url", "")
                or body.get("proxy_add", "")
                or ""
            ).strip()
            account_id = str(
                body.get("registered_account_id", "")
                or body.get("account_id", "")
                or body.get("id", "")
                or auth_payload.get("account_id", "")
                or auth_payload.get("registered_account_id", "")
                or auth_payload.get("id", "")
            ).strip()
            account_email = str(
                body.get("account_email", "")
                or body.get("email", "")
                or auth_payload.get("email", "")
                or auth_payload.get("account_email", "")
            ).strip()
            try:
                result = run_subscribe(
                    token,
                    phone=phone,
                    pin=pin,
                    sms_activation_id=sms_activation_id,
                    proxy_url=proxy_url,
                )
            except Exception as e:
                log.exception("subscribe crashed")
                result = {
                    "ok": False,
                    "error": "subscribe_exception",
                    "detail": str(e),
                }
            update_registered_account_after_payment(
                result,
                account_id=account_id,
                email=account_email,
                token=_token_lookup_value(token, auth_payload),
            )
            self._json(200, result)
            return

        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        log.info("HTTP %s %s", self.address_string(), fmt % args)

def main():
    log.info("orchestrator listening on :%d  otp_mode=%s", HTTP_PORT, OTP_MODE)
    server = ThreadedHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    server.serve_forever()

if __name__ == "__main__":
    main()
