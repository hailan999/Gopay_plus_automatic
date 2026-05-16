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
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_ADB = Path(r"E:\leidian\LDPlayer9\adb.exe")
DEFAULT_LD_DIR = Path(r"E:\leidian\LDPlayer9")
DEFAULT_MT_APK = Path(r"C:\Users\Administrator\Downloads\MT2.26.4.apk")
DEFAULT_GOPAY_APKS = Path(r"C:\Users\Administrator\Downloads\GoPay_2.7.0.apks")
LOG_DIR = ROOT / "logs"
STEP_DIR = LOG_DIR / "gopay_register_steps"
USED_OTPS_PATH = LOG_DIR / "herosms_used_otps.json"
PIN_SETUP_PATH = LOG_DIR / "gopay_pin_setup.json"
UNUSABLE_NUMBERS_PATH = LOG_DIR / "gopay_unusable_numbers.json"
NUMBER_USAGE_PATH = LOG_DIR / "gopay_number_usage.json"
DEVICE_EXCEPTIONS_PATH = LOG_DIR / "gopay_device_exceptions.json"
UI_DUMP_DEVICE_PATH = "/sdcard/window.xml"
DEFAULT_WEBUI_DB = Path(r"E:\development\git_projects\Gpt-Agreement-Payment\output\webui.db")
# Temporary: skip receiving gifts, but still continue to the subscribe step after PIN setup.
RECEIVE_GIFT_AFTER_PIN = False

CONNECT_PORTS = (5555, 5557, 5559, 5561, 7555)
GOPAY_PACKAGE_CANDIDATES = (
    "com.gojek.gopay",
    "com.gojek.app",
    "com.go-jek.ios",
)
REGISTER_EXCEPTIONS = (
    {
        "name": "phone_already_registered",
        "needles": (
            "Other ways to log in",
            "This is not my account",
            "create a new account",
        ),
        "reason": "phone appears to be registered already",
        "action": "mark_phone_unusable",
    },
    {
        "name": "phone_already_registered_pin_login",
        "needles": (
            "Enter your PIN",
            "Enter your GoPay PIN to log in",
            "No OTP required",
        ),
        "reason": "phone appears to be registered already; GoPay asked for login PIN",
        "action": "mark_phone_unusable",
    },
    {
        "name": "phone_already_registered_whatsapp_only",
        "needles": (
            "Check WhatsApp for OTP",
            "Open WhatsApp",
            "Login or signup issues?",
        ),
        "reason": "phone appears to be registered already; GoPay only offered WhatsApp OTP and no SMS switch",
        "action": "mark_phone_unusable",
    },
    {
        "name": "device_login_cooldown",
        "needles": (
            "Try logging in after 12 hours",
        ),
        "reason": "device login cooldown; switch emulator/device",
        "action": "mark_device_unusable",
    },
)


class RegisterError(RuntimeError):
    pass


class OtpTimeoutError(RegisterError):
    pass


class TransientHeroSmsError(RegisterError):
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

    def contains_all(self, *needles: str) -> bool:
        haystack = self.text.lower()
        return all(needle.lower() in haystack for needle in needles if needle)

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


def config_string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def protected_emulators_from_config(gopay_cfg: dict) -> dict[str, list[str]]:
    protected = gopay_cfg.get("protected_emulators") or gopay_cfg.get("protected_emulator") or {}
    if isinstance(protected, list):
        return {"names": [], "indexes": [], "devices": config_string_list(protected)}
    if not isinstance(protected, dict):
        return {"names": [], "indexes": [], "devices": config_string_list(protected)}
    names = config_string_list(protected.get("names") or protected.get("name"))
    indexes = config_string_list(protected.get("indexes") or protected.get("index"))
    devices = config_string_list(
        protected.get("devices")
        or protected.get("device")
        or protected.get("serials")
        or protected.get("serial")
    )
    return {"names": names, "indexes": indexes, "devices": devices}


def protected_device_serials(args: argparse.Namespace) -> set[str]:
    devices = {str(item).strip().lower() for item in getattr(args, "protected_devices", []) if str(item).strip()}
    for index in getattr(args, "protected_indexes", []):
        text = str(index).strip()
        if text.isdigit():
            devices.add(f"emulator-{5554 + int(text) * 2}".lower())
    return devices


def is_protected_device(device: str, protected_devices: set[str]) -> bool:
    return str(device or "").strip().lower() in protected_devices


def filter_protected_devices(
    devices: list[str],
    protected_devices: set[str],
    logger: logging.Logger,
) -> list[str]:
    safe = []
    for device in devices:
        if is_protected_device(device, protected_devices):
            logger.warning("Skipping protected ADB device %s", device)
        else:
            safe.append(device)
    return safe


def assert_prepare_target_not_protected(args: argparse.Namespace) -> None:
    protected_indexes = {str(item).strip() for item in getattr(args, "protected_indexes", []) if str(item).strip()}
    protected_names = {
        str(item).strip().lower()
        for item in getattr(args, "protected_names", [])
        if str(item).strip()
    }
    if args.prepare_index and str(args.prepare_index).strip() in protected_indexes:
        raise RegisterError(
            f"refusing to prepare protected LDPlayer index={args.prepare_index}; "
            "remove it from gopay.protected_emulators only if you really want to use it"
        )
    if args.prepare_name and str(args.prepare_name).strip().lower() in protected_names:
        raise RegisterError(
            f"refusing to prepare protected LDPlayer name={args.prepare_name}; "
            "choose a different gopay.prepare_emulator.name"
        )


def redact_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) <= 4:
        return "***"
    return "***" + digits[-4:]


def _redact_token(value: str) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return "***" if value else ""
    return f"{value[:6]}...{value[-4:]}"


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


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


def random_registration_name() -> str:
    first_names = (
        "Aaron",
        "Adrian",
        "Alan",
        "Andre",
        "Brian",
        "Calvin",
        "Daniel",
        "Darren",
        "Evan",
        "Felix",
        "Gavin",
        "Henry",
        "Ivan",
        "Jaime",
        "Kevin",
        "Leon",
        "Marcus",
        "Nolan",
        "Oscar",
        "Ryan",
        "Simon",
        "Victor",
    )
    last_names = (
        "Adams",
        "Baker",
        "Brown",
        "Clark",
        "Davis",
        "Evans",
        "Foster",
        "Gray",
        "Harris",
        "King",
        "Lewis",
        "Miller",
        "Morgan",
        "Parker",
        "Reed",
        "Smith",
        "Taylor",
        "Walker",
        "Young",
    )
    return f"{random.choice(first_names)} {random.choice(last_names)}"


def normalize_herosms_country_id(value: str) -> str:
    value = str(value or "").strip()
    if value.isdigit():
        return value
    aliases = {
        "id": "6",
        "indonesia": "6",
    }
    return aliases.get(value.lower(), value)


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
    if not phone:
        return False
    entry = load_pin_setup().get(str(phone), {})
    if not entry.get("pin_setup"):
        return False
    # Older versions could mark PIN complete from the security score page.
    # Only trust records written by an explicit success screen or a manual mark.
    return str(entry.get("source") or "") in {"gopay_success_text", "manual"}


def remember_pin_setup(phone: str, name: str = "", source: str = "gopay_success_text") -> None:
    if not phone:
        return
    data = load_pin_setup()
    data[str(phone)] = {
        "pin_setup": True,
        "phone_tail": redact_phone(phone),
        "name": name,
        "source": source,
        "updated_at_unix": int(time.time()),
    }
    save_pin_setup(data)


def load_unusable_numbers() -> dict[str, dict]:
    if not UNUSABLE_NUMBERS_PATH.exists():
        return {}
    try:
        data = json.loads(UNUSABLE_NUMBERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def save_unusable_numbers(data: dict[str, dict]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    UNUSABLE_NUMBERS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def remember_unusable_number(
    phone: str,
    reason: str,
    activation_id: str = "",
    screen: str = "",
) -> None:
    if not phone:
        return
    data = load_unusable_numbers()
    data[str(phone)] = {
        "usable": False,
        "reason": reason,
        "phone_tail": redact_phone(phone),
        "sms_activation_id": str(activation_id or ""),
        "screen": screen[:500],
        "updated_at_unix": int(time.time()),
    }
    save_unusable_numbers(data)


def load_number_usage() -> dict[str, dict]:
    if not NUMBER_USAGE_PATH.exists():
        return {}
    try:
        data = json.loads(NUMBER_USAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def save_number_usage(data: dict[str, dict]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    NUMBER_USAGE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def remember_number_usage(
    phone: str,
    status: str,
    activation_id: str = "",
    name: str = "",
    detail: str = "",
) -> None:
    if not phone:
        return
    now = int(time.time())
    key = str(phone)
    data = load_number_usage()
    entry = data.setdefault(
        key,
        {
            "phone_tail": redact_phone(phone),
            "history": [],
        },
    )
    event = {
        "status": status,
        "sms_activation_id": str(activation_id or ""),
        "name": name,
        "detail": detail[:500],
        "updated_at_unix": now,
    }
    history = entry.setdefault("history", [])
    if isinstance(history, list):
        history.append(event)
        entry["history"] = history[-50:]
    else:
        entry["history"] = [event]
    entry["phone_tail"] = redact_phone(phone)
    entry["last_status"] = status
    entry["last_sms_activation_id"] = str(activation_id or "")
    entry["last_updated_at_unix"] = now
    data[key] = entry
    save_number_usage(data)


def load_device_exceptions() -> dict[str, list[dict]]:
    if not DEVICE_EXCEPTIONS_PATH.exists():
        return {}
    try:
        data = json.loads(DEVICE_EXCEPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, list[dict]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            clean[str(key)] = [item for item in value if isinstance(item, dict)]
    return clean


def save_device_exceptions(data: dict[str, list[dict]]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    DEVICE_EXCEPTIONS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def remember_device_exception(device: str, reason: str, phone: str = "", screen: str = "") -> None:
    key = str(device or "unknown")
    data = load_device_exceptions()
    values = data.setdefault(key, [])
    values.append(
        {
            "reason": reason,
            "phone_tail": redact_phone(phone),
            "screen": screen[:500],
            "updated_at_unix": int(time.time()),
        }
    )
    data[key] = values[-50:]
    save_device_exceptions(data)


def wait_and_tap_open_gift(adb: "Adb", logger: logging.Logger, timeout_seconds: int = 45) -> bool:
    deadline = time.time() + timeout_seconds
    logger.info("Waiting for GoPay gift page after RP link")
    while time.time() < deadline:
        try:
            state = adb.dump_ui()
        except Exception as exc:
            logger.debug("Gift-page check failed: %s", exc)
            time.sleep(2)
            continue
        if state.contains("Open gift", "Received from", "Open before someone else"):
            node = state.find("Open gift")
            if node:
                logger.info("Tap 'Open gift' at %s,%s", node.bounds.cx, node.bounds.cy)
                adb.tap(node.bounds.cx, node.bounds.cy)
            else:
                logger.info("Tap Open gift fallback button")
                adb.tap_rel(state, 0.50, 0.935)
            time.sleep(2)
            return True
        time.sleep(2)
    logger.warning("Open gift button not found after opening RP link")
    return False


def webui_db_path(value: str = "") -> Path:
    if value:
        return Path(value)
    data_dir = os.environ.get("WEBUI_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "webui.db"
    return DEFAULT_WEBUI_DB


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not sqlite_table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def get_paid_or_consumed_emails(conn: sqlite3.Connection) -> set[str]:
    consumed: set[str] = set()
    for table in ("pipeline_results", "card_results"):
        cols = sqlite_columns(conn, table)
        if "email" not in cols:
            continue
        status_cols = [
            col for col in (
                "payment_status",
                "payment_result_status",
                "result_status",
                "status",
                "payment_result",
                "result",
            )
            if col in cols
        ]
        error_cols = [
            col for col in ("error", "error_text", "error_message", "message", "detail")
            if col in cols
        ]
        select_cols = ["email"] + status_cols + error_cols
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM {table} WHERE email IS NOT NULL"
        ).fetchall()
        for row in rows:
            data = dict(zip(select_cols, row))
            email = normalize_email(str(data.get("email") or ""))
            if not email:
                continue
            statuses = [str(data.get(col) or "").strip().lower() for col in status_cols]
            errors = [str(data.get(col) or "") for col in error_cols]
            if any(status == "succeeded" for status in statuses):
                consumed.add(email)
            elif any("user is already paid" in error.lower() for error in errors):
                consumed.add(email)
    return consumed


def claim_registered_account_for_pay_only(
    db_path: Path,
    target_email: str = "",
    logger: Optional[logging.Logger] = None,
) -> Optional[dict]:
    if not db_path.exists():
        raise RegisterError(f"webui db not found: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        if not sqlite_table_exists(conn, "registered_accounts"):
            raise RegisterError(f"registered_accounts table not found in {db_path}")
        consumed = get_paid_or_consumed_emails(conn)
        conn.execute("BEGIN IMMEDIATE")
        cols = sqlite_columns(conn, "registered_accounts")
        wanted = [
            "id",
            "email",
            "ts",
            "password",
            "session_token",
            "access_token",
            "device_id",
            "csrf_token",
            "id_token",
            "refresh_token",
            "cookie_header",
            "proxy_add",
            "hot_client",
            "hot_rt",
            "status",
            "last_check_at",
            "last_check_status",
            "last_check_message",
        ]
        select_cols = [col for col in wanted if col in cols]
        if "id" not in select_cols or "email" not in select_cols:
            raise RegisterError("registered_accounts missing required id/email columns")
        where = ""
        params: list[str] = []
        if target_email:
            where = "WHERE lower(email) = lower(?)"
            params.append(target_email)
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM registered_accounts {where} ORDER BY id DESC",
            params,
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            data = {key: row[key] for key in row.keys()}
            email = normalize_email(str(data.get("email") or ""))
            if not email or email in seen:
                continue
            seen.add(email)
            if email in consumed:
                continue
            status = str(data.get("status") or "INITIAL").upper()
            if status != "INITIAL":
                continue
            if not str(data.get("session_token") or "").strip() and not str(data.get("access_token") or "").strip():
                continue
            updated = conn.execute(
                "UPDATE registered_accounts SET status = 'PROCESSING' "
                "WHERE id = ? AND upper(coalesce(status, 'INITIAL')) = 'INITIAL'",
                (data["id"],),
            )
            if updated.rowcount == 0:
                continue
            claimed = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM registered_accounts WHERE id = ?",
                (data["id"],),
            ).fetchone()
            conn.commit()
            out = {key: claimed[key] for key in claimed.keys()}
            if logger:
                logger.info(
                    "Claimed ChatGPT account id=%s email=%s session=%s access=%s device=%s proxy=%s",
                    out.get("id"),
                    email,
                    _redact_token(str(out.get("session_token") or "")),
                    _redact_token(str(out.get("access_token") or "")),
                    _redact_token(str(out.get("device_id") or "")),
                    "yes" if out.get("proxy_add") else "no",
                )
            return out
        conn.commit()
        return None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def build_claimed_auth_payload(account: dict) -> str:
    payload = {
        "mode": "access_token",
        "prefer_session_refresh": True,
        "session_token": str(account.get("session_token") or ""),
        "access_token": str(account.get("access_token") or ""),
        "device_id": str(account.get("device_id") or ""),
        "cookie_header": str(account.get("cookie_header") or ""),
        "refresh_token": str(account.get("refresh_token") or ""),
        "email": str(account.get("email") or ""),
        "account_id": account.get("id"),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def post_subscribe_after_gift(
    args: argparse.Namespace,
    logger: logging.Logger,
    *,
    wait_response: bool = True,
) -> None:
    if not getattr(args, "post_gift_subscribe", False):
        return
    url = str(getattr(args, "post_gift_subscribe_url", "") or "").strip()
    if not url:
        return
    claimed_account = None
    session_token = str(getattr(args, "post_gift_session_token", "") or "").strip()
    if getattr(args, "claim_account_for_pay", True):
        try:
            db_path = webui_db_path(getattr(args, "webui_db_path", ""))
            if getattr(args, "test_post_gift_subscribe", False):
                LOG_DIR.mkdir(exist_ok=True)
                copy_path = LOG_DIR / "webui_subscribe_test.db"
                shutil.copy2(db_path, copy_path)
                logger.info("Copied db for post-gift subscribe test: %s", copy_path)
                db_path = copy_path
            claimed_account = claim_registered_account_for_pay_only(
                db_path,
                target_email=str(getattr(args, "claim_account_email", "") or ""),
                logger=logger,
            )
        except Exception as exc:
            logger.warning("claim registered account failed: %s", exc)
        if claimed_account:
            session_token = build_claimed_auth_payload(claimed_account)
        elif session_token.lower() == "sample":
            logger.warning("No claimable ChatGPT account found; skip post-gift subscribe sample token")
            return
    if not session_token:
        session_token = "sample"
    body = {
        "session_token": session_token,
        "phone_number": str(getattr(args, "local_phone", "") or getattr(args, "phone_number", "")),
        "pin": str(getattr(args, "pin", "") or ""),
        "sms_activation_id": str(getattr(args, "sms_activation_id", "") or ""),
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {str(getattr(args, 'post_gift_auth_token', '') or '')}",
    }
    safe_body = dict(body)
    safe_body["session_token"] = _redact_token(str(safe_body.get("session_token") or ""))
    safe_body["pin"] = "***"
    safe_body["phone_number"] = redact_phone(str(safe_body.get("phone_number") or ""))
    logger.info("Calling post-gift subscribe url=%s body=%s", url, safe_body)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )

    if not wait_response:
        def _send_async() -> None:
            try:
                with urllib.request.urlopen(req, timeout=3):
                    pass
            except Exception as exc:
                logger.warning("post-gift subscribe fire-and-forget send failed: %s", exc)

        threading.Thread(target=_send_async, name="post-gift-subscribe", daemon=False).start()
        logger.info("post-gift subscribe request dispatched; not waiting for payment result")
        return

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            logger.info("post-gift subscribe response status=%s body=%s", resp.status, text[:500])
    except Exception as exc:
        logger.warning("post-gift subscribe failed: %s", exc)


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
            self.shell("input keyevent 67", timeout=5)
            time.sleep(0.01)

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

    def force_stop_gopay(self, package: str = "") -> None:
        packages = []
        if package:
            packages.append(package)
        packages.extend(GOPAY_PACKAGE_CANDIDATES)
        seen = set()
        for pkg in packages:
            if not pkg or pkg in seen:
                continue
            seen.add(pkg)
            try:
                self.log.info("Force-stop GoPay package %s", pkg)
                self.shell(f"am force-stop {shlex.quote(pkg)}", timeout=8)
            except Exception as exc:
                self.log.debug("force-stop %s failed: %s", pkg, exc)

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
    RETRY_STATUS_CODES = {429, 500, 502, 503, 504, 520, 522, 524}
    RETRY_ERROR_HINTS = (
        "tls connect error",
        "curl: (35)",
        "connection closed abruptly",
        "connection reset",
        "connection aborted",
        "failed to connect",
        "recv failure",
        "send failure",
        "operation timed out",
        "timed out",
        "timeout",
        "curl: (28)",
        "curl: (52)",
        "curl: (55)",
        "curl: (56)",
    )

    def __init__(self, cfg: dict, logger: logging.Logger):
        otp_cfg = (cfg.get("otp") or {}).get("sms_api") or {}
        self.api_key = str(otp_cfg.get("api_key") or "").strip()
        self.base_url = str(otp_cfg.get("base_url") or "https://hero-sms.com").rstrip("/")
        self.poll_interval = int(otp_cfg.get("poll_interval_sec") or 3)
        self.use_proxy = bool(otp_cfg.get("use_proxy", True))
        self.proxy = str(otp_cfg.get("proxy") or cfg.get("proxy") or "").strip()
        self.retry_limit = max(1, int(otp_cfg.get("http_retry_limit") or otp_cfg.get("retry_limit") or 3))
        self.retry_base_sleep = max(0.5, float(otp_cfg.get("http_retry_base_sleep_s") or 2.0))
        self.log = logger
        self.cffi_requests = None
        self.session = None
        try:
            from curl_cffi import requests as cffi_requests  # type: ignore

            self.cffi_requests = cffi_requests
            self._reset_session()
            self.log.debug("HeroSMS will use curl_cffi chrome impersonation")
        except Exception as exc:
            self.log.debug("curl_cffi unavailable for HeroSMS, fallback enabled: %s", exc)
        if not self.api_key:
            raise RegisterError("config.json missing otp.sms_api.api_key")

    def _reset_session(self) -> None:
        if self.cffi_requests is None:
            self.session = None
            return
        old = self.session
        if old is not None:
            close = getattr(old, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self.session = self.cffi_requests.Session(impersonate="chrome136")
        if self.use_proxy and self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

    @classmethod
    def _is_transient_error(cls, exc: Exception) -> bool:
        if isinstance(exc, TransientHeroSmsError):
            return True
        text = str(exc).lower()
        return any(hint in text for hint in cls.RETRY_ERROR_HINTS)

    def _retry_sleep_seconds(self, attempt: int) -> float:
        return min(12.0, self.retry_base_sleep * (2 ** max(0, attempt - 1)))

    def _request(self, method: str, url: str, timeout: int = 15) -> str:
        method = method.upper()
        for attempt in range(1, self.retry_limit + 1):
            try:
                return self._request_once(method, url, timeout=timeout)
            except Exception as exc:
                if attempt >= self.retry_limit or not self._is_transient_error(exc):
                    if isinstance(exc, RegisterError):
                        raise
                    raise RegisterError(f"HeroSMS {method} failed after {attempt} attempt(s): {exc}") from exc
                sleep_s = self._retry_sleep_seconds(attempt)
                self.log.warning(
                    "HeroSMS %s transient error attempt %s/%s: %s; retrying in %.1fs",
                    method,
                    attempt,
                    self.retry_limit,
                    exc,
                    sleep_s,
                )
                if self.session is not None:
                    self._reset_session()
                time.sleep(sleep_s)
        raise RegisterError(f"HeroSMS {method} failed after {self.retry_limit} attempts")

    def _request_once(self, method: str, url: str, timeout: int = 15) -> str:
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
            if resp.status_code in self.RETRY_STATUS_CODES:
                raise TransientHeroSmsError(f"HeroSMS HTTP {resp.status_code}: {resp.text[:160]}")
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
                raise TransientHeroSmsError(f"curl failed: {(proc.stderr or proc.stdout).strip()}")
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
        resend_after_seconds: int = 0,
        on_resend: Optional[Callable[[], bool]] = None,
    ) -> str:
        if not activation_id:
            raise RegisterError("sms_activation_id is required when waiting for OTP")
        url = (
            f"{self.base_url}/stubs/handler_api.php?"
            f"api_key={self.api_key}&action=getStatus&id={activation_id}"
        )
        safe_url = re.sub(r"api_key=[^&]+", "api_key=***", url)
        deadline = time.time() + timeout_seconds
        started_at = time.time()
        last_resend_at = 0.0

        def maybe_resend() -> None:
            nonlocal last_resend_at
            if not resend_after_seconds or not on_resend:
                return
            now = time.time()
            if now - started_at < resend_after_seconds:
                return
            if last_resend_at and now - last_resend_at < resend_after_seconds:
                return
            try:
                if on_resend():
                    last_resend_at = now
            except Exception as exc:
                self.log.warning("OTP resend action failed: %s", exc)

        self.log.info("Polling HeroSMS activation=%s url=%s", activation_id, safe_url)
        while time.time() < deadline:
            try:
                body = self._open(url)
            except (urllib.error.URLError, TimeoutError, OSError, RegisterError) as exc:
                self.log.warning("HeroSMS request failed: %s", exc)
                maybe_resend()
                time.sleep(self.poll_interval)
                continue

            self.log.debug("HeroSMS response: %s", body[:300])
            code = self.extract_code(body)
            if code:
                if code in used_codes and not allow_reused:
                    self.log.info("HeroSMS returned already-used OTP %s; waiting for a newer code", code)
                    maybe_resend()
                    time.sleep(self.poll_interval)
                    continue
                used_codes.add(code)
                remember_used_otp(activation_id, code)
                self.log.info("HeroSMS got OTP %s", code)
                try:
                    self.request_retry_status(activation_id)
                except Exception as exc:
                    self.log.warning("HeroSMS setStatus=3 after OTP failed: %s", exc)
                return code

            self.log.info("HeroSMS waiting: %s", body[:120] or "<empty>")
            maybe_resend()
            time.sleep(self.poll_interval)

        raise OtpTimeoutError(f"timeout waiting for HeroSMS OTP after {timeout_seconds}s")

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

    def request_number(
        self,
        service: str,
        country: str,
        max_price: float = 0.05,
        operator: str = "",
        fixed_price: str = "true",
        ref: str = "",
        phone_exception: str = "",
    ) -> tuple[str, str]:
        if not service:
            raise RegisterError("HeroSMS service is required for getNumber")
        if not str(country or "").isdigit():
            raise RegisterError("HeroSMS country must be a numeric country id for getNumber")
        params = {
            "action": "getNumber",
            "service": service,
            "country": str(country),
            "api_key": self.api_key,
            "maxPrice": f"{float(max_price):.2f}",
        }
        if fixed_price:
            params["fixedPrice"] = str(fixed_price)
        if operator:
            params["operator"] = operator
        if ref:
            params["ref"] = ref
        if phone_exception:
            params["phoneException"] = phone_exception
        url = f"{self.base_url}/stubs/handler_api.php?{urllib.parse.urlencode(params)}"
        safe_url = re.sub(r"api_key=[^&]+", "api_key=***", url)
        self.log.info("HeroSMS getNumber url=%s", safe_url)
        body = self._open(url)
        self.log.info("HeroSMS getNumber response: %s", body[:200] or "<empty>")
        match = re.search(r"ACCESS_NUMBER:(\d+):(\d+)", body or "")
        if not match:
            raise RegisterError(f"HeroSMS getNumber failed: {body[:200]}")
        return match.group(1), match.group(2)

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
        self.pin_otp_submitted = False
        self.pin_success_confirmed = False
        self.step_index = 0
        self.technical_issue_retries = 0

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
        if self.pin_entries > 0:
            resend_after = self.args.pin_otp_resend_after
            resend_action = self.resend_current_otp
        else:
            resend_after = self.args.otp_resend_after
            resend_action = self.resend_current_otp
        self.log.info(
            "Waiting for OTP timeout=%ss resend_after=%ss stage=%s",
            self.args.otp_timeout,
            resend_after,
            "pin" if self.pin_entries > 0 else "registration",
        )
        try:
            code = self.sms.poll_otp(
                self.args.sms_activation_id,
                timeout_seconds=self.args.otp_timeout,
                used_codes=self.used_otps,
                allow_reused=self.args.allow_reused_otp,
                resend_after_seconds=resend_after,
                on_resend=resend_action if resend_after else None,
            )
        except OtpTimeoutError:
            remember_number_usage(
                self.args.full_phone or self.args.phone_number,
                "otp_timeout",
                activation_id=self.args.sms_activation_id,
                name=self.args.name,
                detail="HeroSMS OTP polling timed out",
            )
            raise
        self.log.info("Input OTP %s", code)
        otp_node = state.find_edit_text()
        if otp_node:
            self.log.info("Focus OTP EditText at %s,%s", otp_node.bounds.cx, otp_node.bounds.cy)
            self.adb.tap(otp_node.bounds.cx, otp_node.bounds.cy)
        else:
            self.log.info("Focus OTP fallback area")
            self.adb.tap_rel(state, 0.14, 0.34)
        self.adb.digits(code)
        if self.pin_entries > 0:
            self.pin_otp_submitted = True
        time.sleep(2)

    def resend_current_otp(self) -> bool:
        stage = "PIN OTP" if self.pin_entries > 0 else "registration OTP"
        self.log.info("%s still not received/refreshed; trying GoPay Resend", stage)
        state = self.adb.dump_ui()
        node = state.find("Resend")
        if not node:
            self.log.info("Resend button not visible on current OTP screen")
            return False
        self.log.info("Tap 'Resend' at %s,%s", node.bounds.cx, node.bounds.cy)
        self.adb.tap(node.bounds.cx, node.bounds.cy)
        if self.sms:
            try:
                self.sms.request_retry_status(self.args.sms_activation_id)
            except Exception as exc:
                self.log.warning("HeroSMS setStatus=3 after GoPay Resend failed: %s", exc)
        return True

    def handle_technical_issue(self, state: UiState) -> bool:
        self.technical_issue_retries += 1
        limit = max(1, int(getattr(self.args, "technical_issue_retry_limit", 5) or 5))
        if self.technical_issue_retries > limit:
            raise RegisterError(f"GoPay Technical Issue persisted after {limit} retries")
        self.log.info(
            "GoPay Technical Issue detected; retrying %s/%s after 5s",
            self.technical_issue_retries,
            limit,
        )
        time.sleep(5)
        if self.tap_text(state, "Try again", "Retry"):
            return True
        self.log.info("Try again button not found; tapping fallback bottom button")
        self.adb.tap_rel(state, 0.50, 0.92)
        return True

    def handle_offline_popup(self, state: UiState) -> bool:
        if not state.contains("You seem to be offline", "Check your internet connection"):
            return False
        self.technical_issue_retries += 1
        limit = max(1, int(getattr(self.args, "technical_issue_retry_limit", 5) or 5))
        if self.technical_issue_retries > limit:
            raise RegisterError(f"GoPay offline popup persisted after {limit} retries")
        self.log.info(
            "GoPay offline popup detected; tapping Try again %s/%s",
            self.technical_issue_retries,
            limit,
        )
        if self.tap_text(state, "Try again", "Retry"):
            time.sleep(3)
            return True
        self.log.info("Offline Try again button not found; tapping fallback bottom button")
        self.adb.tap_rel(state, 0.50, 0.92)
        time.sleep(3)
        return True

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
        if state.contains("Izinkan akses lokasi", "Oke, lanjut", "Perlindungan dari penipuan"):
            if self.tap_text(state, "Nanti aja"):
                return True
            self.log.info("Location onboarding skip button not found by text; tapping fallback")
            self.adb.tap_rel(state, 0.50, 0.945)
            return True
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
        self.pin_success_confirmed = True
        remember_pin_setup(self.args.full_phone, self.args.name)
        remember_number_usage(
            self.args.full_phone,
            "registered_success",
            activation_id=self.args.sms_activation_id,
            name=self.args.name,
            detail="GoPay registration and PIN setup completed",
        )
        self.log.info("GoPay PIN updated successfully")
        if self.args.get_rp_link and RECEIVE_GIFT_AFTER_PIN:
            self.log.info("Opening config gopay.get_rp_link in emulator browser")
            self.adb.open_url(self.args.get_rp_link)
            if wait_and_tap_open_gift(self.adb, self.log):
                self.finish_after_open_gift("Open gift tapped after RP link")
        elif self.args.get_rp_link:
            self.log.info("Skipping RP link/open gift; dispatching subscribe after PIN setup")
            post_subscribe_after_gift(self.args, self.log, wait_response=False)
        else:
            post_subscribe_after_gift(self.args, self.log, wait_response=False)

    def finish_after_open_gift(self, detail: str = "Open gift tapped") -> None:
        remember_number_usage(
            self.args.full_phone,
            "gift_opened_subscribe_dispatched",
            activation_id=self.args.sms_activation_id,
            name=self.args.name,
            detail=detail,
        )
        self.log.info("Open gift completed; dispatching post-gift subscribe and ending registration flow")
        post_subscribe_after_gift(self.args, self.log, wait_response=False)

    def security_score_needs_pin_setup(self, state: UiState) -> bool:
        return state.contains("25%", "1/4 actions completed", "Maximize your security")

    def pin_setup_completed_after_otp(self, state: UiState) -> bool:
        if self.pin_success_confirmed or not self.pin_otp_submitted or self.pin_entries < 2:
            return False
        # After PIN OTP is accepted, GoPay often returns to Account & safety
        # without showing the "successfully updated" toast. At that point 25% /
        # 1/4 means the PIN task is done, not that Create PIN should be opened
        # again. Treat either Manage PIN or the security score landing page as
        # enough evidence to stop the reset loop.
        return state.contains("Manage PIN", "25%", "1/4 actions completed", "Account & safety", "Account protection")

    def stop_existing_pin_account(self, state: UiState, detail: str) -> bool:
        if not state.contains("Manage PIN"):
            return False
        phone = self.args.full_phone or self.args.phone_number
        reason = "phone/account already has GoPay PIN; skip this registration"
        remember_unusable_number(
            phone,
            reason=reason,
            activation_id=self.args.sms_activation_id,
            screen=state.text,
        )
        remember_number_usage(
            phone,
            "phone_already_has_pin",
            activation_id=self.args.sms_activation_id,
            name=self.args.name,
            detail=detail or reason,
        )
        self.log.warning("%s; ending current run so batch can continue", reason)
        self.adb.force_stop_gopay(getattr(self.args, "package", ""))
        return True

    def handle_security_score_pin_path(self, state: UiState) -> bool:
        self.log.info("Security score is 25%% / 1/4; checking PIN setup/reset path")
        if self.tap_row_by_text(state, "Create PIN"):
            return True
        if state.contains("Manage PIN"):
            self.stop_existing_pin_account(state, "Manage PIN visible on security score page")
            return False
        if self.tap_text(state, "Strengthen your protection now", "Account & safety"):
            return True
        self.log.info("Create PIN / Manage PIN not visible; scrolling security settings")
        self.adb.shell("input swipe 280 860 280 520 500")
        time.sleep(1)
        return True

    def match_register_exception(self, state: UiState) -> Optional[dict]:
        for item in REGISTER_EXCEPTIONS:
            if state.contains_all(*item["needles"]):
                return item
        return None

    def handle_register_exception(self, item: dict, state: UiState) -> bool:
        name = str(item.get("name") or "unknown_exception")
        reason = str(item.get("reason") or name)
        action = str(item.get("action") or "")
        self.log.warning("Register exception matched: %s", name)
        phone = self.args.full_phone or self.args.phone_number
        try:
            if action == "mark_phone_unusable":
                remember_unusable_number(
                    phone,
                    reason=reason,
                    activation_id=self.args.sms_activation_id,
                    screen=state.text,
                )
                remember_number_usage(
                    phone,
                    name,
                    activation_id=self.args.sms_activation_id,
                    name=self.args.name,
                    detail=reason,
                )
                self.log.warning(
                    "Marked phone=%s unusable and stopped current flow: %s",
                    redact_phone(phone),
                    reason,
                )
                return True
            if action == "mark_device_unusable":
                remember_device_exception(
                    self.adb.device,
                    reason=reason,
                    phone=phone,
                    screen=state.text,
                )
                remember_number_usage(
                    phone,
                    "device_unusable",
                    activation_id=self.args.sms_activation_id,
                    name=self.args.name,
                    detail=reason,
                )
                self.log.warning(
                    "Marked device=%s unusable and stopped current flow: %s",
                    self.adb.device or "unknown",
                    reason,
                )
                return True
        finally:
            self.adb.force_stop_gopay(getattr(self.args, "package", ""))
        raise RegisterError(f"Unhandled register exception {name}: {reason}")

    def run(self) -> None:
        deadline = time.time() + self.args.flow_timeout
        while time.time() < deadline:
            state = self.adb.dump_ui()
            brief = first_interesting_line(state.text)
            self.log.info("Current screen: %s", brief)
            self.save_observation(state, classify_state(state))

            if self.handle_offline_popup(state):
                continue

            exception = self.match_register_exception(state)
            if exception:
                if self.handle_register_exception(exception, state):
                    return

            if state.contains("successfully updated your GoPay PIN"):
                self.tap_text(state, "Got it")
                self.finish_after_pin_success()
                return

            if self.pin_setup_completed_after_otp(state):
                self.log.info("PIN OTP accepted; Account & safety shows PIN is already set")
                self.finish_after_pin_success()
                return

            if self.stop_existing_pin_account(state, "Manage PIN screen detected before PIN setup completion"):
                return

            if self.security_score_needs_pin_setup(state):
                if not self.handle_security_score_pin_path(state):
                    return
                continue

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
                if not self.pin_success_confirmed:
                    self.log.warning("Gift page appeared before confirmed PIN success; leaving gift page and continuing PIN setup")
                    self.adb.keyevent(4)
                    time.sleep(1)
                    continue
                if not RECEIVE_GIFT_AFTER_PIN:
                    self.log.info("Gift page visible after PIN setup; skipping Open gift and dispatching subscribe")
                    post_subscribe_after_gift(self.args, self.log, wait_response=False)
                    return
                if not self.tap_text(state, "Open gift"):
                    self.adb.tap_rel(state, 0.50, 0.95)
                time.sleep(2)
                self.finish_after_open_gift("Open gift tapped from visible gift page")
                return

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
                if self.stop_existing_pin_account(state, "Manage PIN visible on protection page"):
                    return
                self.log.info("Create PIN / Manage PIN not visible; scrolling protection page")
                self.adb.shell("input swipe 280 860 280 520 500")
                time.sleep(1)
                continue

            if state.contains("Technical Issue", "There's a technical error"):
                self.handle_technical_issue(state)
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
        ("exception_phone_registered_whatsapp_only", ("Check WhatsApp for OTP", "Open WhatsApp", "Login or signup issues?")),
        ("exception_phone_registered", ("Other ways to log in", "This is not my account")),
        ("exception_phone_registered_pin_login", ("Enter your PIN", "Enter your GoPay PIN to log in")),
        ("exception_device_cooldown", ("Try logging in after 12 hours",)),
        ("verification_method", ("Choose verification method",)),
        ("privacy_consent", ("Important before you proceed",)),
        ("welcome", ("Welcome to GoPay",)),
        ("details", ("Fill out a few details",)),
        ("create_pin", ("Create PIN",)),
        ("confirm_pin", ("Confirm PIN",)),
        ("profile", ("Account & safety", "Account protection")),
        ("home", ("Top up", "Withdraw")),
        ("gift", ("Received from", "Open gift", "Make it festive")),
        ("location_onboarding", ("Izinkan akses lokasi", "Oke, lanjut", "Perlindungan dari penipuan")),
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


def should_prepare_emulator(args: argparse.Namespace) -> bool:
    if args.skip_prepare_emulator or args.device:
        return False
    if any(
        (
            args.test_claim_account,
            args.test_post_gift_subscribe,
            args.test_otp,
            args.test_next_otp,
            args.request_extra_sms,
            args.request_retry_status,
            args.mark_pin_setup,
            args.input_test,
        )
    ):
        return False
    return bool(args.prepare_emulator)


def prepare_emulator_for_registration(args: argparse.Namespace, logger: logging.Logger) -> str:
    try:
        import gopay_prepare_emulator as prep
    except Exception as exc:
        raise RegisterError(f"cannot import gopay_prepare_emulator.py: {exc}") from exc

    prep_args = argparse.Namespace(
        ld_dir=args.ld_dir,
        name=args.prepare_name,
        index=args.prepare_index,
        create=args.prepare_create,
        unique_name=args.prepare_unique_name,
        width=args.prepare_width,
        height=args.prepare_height,
        dpi=args.prepare_dpi,
        mt_apk=args.mt_apk,
        gopay_apks=args.gopay_apks,
        boot_timeout=args.prepare_boot_timeout,
        open_mt=args.prepare_open_mt,
        print_device=False,
        verbose=args.verbose,
    )
    assert_prepare_target_not_protected(args)
    logger.info(
        "Preparing LDPlayer before registration name=%s index=%s",
        prep_args.name or "<auto>",
        prep_args.index or "<none>",
    )
    try:
        result = prep.prepare(prep_args, logger)
    except Exception as exc:
        raise RegisterError(f"prepare emulator failed: {exc}") from exc
    device = str((result or {}).get("device") or "").strip()
    if not device:
        raise RegisterError("prepare emulator did not return an adb device")
    logger.info("Prepared emulator device=%s; registration will use this device", device)
    return device


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


def connect_device(
    adb_path: Path,
    requested: str,
    logger: logging.Logger,
    protected_devices: Optional[set[str]] = None,
) -> str:
    protected_devices = protected_devices or set()
    base = adb_without_device(adb_path, logger)
    if requested:
        logger.info("Using requested ADB device %s", requested)
        if is_protected_device(requested, protected_devices):
            raise RegisterError(
                f"refusing to use protected ADB device {requested}; choose another emulator/device"
            )
        if ":" in requested:
            base.raw(["connect", requested], timeout=10)
            time.sleep(0.8)
        check = Adb(adb_path, requested, logger).raw(["get-state"], timeout=15)
        if check.returncode != 0 or "device" not in (check.stdout or ""):
            raise RegisterError(
                f"requested ADB device is not ready: {requested} :: "
                f"{(check.stderr or check.stdout or '').strip()}"
            )
        return requested

    out = base.check(["devices"], timeout=15)
    devices = filter_protected_devices(parse_adb_devices(out), protected_devices, logger)
    if devices:
        logger.info("Using connected ADB device %s", devices[0])
        return devices[0]

    for port in CONNECT_PORTS:
        target = f"127.0.0.1:{port}"
        logger.info("Trying adb connect %s", target)
        base.raw(["connect", target], timeout=10)
        time.sleep(0.8)
        out = base.check(["devices"], timeout=15)
        devices = filter_protected_devices(parse_adb_devices(out), protected_devices, logger)
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


def maybe_buy_herosms_number(args: argparse.Namespace, sms: Optional[HeroSmsClient], logger: logging.Logger) -> None:
    if args.full_phone and args.sms_activation_id:
        return
    if not args.auto_buy_number:
        return
    if not sms:
        raise RegisterError("HeroSMS client is required for --auto-buy-number")
    activation_id, phone = sms.request_number(
        service=args.sms_service,
        country=args.sms_country_id,
        max_price=args.sms_max_price,
        operator=args.sms_operator,
        fixed_price=args.sms_fixed_price,
        phone_exception=args.sms_phone_exception,
    )
    full_phone, local_phone = normalize_indonesia_phone(phone)
    args.sms_activation_id = activation_id
    args.phone_number = phone
    args.full_phone = full_phone
    args.local_phone = local_phone
    logger.info(
        "HeroSMS bought phone=%s activation=%s",
        redact_phone(full_phone),
        activation_id,
    )
    remember_number_usage(
        full_phone,
        "number_bought",
        activation_id=activation_id,
        name=args.name,
        detail=f"HeroSMS getNumber service={args.sms_service} country={args.sms_country_id} maxPrice={args.sms_max_price}",
    )


def test_claim_registered_account(args: argparse.Namespace, logger: logging.Logger) -> None:
    source = webui_db_path(args.webui_db_path)
    if not source.exists():
        raise RegisterError(f"webui db not found: {source}")
    if args.claim_test_write_real_db:
        db_path = source
        logger.warning("Testing claim against REAL db: %s", db_path)
    else:
        LOG_DIR.mkdir(exist_ok=True)
        db_path = LOG_DIR / "webui_claim_test.db"
        shutil.copy2(source, db_path)
        logger.info("Copied db for claim test: %s", db_path)
    before_conn = sqlite3.connect(str(db_path))
    try:
        before_conn.row_factory = sqlite3.Row
        initial_count = before_conn.execute(
            "SELECT COUNT(*) AS n FROM registered_accounts WHERE upper(coalesce(status, 'INITIAL')) = 'INITIAL'"
        ).fetchone()["n"]
    finally:
        before_conn.close()
    account = claim_registered_account_for_pay_only(
        db_path,
        target_email=args.claim_account_email,
        logger=logger,
    )
    if not account:
        logger.info("Claim test result: no claimable account; initial_count=%s", initial_count)
        return
    after_conn = sqlite3.connect(str(db_path))
    try:
        after_conn.row_factory = sqlite3.Row
        row = after_conn.execute(
            "SELECT id, email, status FROM registered_accounts WHERE id = ?",
            (account["id"],),
        ).fetchone()
    finally:
        after_conn.close()
    logger.info(
        "Claim test result: id=%s email=%s status=%s initial_count_before=%s db=%s",
        row["id"],
        row["email"],
        row["status"],
        initial_count,
        db_path,
    )


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
    parser.add_argument("--auto-buy-number", action="store_true", help="Buy phone number from HeroSMS before registration")
    parser.add_argument("--sms-service", default="", help="HeroSMS getNumber service code")
    parser.add_argument("--sms-country-id", default="", help="HeroSMS numeric country id")
    parser.add_argument("--sms-max-price", type=float, default=0.05, help="HeroSMS maxPrice for getNumber")
    parser.add_argument("--sms-operator", default="", help="HeroSMS operator list for getNumber")
    parser.add_argument("--sms-fixed-price", default="true", help="HeroSMS fixedPrice value for getNumber")
    parser.add_argument("--sms-phone-exception", default="", help="HeroSMS phoneException prefixes")
    parser.add_argument("--post-gift-subscribe", action="store_true", help="Call local /subscribe after Open gift")
    parser.add_argument("--post-gift-subscribe-url", default="", help="Subscribe URL after Open gift")
    parser.add_argument("--post-gift-session-token", default="", help="Session token for post-gift subscribe; default sample")
    parser.add_argument("--webui-db-path", default="", help="Path to webui.db for claiming ChatGPT credentials")
    parser.add_argument("--claim-account-email", default="", help="Only claim this registered account email")
    parser.add_argument("--no-claim-account-for-pay", action="store_true", help="Do not claim ChatGPT credentials from SQLite")
    parser.add_argument("--test-claim-account", action="store_true", help="Test claiming a ChatGPT account from SQLite and exit")
    parser.add_argument("--claim-test-write-real-db", action="store_true", help="For --test-claim-account, write to the real DB instead of a copy")
    parser.add_argument("--test-post-gift-subscribe", action="store_true", help="Only test claiming an account and POST /subscribe")
    parser.add_argument("--adb-path", default="", help=rf"ADB path; default {DEFAULT_ADB}")
    parser.add_argument("--device", default="", help="ADB device serial; optional")
    parser.add_argument("--package", default="", help="GoPay/Gojek Android package; optional")
    parser.add_argument("--step-dir", default="", help="Directory for screenshots/XML of this run")
    parser.add_argument("--prepare-emulator", action="store_true", help="Run LDPlayer preparation before registration")
    parser.add_argument("--skip-prepare-emulator", action="store_true", help="Do not run LDPlayer preparation")
    parser.add_argument("--prepare-name", default="", help="LDPlayer instance name for preparation")
    parser.add_argument("--prepare-index", default="", help="LDPlayer instance index for preparation")
    parser.add_argument("--prepare-create", action="store_true", help="Create LDPlayer instance when prepare-name does not exist")
    parser.add_argument("--prepare-unique-name", action="store_true", help="Auto-suffix prepare-name when creating and the name exists")
    parser.add_argument("--protected-emulator-name", action="append", default=[], help="LDPlayer instance name that registration must never use")
    parser.add_argument("--protected-emulator-index", action="append", default=[], help="LDPlayer instance index that registration must never use")
    parser.add_argument("--protected-device", action="append", default=[], help="ADB device serial that registration must never use")
    parser.add_argument("--ld-dir", default="", help=rf"LDPlayer directory; default {DEFAULT_LD_DIR}")
    parser.add_argument("--mt-apk", default="", help=rf"MT Manager APK; default {DEFAULT_MT_APK}")
    parser.add_argument("--gopay-apks", default="", help=rf"GoPay APKS; default {DEFAULT_GOPAY_APKS}")
    parser.add_argument("--prepare-width", type=int, default=1080)
    parser.add_argument("--prepare-height", type=int, default=1920)
    parser.add_argument("--prepare-dpi", type=int, default=480)
    parser.add_argument("--prepare-boot-timeout", type=int, default=180)
    parser.add_argument("--prepare-open-mt", action="store_true", help="Open MT Manager after preparation")
    parser.add_argument("--technical-issue-retry-limit", type=int, default=5, help="How many times to tap Try again on GoPay Technical Issue")
    parser.add_argument("--flow-timeout", type=int, default=600, help="Whole flow timeout seconds")
    parser.add_argument("--otp-timeout", type=int, default=180, help="HeroSMS OTP timeout seconds")
    parser.add_argument("--otp-resend-after", type=int, default=60, help="After registration OTP waits this many seconds, tap Resend")
    parser.add_argument("--pin-otp-resend-after", type=int, default=60, help="After PIN setup OTP waits this many seconds, tap Resend")
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
    otp_sms_cfg = ((cfg.get("otp") or {}).get("sms_api") or {})
    post_gift_cfg = (gopay_cfg.get("post_gift_subscribe") or {})
    cfg_name = str(gopay_cfg.get("name") or "").strip()
    args.name = (args.name or ("" if cfg_name.lower() == "random" else cfg_name)).strip()
    if not args.name:
        args.name = random_registration_name()
    args.get_rp_link = (args.get_rp_link or str(gopay_cfg.get("get_rp_link") or "")).strip()
    args.auto_buy_number = bool(args.auto_buy_number or gopay_cfg.get("auto_buy_number"))
    args.sms_service = (args.sms_service or str(otp_sms_cfg.get("service") or "")).strip()
    args.sms_country_id = (
        args.sms_country_id
        or str(otp_sms_cfg.get("country_id") or otp_sms_cfg.get("country") or "")
    ).strip()
    args.sms_country_id = normalize_herosms_country_id(args.sms_country_id)
    args.sms_operator = (args.sms_operator or str(otp_sms_cfg.get("operator") or "")).strip()
    args.sms_fixed_price = (
        args.sms_fixed_price
        if args.sms_fixed_price != "true"
        else str(otp_sms_cfg.get("fixedPrice") or "true")
    ).strip()
    args.sms_phone_exception = (
        args.sms_phone_exception or str(otp_sms_cfg.get("phoneException") or "")
    ).strip()
    if "otp_resend_after" in gopay_cfg and args.otp_resend_after == 60:
        args.otp_resend_after = int(gopay_cfg.get("otp_resend_after") or 60)
    if "pin_otp_resend_after" in gopay_cfg and args.pin_otp_resend_after == 60:
        args.pin_otp_resend_after = int(gopay_cfg.get("pin_otp_resend_after") or 60)
    if args.otp_timeout == 180:
        args.otp_timeout = int(
            gopay_cfg.get("otp_timeout")
            or otp_sms_cfg.get("poll_timeout_sec")
            or (cfg.get("orchestrator") or {}).get("otp_timeout")
            or 180
        )
    if not args.sms_max_price:
        args.sms_max_price = float(otp_sms_cfg.get("maxPrice") or 0.05)
    args.post_gift_subscribe = bool(
        args.post_gift_subscribe or post_gift_cfg.get("enabled", True)
    )
    orchestrator_cfg = cfg.get("orchestrator") or {}
    args.post_gift_subscribe_url = (
        args.post_gift_subscribe_url
        or str(post_gift_cfg.get("url") or f"http://localhost:{orchestrator_cfg.get('port') or 8800}/subscribe")
    ).strip()
    args.post_gift_session_token = (
        args.post_gift_session_token
        or str(post_gift_cfg.get("session_token") or "sample")
    ).strip()
    args.post_gift_auth_token = str(
        post_gift_cfg.get("auth_token")
        or orchestrator_cfg.get("auth_token")
        or ""
    ).strip()
    account_claim_cfg = (gopay_cfg.get("account_claim") or {})
    prepare_cfg = (gopay_cfg.get("prepare_emulator") or {})
    protected_cfg = protected_emulators_from_config(gopay_cfg)
    args.protected_names = list(dict.fromkeys(
        config_string_list(args.protected_emulator_name) + protected_cfg["names"]
    ))
    args.protected_indexes = list(dict.fromkeys(
        config_string_list(args.protected_emulator_index) + protected_cfg["indexes"]
    ))
    args.protected_devices = list(dict.fromkeys(
        config_string_list(args.protected_device) + protected_cfg["devices"]
    ))
    args.prepare_emulator = bool(args.prepare_emulator or prepare_cfg.get("enabled", True))
    args.prepare_name = (
        args.prepare_name or str(prepare_cfg.get("name") or "gopay-auto-1")
    ).strip()
    args.prepare_index = (
        args.prepare_index or str(prepare_cfg.get("index") or "")
    ).strip()
    args.prepare_create = bool(args.prepare_create or prepare_cfg.get("create", True))
    args.prepare_unique_name = bool(args.prepare_unique_name or prepare_cfg.get("unique_name", True))
    args.ld_dir = (
        args.ld_dir or str(prepare_cfg.get("ld_dir") or DEFAULT_LD_DIR)
    ).strip()
    args.mt_apk = (
        args.mt_apk or str(prepare_cfg.get("mt_apk") or DEFAULT_MT_APK)
    ).strip()
    args.gopay_apks = (
        args.gopay_apks or str(prepare_cfg.get("gopay_apks") or DEFAULT_GOPAY_APKS)
    ).strip()
    args.prepare_width = int(prepare_cfg.get("width") or args.prepare_width)
    args.prepare_height = int(prepare_cfg.get("height") or args.prepare_height)
    args.prepare_dpi = int(prepare_cfg.get("dpi") or args.prepare_dpi)
    args.prepare_boot_timeout = int(prepare_cfg.get("boot_timeout") or args.prepare_boot_timeout)
    args.prepare_open_mt = bool(args.prepare_open_mt or prepare_cfg.get("open_mt", False))
    args.webui_db_path = (
        args.webui_db_path or str(account_claim_cfg.get("db_path") or "")
    ).strip()
    args.claim_account_email = (
        args.claim_account_email or str(account_claim_cfg.get("target_email") or "")
    ).strip()
    args.claim_account_for_pay = not bool(args.no_claim_account_for_pay)

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
    if args.phone_number:
        full_phone, local_phone = normalize_indonesia_phone(args.phone_number)
        args.full_phone = full_phone
        args.local_phone = local_phone
    elif args.auto_buy_number:
        args.full_phone = ""
        args.local_phone = ""
    else:
        full_phone, local_phone = normalize_indonesia_phone(args.phone_number)
        args.full_phone = full_phone
        args.local_phone = local_phone
    return args


def main(argv: Optional[list[str]] = None) -> int:
    global STEP_DIR
    args = build_args(argv)
    if args.step_dir:
        STEP_DIR = Path(args.step_dir)
    log = setup_logging(args.verbose)
    try:
        cfg = load_config(Path(args.config))
        args = enrich_args(args, cfg)
        adb_path = find_adb_path(args.adb_path)
        log.info("Using adb: %s", adb_path)
        log.info("Phone=%s name=%s", redact_phone(args.full_phone), args.name)
        prepared_device = ""

        if args.test_claim_account:
            test_claim_registered_account(args, log)
            return 0

        if args.test_post_gift_subscribe:
            if not args.pin:
                raise RegisterError("--pin is required for --test-post-gift-subscribe")
            if not args.phone_number:
                raise RegisterError("--phone-number is required for --test-post-gift-subscribe")
            if not args.sms_activation_id:
                raise RegisterError("--sms-activation-id is required for --test-post-gift-subscribe")
            post_subscribe_after_gift(args, log)
            return 0

        if args.mark_pin_setup:
            if not args.full_phone:
                raise RegisterError("--phone-number is required for --mark-pin-setup")
            remember_pin_setup(args.full_phone, args.name, source="manual")
            remember_number_usage(
                args.full_phone,
                "manual_mark_pin_setup",
                activation_id=args.sms_activation_id,
                name=args.name,
                detail="Manually marked PIN setup as complete",
            )
            log.info("Recorded PIN setup for phone=%s", redact_phone(args.full_phone))
            return 0

        if args.open_rp_link_only:
            if should_prepare_emulator(args):
                prepared_device = prepare_emulator_for_registration(args, log)
                args.device = prepared_device
            if not args.get_rp_link:
                raise RegisterError("get_rp_link is empty; set config.json gopay.get_rp_link or pass --get-rp-link")
            device = connect_device(adb_path, args.device, log, protected_device_serials(args))
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
            remember_number_usage(
                args.full_phone,
                "pin_already_recorded",
                activation_id=args.sms_activation_id,
                name=args.name,
                detail="Skipped registration because PIN setup is already recorded",
            )
            log.info(
                "PIN setup already recorded for phone=%s; skipping Profile/PIN steps",
                redact_phone(args.full_phone),
            )
            if args.get_rp_link and RECEIVE_GIFT_AFTER_PIN:
                if should_prepare_emulator(args):
                    prepared_device = prepare_emulator_for_registration(args, log)
                    args.device = prepared_device
                device = connect_device(adb_path, args.device, log, protected_device_serials(args))
                adb = Adb(adb_path, device, log)
                log.info("Opening get_rp_link in emulator browser")
                adb.open_url(args.get_rp_link)
                if wait_and_tap_open_gift(adb, log):
                    post_subscribe_after_gift(args, log, wait_response=False)
            elif args.get_rp_link:
                log.info("Skipping RP link/open gift; dispatching subscribe because PIN is already recorded")
                post_subscribe_after_gift(args, log, wait_response=False)
            else:
                post_subscribe_after_gift(args, log, wait_response=False)
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

        maybe_buy_herosms_number(args, sms, log)
        if not args.full_phone:
            raise RegisterError("phone_number is required unless --auto-buy-number succeeds")
        log.info("Registration phone=%s activation=%s", redact_phone(args.full_phone), args.sms_activation_id)

        if should_prepare_emulator(args):
            prepared_device = prepare_emulator_for_registration(args, log)
            args.device = prepared_device

        device = connect_device(adb_path, args.device, log, protected_device_serials(args))
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

        remember_number_usage(
            args.full_phone,
            "started",
            activation_id=args.sms_activation_id,
            name=args.name,
            detail="Started GoPay registration flow",
        )
        flow = GoPayRegisterFlow(adb, sms, args, log)
        flow.run()
        return 0
    except RegisterError as exc:
        phone = getattr(args, "full_phone", "") or getattr(args, "phone_number", "")
        should_record_failure = bool(
            phone
            and not getattr(args, "test_otp", False)
            and not getattr(args, "test_next_otp", False)
            and not getattr(args, "request_extra_sms", False)
            and not getattr(args, "request_retry_status", False)
            and not getattr(args, "open_rp_link_only", False)
            and not getattr(args, "input_test", False)
        )
        if should_record_failure and not isinstance(exc, OtpTimeoutError):
            remember_number_usage(
                phone,
                "failed",
                activation_id=getattr(args, "sms_activation_id", ""),
                name=getattr(args, "name", ""),
                detail=str(exc),
            )
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        phone = getattr(args, "full_phone", "") or getattr(args, "phone_number", "")
        if phone:
            remember_number_usage(
                phone,
                "interrupted",
                activation_id=getattr(args, "sms_activation_id", ""),
                name=getattr(args, "name", ""),
                detail="Interrupted by user",
            )
        log.warning("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
