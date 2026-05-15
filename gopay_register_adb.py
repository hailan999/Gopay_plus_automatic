#!/usr/bin/env python3
"""ADB-driven GoPay registration helper for LDPlayer/Android emulators.

This is intentionally separate from the existing ChatGPT Plus payment flow. It
only drives the GoPay/Gojek app registration screens, waits for SMS OTP from
HeroSMS, and sets a GoPay PIN.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_ADB = Path(r"E:\leidian\LDPlayer9\adb.exe")
LOG_DIR = ROOT / "logs"
STEP_DIR = LOG_DIR / "gopay_register_steps"
USED_OTPS_PATH = LOG_DIR / "herosms_used_otps.json"
PIN_SETUP_PATH = LOG_DIR / "gopay_pin_setup.json"
UI_DUMP_DEVICE_PATH = "/sdcard/window.xml"

CONNECT_PORTS = (5555, 5557, 5559, 5561, 7555)
GOPAY_PACKAGE_CANDIDATES = (
    "com.gojek.gopay",
    "com.gojek.app",
    "com.go-jek.ios",
)


class RegisterError(RuntimeError):
    pass


@dataclass
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def cx(self) -> int:
        return (self.left + self.right) // 2

    @property
    def cy(self) -> int:
        return (self.top + self.bottom) // 2


@dataclass
class UiNode:
    text: str
    desc: str
    klass: str
    bounds: Bounds
    clickable: bool
    enabled: bool

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.text, self.desc) if part).strip()


@dataclass
class UiState:
    xml: str
    nodes: list[UiNode]
    width: int
    height: int

    @property
    def text(self) -> str:
        return "\n".join(node.label for node in self.nodes if node.label)

    def contains(self, *needles: str) -> bool:
        haystack = self.text.lower()
        return any(needle.lower() in haystack for needle in needles)

    def find(
        self,
        *needles: str,
        enabled_only: bool = True,
        exact: bool = False,
    ) -> Optional[UiNode]:
        haystack_needles = [needle.lower() for needle in needles if needle]
        for node in self.nodes:
            if enabled_only and not node.enabled:
                continue
            label = node.label.lower()
            if exact and any(label == needle for needle in haystack_needles):
                return node
            if not exact and any(needle in label for needle in haystack_needles):
                return node
        return None

    def find_edit_text(self, index: int = 0) -> Optional[UiNode]:
        edit_nodes = [node for node in self.nodes if node.klass.endswith("EditText") and node.enabled]
        if 0 <= index < len(edit_nodes):
            return edit_nodes[index]
        return None


def setup_logging(verbose: bool) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("gopay-register-adb")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(stream)

    file_handler = logging.FileHandler(LOG_DIR / "gopay_register_adb.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    return logger


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def redact_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) <= 4:
        return "***"
    return "***" + digits[-4:]


def normalize_indonesia_phone(phone: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        raise RegisterError("phone_number is required")
    if digits.startswith("62"):
        local = digits[2:]
    elif digits.startswith("0"):
        local = digits[1:]
    else:
        local = digits
    if not re.fullmatch(r"\d{8,13}", local):
        raise RegisterError(f"invalid Indonesian local phone length: {redact_phone(local)}")
    return "62" + local, local


def validate_pin(pin: str) -> str:
    pin = str(pin or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        raise RegisterError("pin must be exactly 6 digits")
    return pin


def adb_text(value: str) -> str:
    # Android input text uses %s for spaces and treats some shell chars specially.
    safe = str(value).strip().replace(" ", "%s")
    return re.sub(r"[^A-Za-z0-9@._%+-]", "", safe)


def load_used_otps() -> dict[str, list[str]]:
    if not USED_OTPS_PATH.exists():
        return {}
    try:
        data = json.loads(USED_OTPS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, list[str]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            clean[str(key)] = [str(item) for item in value if re.fullmatch(r"\d{4,8}", str(item))]
    return clean


def save_used_otps(data: dict[str, list[str]]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    USED_OTPS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_used_otp_set(activation_id: str) -> set[str]:
    if not activation_id:
        return set()
    return set(load_used_otps().get(str(activation_id), []))


def remember_used_otp(activation_id: str, code: str) -> None:
    if not activation_id or not code:
        return
    data = load_used_otps()
    key = str(activation_id)
    values = data.setdefault(key, [])
    if code not in values:
        values.append(code)
    data[key] = values[-10:]
    save_used_otps(data)


def load_pin_setup() -> dict[str, dict]:
    if not PIN_SETUP_PATH.exists():
        return {}
    try:
        data = json.loads(PIN_SETUP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def save_pin_setup(data: dict[str, dict]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    PIN_SETUP_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def is_pin_setup_recorded(phone: str) -> bool:
    return bool(phone and load_pin_setup().get(str(phone), {}).get("pin_setup"))


def remember_pin_setup(phone: str, name: str = "") -> None:
    if not phone:
        return
    data = load_pin_setup()
    data[str(phone)] = {
        "pin_setup": True,
        "phone_tail": redact_phone(phone),
        "name": name,
        "updated_at_unix": int(time.time()),
    }
    save_pin_setup(data)


def parse_bounds(raw: str) -> Bounds:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
    if not match:
        return Bounds(0, 0, 0, 0)
    return Bounds(*(int(part) for part in match.groups()))


class Adb:
    def __init__(self, adb_path: Path, device: str, logger: logging.Logger):
        self.adb_path = str(adb_path)
        self.device = device
        self.log = logger

    def raw(self, args: list[str], timeout: int = 20, binary: bool = False) -> subprocess.CompletedProcess:
        cmd = [self.adb_path]
        if self.device and args[:1] != ["connect"]:
            cmd += ["-s", self.device]
        cmd += args
        self.log.debug("adb: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            capture_output=True,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            timeout=timeout,
            check=False,
        )

    def check(self, args: list[str], timeout: int = 20) -> str:
        proc = self.raw(args, timeout=timeout)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            raise RegisterError(f"adb command failed: {' '.join(args)} :: {stderr or stdout}")
        return proc.stdout or ""

    def shell(self, command: str, timeout: int = 20) -> str:
        return self.check(["shell", command], timeout=timeout)

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}", timeout=8)
        time.sleep(0.7)

    def tap_rel(self, state: UiState, rx: float, ry: float) -> None:
        self.tap(int(state.width * rx), int(state.height * ry))

    def text(self, value: str) -> None:
        safe = adb_text(value)
        if not safe:
            raise RegisterError("refusing to input empty/sanitized text")
        self.shell(f"input text {safe}", timeout=10)
        time.sleep(0.5)

    def raw_text(self, value: str) -> None:
        raw = str(value or "").replace(" ", "%s")
        if not raw:
            raise RegisterError("refusing to input empty text")
        self.shell(f"input text {shlex.quote(raw)}", timeout=10)
        time.sleep(0.5)

    def raw_text_chunks(self, value: str, chunk_size: int = 60) -> None:
        text = str(value or "")
        for start in range(0, len(text), chunk_size):
            self.raw_text(text[start : start + chunk_size])

    def clear_text(self) -> None:
        # Select-all is unreliable across custom inputs; repeated DEL is boring but stable.
        for _ in range(24):
            self.keyevent(67)
            time.sleep(0.02)

    def keyevent(self, key: int) -> None:
        self.shell(f"input keyevent {key}", timeout=8)
        time.sleep(0.4)

    def digits(self, value: str) -> None:
        keycodes = {str(i): 7 + i for i in range(10)}
        for digit in str(value):
            if digit not in keycodes:
                continue
            self.keyevent(keycodes[digit])
            time.sleep(0.08)

    def wm_size(self) -> tuple[int, int]:
        out = self.shell("wm size", timeout=8)
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        if not match:
            return 560, 1000
        return int(match.group(1)), int(match.group(2))

    def screenshot(self, path: Path) -> None:
        proc = self.raw(["exec-out", "screencap", "-p"], timeout=20, binary=True)
        if proc.returncode != 0:
            raise RegisterError(f"screenshot failed: {(proc.stderr or b'').decode(errors='ignore')}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(proc.stdout)

    def dump_ui(self) -> UiState:
        width, height = self.wm_size()
        self.shell(f"uiautomator dump {UI_DUMP_DEVICE_PATH} >/dev/null 2>&1", timeout=15)
        xml = self.shell(f"cat {UI_DUMP_DEVICE_PATH}", timeout=15)
        nodes: list[UiNode] = []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise RegisterError(f"uiautomator XML parse failed: {exc}") from exc
        for elem in root.iter("node"):
            text = elem.attrib.get("text", "") or ""
            desc = elem.attrib.get("content-desc", "") or ""
            klass = elem.attrib.get("class", "") or ""
            bounds = parse_bounds(elem.attrib.get("bounds", ""))
            clickable = elem.attrib.get("clickable", "false") == "true"
            enabled = elem.attrib.get("enabled", "true") == "true"
            nodes.append(UiNode(text, desc, klass, bounds, clickable, enabled))
        return UiState(xml=xml, nodes=nodes, width=width, height=height)

    def installed_packages(self) -> list[str]:
        out = self.shell("pm list packages", timeout=15)
        packages = []
        for line in out.splitlines():
            if line.startswith("package:"):
                packages.append(line.split(":", 1)[1].strip())
        return packages

    def start_package(self, package: str) -> None:
        proc = self.raw(
            ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=20,
        )
        if proc.returncode != 0 or "monkey aborted" in (proc.stdout or "").lower():
            raise RegisterError(f"failed to launch package {package}: {proc.stdout or proc.stderr}")
        time.sleep(3)

    def open_url(self, url: str) -> None:
        url = str(url or "").strip()
        if not url:
            return
        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise RegisterError("get_rp_link must be a full http(s) URL, for example https://...")
        self.open_url_like_manual(url)

    def set_clipboard(self, value: str) -> None:
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        self.shell(f"cmd clipboard set '{escaped}'", timeout=10)

    def edit_text_contains(self, expected: str) -> bool:
        expected = str(expected or "").strip()
        if not expected:
            return False
        prefix = expected[: min(len(expected), 24)]
        try:
            state = self.dump_ui()
        except Exception as exc:
            self.log.debug("Cannot verify address bar text: %s", exc)
            return False
        for node in state.nodes:
            if node.klass.endswith("EditText") and (expected in node.text or prefix in node.text):
                return True
        return False

    def open_url_like_manual(self, url: str) -> None:
        self.log.info("Opening URL in Android browser with exact quoted link")
        quoted_url = shlex.quote(url)
        # The LDPlayer stock browser address bar does not accept normal
        # `adb shell input text`, so use the browser component directly. Quoting
        # is important: RP links usually contain '&', which Android shell would
        # otherwise treat as a command separator and silently truncate.
        self.shell(
            "am start -n com.android.browser/.BrowserActivity "
            "-a android.intent.action.VIEW "
            "-c android.intent.category.BROWSABLE "
            f"-d {quoted_url}",
            timeout=20,
        )
        time.sleep(5)


class HeroSmsClient:
    def __init__(self, cfg: dict, logger: logging.Logger):
        otp_cfg = (cfg.get("otp") or {}).get("sms_api") or {}
        self.api_key = str(otp_cfg.get("api_key") or "").strip()
        self.base_url = str(otp_cfg.get("base_url") or "https://hero-sms.com").rstrip("/")
        self.poll_interval = int(otp_cfg.get("poll_interval_sec") or 3)
        self.use_proxy = bool(otp_cfg.get("use_proxy", True))
        self.proxy = str(otp_cfg.get("proxy") or cfg.get("proxy") or "").strip()
        self.log = logger
        self.session = None
        try:
            from curl_cffi import requests as cffi_requests  # type: ignore

            self.session = cffi_requests.Session(impersonate="chrome136")
            if self.use_proxy and self.proxy:
                self.session.proxies = {"http": self.proxy, "https": self.proxy}
            self.log.debug("HeroSMS will use curl_cffi chrome impersonation")
        except Exception as exc:
            self.log.debug("curl_cffi unavailable for HeroSMS, fallback enabled: %s", exc)
        if not self.api_key:
            raise RegisterError("config.json missing otp.sms_api.api_key")

    def _request(self, method: str, url: str, timeout: int = 15) -> str:
        method = method.upper()
        if self.session is not None:
            resp = self.session.request(
                method,
                url,
                headers={
                    "Accept": "text/plain, application/json, */*",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.0.0 Safari/537.36"
                    ),
                },
                timeout=timeout,
            )
            if resp.status_code >= 400:
                raise RegisterError(f"HeroSMS HTTP {resp.status_code}: {resp.text[:160]}")
            return resp.text.strip()

        curl = shutil.which("curl.exe") or shutil.which("curl")
        if curl:
            cmd = [curl, "-s", "-S", "--max-time", str(timeout), "-X", method]
            if self.use_proxy and self.proxy:
                cmd += ["-x", self.proxy]
            cmd += [url]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + 5,
                check=False,
            )
            if proc.returncode != 0:
                raise RegisterError(f"curl failed: {(proc.stderr or proc.stdout).strip()}")
            return (proc.stdout or "").strip()

        handlers = []
        if self.use_proxy and self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        opener = urllib.request.build_opener(*handlers)
        data = b"" if method != "GET" else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "text/plain, application/json"})
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace").strip()

    def _open(self, url: str, timeout: int = 15) -> str:
        return self._request("GET", url, timeout=timeout)

    def poll_otp(
        self,
        activation_id: str,
        timeout_seconds: int,
        used_codes: set[str],
        allow_reused: bool = False,
    ) -> str:
        if not activation_id:
            raise RegisterError("sms_activation_id is required when waiting for OTP")
        url = (
            f"{self.base_url}/stubs/handler_api.php?"
            f"api_key={self.api_key}&action=getStatus&id={activation_id}"
        )
        safe_url = re.sub(r"api_key=[^&]+", "api_key=***", url)
        deadline = time.time() + timeout_seconds
        self.log.info("Polling HeroSMS activation=%s url=%s", activation_id, safe_url)
        while time.time() < deadline:
            try:
                body = self._open(url)
            except (urllib.error.URLError, TimeoutError, OSError, RegisterError) as exc:
                self.log.warning("HeroSMS request failed: %s", exc)
                time.sleep(self.poll_interval)
                continue

            self.log.debug("HeroSMS response: %s", body[:300])
            code = self.extract_code(body)
            if code:
                if code in used_codes and not allow_reused:
                    self.log.info("HeroSMS returned already-used OTP %s; waiting for a newer code", code)
                    time.sleep(self.poll_interval)
                    continue
                used_codes.add(code)
                remember_used_otp(activation_id, code)
                self.log.info("HeroSMS got OTP %s", code)
                return code

            self.log.info("HeroSMS waiting: %s", body[:120] or "<empty>")
            time.sleep(self.poll_interval)

        raise RegisterError(f"timeout waiting for HeroSMS OTP after {timeout_seconds}s")

    def request_extra_sms(self, activation_id: str) -> str:
        if not activation_id:
            raise RegisterError("activation_id is required for request-extra-sms")
        url = f"{self.base_url}/api/v1/activations/{activation_id}/request-extra-sms"
        self.log.info("HeroSMS request-extra-sms activation=%s url=%s", activation_id, url)
        body = self._request("POST", url)
        self.log.info("HeroSMS request-extra-sms response: %s", body[:200] or "<empty>")
        return body

    def request_retry_status(self, activation_id: str) -> str:
        if not activation_id:
            raise RegisterError("activation_id is required for setStatus")
        url = (
            f"{self.base_url}/stubs/handler_api.php?"
            f"action=setStatus&id={activation_id}&status=3&api_key={self.api_key}"
        )
        safe_url = re.sub(r"api_key=[^&]+", "api_key=***", url)
        self.log.info("HeroSMS setStatus=3 activation=%s url=%s", activation_id, safe_url)
        body = self._open(url)
        self.log.info("HeroSMS setStatus=3 response: %s", body[:200] or "<empty>")
        return body

    @staticmethod
    def extract_code(body: str) -> str:
        match = re.search(r"STATUS_OK:(\d{4,8})", body or "")
        if match:
            return match.group(1)
        try:
            data = json.loads(body)
        except Exception:
            data = None
        if isinstance(data, dict):
            values = [
                data.get("smsCode"),
                data.get("code"),
                data.get("otp"),
                data.get("sms"),
                data.get("text"),
                data.get("message"),
            ]
            for value in values:
                match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", str(value or ""))
                if match:
                    return match.group(1)
        match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", body or "")
        return match.group(1) if match else ""


class GoPayRegisterFlow:
    def __init__(
        self,
        adb: Adb,
        sms: Optional[HeroSmsClient],
        args: argparse.Namespace,
        logger: logging.Logger,
    ):
        self.adb = adb
        self.sms = sms
        self.args = args
        self.log = logger
        self.used_otps: set[str] = load_used_otp_set(args.sms_activation_id)
        if args.used_otp:
            self.used_otps.add(args.used_otp)
        self.did_phone = False
        self.did_name = False
        self.pin_entries = 0
        self.step_index = 0

    def save_observation(self, state: UiState, reason: str) -> None:
        STEP_DIR.mkdir(parents=True, exist_ok=True)
        self.step_index += 1
        stem = f"{self.step_index:03d}_{re.sub(r'[^A-Za-z0-9_-]+', '_', reason)[:40]}"
        png = STEP_DIR / f"{stem}.png"
        xml = STEP_DIR / f"{stem}.xml"
        try:
            self.adb.screenshot(png)
        except Exception as exc:
            self.log.debug("Failed to save screenshot: %s", exc)
        xml.write_text(state.xml, encoding="utf-8")

    def tap_text(self, state: UiState, *labels: str) -> bool:
        node = state.find(*labels)
        if not node:
            return False
        self.log.info("Tap '%s' at %s,%s", node.label, node.bounds.cx, node.bounds.cy)
        self.adb.tap(node.bounds.cx, node.bounds.cy)
        return True

    def tap_exact_text(self, state: UiState, *labels: str) -> bool:
        node = state.find(*labels, exact=True)
        if not node:
            return False
        self.log.info("Tap '%s' at %s,%s", node.label, node.bounds.cx, node.bounds.cy)
        self.adb.tap(node.bounds.cx, node.bounds.cy)
        return True

    def tap_row_by_text(self, state: UiState, label: str, x_ratio: float = 0.82) -> bool:
        node = state.find(label)
        if not node:
            return False
        x = int(state.width * x_ratio)
        y = node.bounds.cy
        self.log.info("Tap row '%s' at %s,%s", node.label, x, y)
        self.adb.tap(x, y)
        return True

    def input_phone(self, state: UiState) -> None:
        self.log.info("Input phone %s", redact_phone(self.args.phone_number))
        field = state.find_edit_text()
        if field:
            self.adb.tap(field.bounds.cx, field.bounds.cy)
        else:
            self.adb.tap_rel(state, 0.43, 0.33)
        self.adb.text(self.args.local_phone)
        time.sleep(0.8)
        next_state = self.adb.dump_ui()
        if self.tap_exact_text(next_state, "Continue"):
            self.did_phone = True
            return
        disabled_continue = next_state.find("Continue", enabled_only=False, exact=True)
        if disabled_continue and not disabled_continue.enabled:
            raise RegisterError(
                "phone input did not enable Continue; check whether the field accepted the number"
            )
        raise RegisterError("Continue button not found after phone input")
        self.did_phone = True

    def input_name(self, state: UiState) -> None:
        self.log.info("Input account name")
        field = state.find_edit_text()
        if field:
            self.log.info("Focus name EditText at %s,%s", field.bounds.cx, field.bounds.cy)
            self.adb.tap(field.bounds.cx, field.bounds.cy)
            self.adb.tap(field.bounds.cx, field.bounds.cy)
        else:
            self.log.info("Focus name fallback area")
            self.adb.tap_rel(state, 0.18, 0.31)
        self.adb.clear_text()
        self.adb.text(self.args.name)
        self.adb.keyevent(66)
        time.sleep(0.8)
        next_state = self.adb.dump_ui()
        if self.tap_exact_text(next_state, "Create account"):
            self.did_name = True
            return
        disabled_create = next_state.find("Create account", enabled_only=False, exact=True)
        if disabled_create and not disabled_create.enabled:
            raise RegisterError(
                "name input did not enable Create account; check whether the field accepted the name"
            )
        raise RegisterError("Create account button not found after name input")
        self.did_name = True

    def input_otp(self, state: UiState) -> None:
        if self.args.dry_run:
            self.log.info("Dry run: OTP page detected, not polling or typing OTP")
            raise RegisterError("dry-run stopped at OTP page")
        if not self.sms:
            raise RegisterError("SMS client is not configured")
        code = self.sms.poll_otp(
            self.args.sms_activation_id,
            timeout_seconds=self.args.otp_timeout,
            used_codes=self.used_otps,
            allow_reused=self.args.allow_reused_otp,
        )
        self.log.info("Input OTP %s", code)
        otp_node = state.find_edit_text()
        if otp_node:
            self.log.info("Focus OTP EditText at %s,%s", otp_node.bounds.cx, otp_node.bounds.cy)
            self.adb.tap(otp_node.bounds.cx, otp_node.bounds.cy)
        else:
            self.log.info("Focus OTP fallback area")
            self.adb.tap_rel(state, 0.14, 0.34)
        self.adb.digits(code)
        time.sleep(2)

    def input_pin(self, state: UiState) -> None:
        if self.args.dry_run:
            self.log.info("Dry run: PIN page detected, not typing PIN")
            raise RegisterError("dry-run stopped at PIN page")
        self.pin_entries += 1
        self.log.info("Input PIN entry %d", self.pin_entries)
        for digit in self.args.pin:
            self.tap_pin_digit(state, digit)
        time.sleep(0.8)
        next_state = self.adb.dump_ui()
        if not self.tap_text(next_state, "Continue", "Save"):
            # Continue/Save is centered above the keypad in the reference images.
            self.adb.tap_rel(next_state, 0.50, 0.46)

    def tap_pin_digit(self, state: UiState, digit: str) -> None:
        # Relative keypad positions from gopay-steps/14.png and 15.png.
        positions = {
            "1": (0.165, 0.575),
            "2": (0.500, 0.575),
            "3": (0.840, 0.575),
            "4": (0.165, 0.700),
            "5": (0.500, 0.700),
            "6": (0.840, 0.700),
            "7": (0.165, 0.820),
            "8": (0.500, 0.820),
            "9": (0.840, 0.820),
            "0": (0.500, 0.945),
        }
        rx, ry = positions[digit]
        self.adb.tap_rel(state, rx, ry)
        time.sleep(0.15)

    def choose_sms_method(self, state: UiState) -> None:
        if self.tap_text(state, "OTP via SMS"):
            return
        self.adb.tap_rel(state, 0.48, 0.46)

    def handle_home_or_profile(self, state: UiState) -> None:
        if state.contains("Account & safety", "Account protection"):
            if self.tap_text(state, "Strengthen your protection now"):
                return
            if self.tap_text(state, "Account & app settings"):
                return
        if state.contains("0/4 actions completed", "Maximize your security"):
            if self.tap_text(state, "Create PIN"):
                return
            self.log.info("Create PIN not visible; scrolling protection page")
            self.adb.shell("input swipe 280 850 280 420 500")
            time.sleep(1)
            return
        if state.contains("Top up", "Withdraw", "Home", "Profile", "QRIS"):
            if self.tap_exact_text(state, "Profile") or self.tap_text(state, "Profile"):
                return
            self.log.info("Profile tab not found by text; tapping bottom-right tab")
            self.adb.tap_rel(state, 0.90, 0.94)
            return

    def is_pin_keypad_screen(self, state: UiState) -> bool:
        if state.contains("Confirm PIN"):
            return True
        if not state.contains("Create PIN"):
            return False
        labels = {node.label.strip() for node in state.nodes if node.label.strip()}
        digit_count = sum(1 for digit in "0123456789" if digit in labels)
        return digit_count >= 8

    def maybe_start_language_flow(self, state: UiState) -> bool:
        if state.contains("Bahasa Indonesia") and not state.contains("English"):
            if self.tap_text(state, "Bahasa Indonesia"):
                return True
        if state.contains("Pilihan bahasa", "Mau pakai bahasa"):
            if self.tap_text(state, "English"):
                return True
        if state.contains("Cheapest pulsa", "Enter your phone number"):
            if self.tap_text(state, "Enter your phone number"):
                return True
        if state.contains("Masukkan nomor"):
            self.adb.tap_rel(state, 0.50, 0.95)
            return True
        return False

    def finish_after_pin_success(self) -> None:
        remember_pin_setup(self.args.full_phone, self.args.name)
        self.log.info("GoPay PIN updated successfully")
        if self.args.get_rp_link:
            self.log.info("Opening config gopay.get_rp_link in emulator browser")
            self.adb.open_url(self.args.get_rp_link)

    def run(self) -> None:
        deadline = time.time() + self.args.flow_timeout
        while time.time() < deadline:
            state = self.adb.dump_ui()
            brief = first_interesting_line(state.text)
            self.log.info("Current screen: %s", brief)
            self.save_observation(state, classify_state(state))

            if state.contains("successfully updated your GoPay PIN"):
                self.tap_text(state, "Got it")
                self.finish_after_pin_success()
                return

            if self.maybe_start_language_flow(state):
                continue

            if state.contains("Welcome to GoPay"):
                if not self.did_phone:
                    self.input_phone(state)
                else:
                    self.tap_exact_text(state, "Continue")
                continue

            if state.contains("Important before you proceed"):
                if not self.tap_exact_text(state, "Continue"):
                    self.adb.tap_rel(state, 0.50, 0.94)
                continue

            if state.contains("Choose verification method"):
                self.choose_sms_method(state)
                continue

            if state.contains("Check WhatsApp for OTP"):
                if self.tap_text(state, "Try another method"):
                    continue
                self.input_otp(state)
                continue

            if state.contains("Enter OTP sent via SMS", "OTP sent via SMS"):
                self.input_otp(state)
                continue

            if state.contains("Fill out a few details"):
                if not self.did_name:
                    self.input_name(state)
                else:
                    self.tap_text(state, "Create account")
                continue

            if state.contains("Received from", "Open before someone else"):
                if not self.tap_text(state, "Open gift"):
                    self.adb.tap_rel(state, 0.50, 0.95)
                continue

            if state.contains("sent you", "Share happiness", "Make it festive"):
                # These screens are post-registration gift prompts. Go back/home
                # until the bottom navigation is visible.
                self.adb.keyevent(4)
                continue

            if self.is_pin_keypad_screen(state):
                self.input_pin(state)
                continue

            if state.contains("0/4 actions completed", "Maximize your security"):
                if self.tap_row_by_text(state, "Create PIN"):
                    continue
                self.log.info("Create PIN not visible; scrolling protection page")
                self.adb.shell("input swipe 280 860 280 520 500")
                time.sleep(1)
                continue

            if state.contains("There's a technical error"):
                self.log.info("GoPay technical error/429 detected; waiting 5s before retry")
                time.sleep(5)
                if self.tap_text(state, "Try again", "Retry"):
                    continue
                self.adb.keyevent(4)
                continue

            if state.contains("error", "failed", "try again later"):
                raise RegisterError(f"GoPay showed an error screen: {brief}")

            before = time.time()
            self.handle_home_or_profile(state)
            if time.time() - before < 0.2:
                self.log.info("No known action for this screen; waiting")
                time.sleep(2)

        raise RegisterError(f"flow timeout after {self.args.flow_timeout}s")


def first_interesting_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return "<no visible text>"


def classify_state(state: UiState) -> str:
    checks = [
        ("success", ("successfully updated",)),
        ("otp_sms", ("Enter OTP sent via SMS", "OTP sent via SMS")),
        ("otp_whatsapp", ("Check WhatsApp for OTP",)),
        ("verification_method", ("Choose verification method",)),
        ("privacy_consent", ("Important before you proceed",)),
        ("welcome", ("Welcome to GoPay",)),
        ("details", ("Fill out a few details",)),
        ("create_pin", ("Create PIN",)),
        ("confirm_pin", ("Confirm PIN",)),
        ("profile", ("Account & safety", "Account protection")),
        ("home", ("Top up", "Withdraw")),
        ("gift", ("Received from", "Open gift", "Make it festive")),
        ("onboarding", ("Enter your phone number", "Bahasa Indonesia", "Cheapest pulsa")),
    ]
    for name, needles in checks:
        if state.contains(*needles):
            return name
    return "unknown"


def find_adb_path(value: str) -> Path:
    candidates: list[Path] = []
    if value:
        candidates.append(Path(value))
    candidates.append(DEFAULT_ADB)
    env_path = os.environ.get("PATH", "")
    exe = "adb.exe" if os.name == "nt" else "adb"
    for folder in env_path.split(os.pathsep):
        if folder:
            candidates.append(Path(folder) / exe)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RegisterError("adb not found; pass --adb-path or install Android platform-tools")


def adb_without_device(adb_path: Path, logger: logging.Logger) -> Adb:
    return Adb(adb_path, "", logger)


def parse_adb_devices(output: str) -> list[str]:
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def connect_device(adb_path: Path, requested: str, logger: logging.Logger) -> str:
    base = adb_without_device(adb_path, logger)
    if requested:
        logger.info("Using requested ADB device %s", requested)
        return requested

    out = base.check(["devices"], timeout=15)
    devices = parse_adb_devices(out)
    if devices:
        logger.info("Using connected ADB device %s", devices[0])
        return devices[0]

    for port in CONNECT_PORTS:
        target = f"127.0.0.1:{port}"
        logger.info("Trying adb connect %s", target)
        base.raw(["connect", target], timeout=10)
        time.sleep(0.8)
        out = base.check(["devices"], timeout=15)
        devices = parse_adb_devices(out)
        if devices:
            logger.info("Connected ADB device %s", devices[0])
            return devices[0]

    raise RegisterError(
        "No ADB device connected. Open LDPlayer, enable ADB debugging, then retry. "
        f"Tried ports: {', '.join(str(p) for p in CONNECT_PORTS)}"
    )


def choose_gopay_package(adb: Adb, requested: str, logger: logging.Logger) -> str:
    if requested:
        return requested
    packages = adb.installed_packages()
    for candidate in GOPAY_PACKAGE_CANDIDATES:
        if candidate in packages:
            logger.info("Detected GoPay package %s", candidate)
            return candidate
    fuzzy = [pkg for pkg in packages if "gopay" in pkg.lower() or "gojek" in pkg.lower()]
    if fuzzy:
        logger.info("Detected possible GoPay package %s", fuzzy[0])
        return fuzzy[0]
    raise RegisterError("GoPay/Gojek package not found. Install GoPay in the emulator or pass --package")


def build_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive GoPay registration in an Android emulator via ADB.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--phone-number", default="", help="Indonesian GoPay phone number")
    parser.add_argument("--sms-activation-id", default="", help="HeroSMS activation/order id")
    parser.add_argument("--pin", default="", help="6-digit GoPay PIN to create")
    parser.add_argument("--name", default="", help="Registration name")
    parser.add_argument("--get-rp-link", default="", help="URL to open in emulator browser after PIN is set")
    parser.add_argument("--adb-path", default="", help=rf"ADB path; default {DEFAULT_ADB}")
    parser.add_argument("--device", default="", help="ADB device serial; optional")
    parser.add_argument("--package", default="", help="GoPay/Gojek Android package; optional")
    parser.add_argument("--flow-timeout", type=int, default=600, help="Whole flow timeout seconds")
    parser.add_argument("--otp-timeout", type=int, default=180, help="HeroSMS OTP timeout seconds")
    parser.add_argument("--allow-reused-otp", action="store_true", help="Allow using a previously seen OTP")
    parser.add_argument("--used-otp", default="", help="Treat this code as already used and wait for a newer one")
    parser.add_argument("--dry-run", action="store_true", help="Connect, launch, screenshot, and classify only")
    parser.add_argument("--test-otp", action="store_true", help="Only poll HeroSMS for the activation id")
    parser.add_argument("--test-next-otp", action="store_true", help="Only poll HeroSMS, ignoring used/local remembered OTPs")
    parser.add_argument("--request-extra-sms", action="store_true", help="POST HeroSMS /api/v1/activations/{id}/request-extra-sms")
    parser.add_argument("--request-retry-status", action="store_true", help="GET HeroSMS setStatus status=3")
    parser.add_argument("--mark-pin-setup", action="store_true", help="Record this phone as already having a GoPay PIN")
    parser.add_argument("--force-pin-setup", action="store_true", help="Run PIN setup even if this phone is recorded as done")
    parser.add_argument("--open-rp-link-only", action="store_true", help="Only open get_rp_link in the emulator")
    parser.add_argument("--input-test", default="", help="Only type this value into the currently focused field")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def enrich_args(args: argparse.Namespace, cfg: dict) -> argparse.Namespace:
    gopay_cfg = cfg.get("gopay") or {}
    args.name = (args.name or str(gopay_cfg.get("name") or "smith")).strip()
    if not args.name:
        args.name = "smith"
    args.get_rp_link = (args.get_rp_link or str(gopay_cfg.get("get_rp_link") or "")).strip()

    phone = args.phone_number or str(gopay_cfg.get("phone_number") or "")
    pin = args.pin or str(gopay_cfg.get("pin") or "")

    if args.test_otp or args.test_next_otp or args.request_extra_sms or args.request_retry_status:
        args.phone_number = phone
        args.pin = pin
        args.full_phone = phone
        args.local_phone = phone
        return args

    if args.dry_run or args.input_test:
        args.phone_number = phone
        args.pin = validate_pin(pin) if pin else ""
        if phone:
            full_phone, local_phone = normalize_indonesia_phone(phone)
            args.full_phone = full_phone
            args.local_phone = local_phone
        else:
            args.full_phone = ""
            args.local_phone = ""
        return args

    args.phone_number = phone
    args.pin = validate_pin(pin)
    full_phone, local_phone = normalize_indonesia_phone(args.phone_number)
    args.full_phone = full_phone
    args.local_phone = local_phone
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = build_args(argv)
    log = setup_logging(args.verbose)
    try:
        cfg = load_config(Path(args.config))
        args = enrich_args(args, cfg)
        adb_path = find_adb_path(args.adb_path)
        log.info("Using adb: %s", adb_path)
        log.info("Phone=%s name=%s", redact_phone(args.full_phone), args.name)

        if args.mark_pin_setup:
            if not args.full_phone:
                raise RegisterError("--phone-number is required for --mark-pin-setup")
            remember_pin_setup(args.full_phone, args.name)
            log.info("Recorded PIN setup for phone=%s", redact_phone(args.full_phone))
            return 0

        if args.open_rp_link_only:
            if not args.get_rp_link:
                raise RegisterError("get_rp_link is empty; set config.json gopay.get_rp_link or pass --get-rp-link")
            device = connect_device(adb_path, args.device, log)
            adb = Adb(adb_path, device, log)
            log.info("Opening get_rp_link in emulator browser")
            adb.open_url(args.get_rp_link)
            return 0

        if (
            args.full_phone
            and not args.force_pin_setup
            and not args.test_otp
            and not args.test_next_otp
            and not args.request_extra_sms
            and not args.request_retry_status
            and not args.dry_run
            and not args.input_test
            and not args.open_rp_link_only
            and is_pin_setup_recorded(args.full_phone)
        ):
            log.info(
                "PIN setup already recorded for phone=%s; skipping Profile/PIN steps",
                redact_phone(args.full_phone),
            )
            if args.get_rp_link:
                device = connect_device(adb_path, args.device, log)
                adb = Adb(adb_path, device, log)
                log.info("Opening get_rp_link in emulator browser")
                adb.open_url(args.get_rp_link)
            return 0

        sms = None
        if args.test_otp or args.test_next_otp or args.request_extra_sms or args.request_retry_status or not args.dry_run:
            sms = HeroSmsClient(cfg, log)

        if args.request_extra_sms or args.request_retry_status:
            if not args.sms_activation_id:
                raise RegisterError("--sms-activation-id is required for HeroSMS action")
            if args.request_extra_sms:
                sms.request_extra_sms(args.sms_activation_id)
            if args.request_retry_status:
                sms.request_retry_status(args.sms_activation_id)
            return 0

        if args.test_otp or args.test_next_otp:
            if not args.sms_activation_id:
                raise RegisterError("--sms-activation-id is required for OTP test")
            used = set() if args.test_otp else load_used_otp_set(args.sms_activation_id)
            if args.used_otp:
                used.add(args.used_otp)
            code = sms.poll_otp(
                args.sms_activation_id,
                args.otp_timeout,
                used,
                allow_reused=args.allow_reused_otp or not used,
            )
            log.info("OTP test succeeded: %s", code)
            return 0

        device = connect_device(adb_path, args.device, log)
        adb = Adb(adb_path, device, log)

        if args.input_test:
            log.info("Typing input-test value into the current focused field")
            adb.text(args.input_test)
            return 0

        package = choose_gopay_package(adb, args.package, log)
        log.info("Launching %s", package)
        adb.start_package(package)

        if args.dry_run:
            state = adb.dump_ui()
            STEP_DIR.mkdir(parents=True, exist_ok=True)
            adb.screenshot(STEP_DIR / "dry_run_current.png")
            (STEP_DIR / "dry_run_current.xml").write_text(state.xml, encoding="utf-8")
            log.info("Dry-run screen classification: %s", classify_state(state))
            log.info("First visible text: %s", first_interesting_line(state.text))
            return 0

        flow = GoPayRegisterFlow(adb, sms, args, log)
        flow.run()
        return 0
    except RegisterError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
