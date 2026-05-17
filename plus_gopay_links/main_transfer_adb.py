#!/usr/bin/env python3
"""ADB helper that transfers GoPay balance from the main emulator account."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


DEFAULT_ADB = Path(r"E:\leidian\LDPlayer9\adb.exe")
DEFAULT_PACKAGE = "com.gojek.gopay"
GOPAY_PACKAGE_CANDIDATES = ("com.gojek.gopay", "com.gojek.app", "com.go-jek.ios")
UI_DUMP_DEVICE_PATH = "/sdcard/window.xml"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import clash_verge_rotator

DEFAULT_LOCK_DIR = ROOT / "logs" / "main_transfer_locks"


class MainTransferError(RuntimeError):
    pass


class MainTransferTechnicalIssue(MainTransferError):
    pass


class CallableLogAdapter:
    def __init__(self, writer: Callable[..., None]):
        self.writer = writer

    def info(self, msg: str, *args) -> None:
        self.writer(msg % args if args else msg)

    def warning(self, msg: str, *args) -> None:
        self.writer(msg % args if args else msg)

    def error(self, msg: str, *args) -> None:
        self.writer(msg % args if args else msg)

    def debug(self, msg: str, *args) -> None:
        return None


class MainTransferDeviceLock:
    def __init__(
        self,
        device: str,
        enabled: bool,
        wait_timeout_seconds: int,
        stale_seconds: int,
        logger,
        lock_dir: Path = DEFAULT_LOCK_DIR,
    ):
        self.device = str(device or "unknown").strip() or "unknown"
        self.enabled = enabled
        self.wait_timeout_seconds = max(0, int(wait_timeout_seconds))
        self.stale_seconds = max(0, int(stale_seconds))
        self.logger = logger
        self.lock_dir = lock_dir
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.device)
        self.path = self.lock_dir / f"{safe_name}.lock"
        self.fd: int | None = None

    def __enter__(self):
        if not self.enabled:
            self.logger.info("[main-transfer] lock disabled for device=%s", self.device)
            return self
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.wait_timeout_seconds
        logged_wait = False
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = f"pid={os.getpid()} device={self.device} acquired_at={int(time.time())}\n"
                os.write(self.fd, payload.encode("utf-8", errors="replace"))
                self.logger.info("[main-transfer] acquired lock %s", self.path)
                return self
            except FileExistsError:
                if self._remove_stale_lock():
                    continue
                if time.time() >= deadline:
                    raise MainTransferError(f"main_transfer lock timeout for {self.device}")
                if not logged_wait:
                    self.logger.info("[main-transfer] waiting for lock %s", self.path)
                    logged_wait = True
                time.sleep(1)

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.enabled:
            return
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            self.path.unlink()
            self.logger.info("[main-transfer] released lock %s", self.path)
        except FileNotFoundError:
            pass
        except OSError as err:
            self.logger.warning("[main-transfer] failed to release lock %s: %s", self.path, err)

    def _remove_stale_lock(self) -> bool:
        if self.stale_seconds <= 0:
            return False
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age < self.stale_seconds:
            return False
        try:
            self.path.unlink()
            self.logger.warning(
                "[main-transfer] removed stale lock %s age=%.1fs",
                self.path,
                age,
            )
            return True
        except FileNotFoundError:
            return True
        except OSError as err:
            self.logger.warning("[main-transfer] failed to remove stale lock %s: %s", self.path, err)
            return False


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
        return any(needle.lower() in haystack for needle in needles if needle)

    def contains_all(self, *needles: str) -> bool:
        haystack = self.text.lower()
        return all(needle.lower() in haystack for needle in needles if needle)

    def find(self, *needles: str, exact: bool = False, enabled_only: bool = True) -> Optional[UiNode]:
        lowered = [needle.lower() for needle in needles if needle]
        for node in self.nodes:
            if enabled_only and not node.enabled:
                continue
            label = node.label.lower()
            if exact and any(label == needle for needle in lowered):
                return node
            if not exact and any(needle in label for needle in lowered):
                return node
        return None

    def find_edit_text(self, index: int = 0) -> Optional[UiNode]:
        nodes = [node for node in self.nodes if node.klass.endswith("EditText") and node.enabled]
        if 0 <= index < len(nodes):
            return nodes[index]
        return None


class Adb:
    def __init__(self, adb_path: Path, device: str, log: logging.Logger):
        self.adb_path = str(adb_path)
        self.device = str(device or "").strip()
        self.log = log

    def raw(self, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
        cmd = [self.adb_path]
        if self.device and args[:1] != ["connect"]:
            cmd += ["-s", self.device]
        cmd += args
        self.log.debug("[main-transfer] adb: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def check(self, args: list[str], timeout: int = 20) -> str:
        proc = self.raw(args, timeout=timeout)
        if proc.returncode != 0:
            raise MainTransferError(
                f"adb command failed: {' '.join(args)} :: {(proc.stderr or proc.stdout or '').strip()}"
            )
        return proc.stdout or ""

    def shell(self, command: str, timeout: int = 20) -> str:
        return self.check(["shell", command], timeout=timeout)

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}", timeout=8)
        time.sleep(0.7)

    def tap_rel(self, state: UiState, rx: float, ry: float) -> None:
        self.tap(int(state.width * rx), int(state.height * ry))

    def text(self, value: str) -> None:
        safe = str(value or "").replace(" ", "%s")
        if not safe:
            raise MainTransferError("refusing to input empty text")
        self.shell(f"input text {shlex.quote(safe)}", timeout=10)
        time.sleep(0.5)

    def digits(self, value: str) -> None:
        keycodes = {str(i): 7 + i for i in range(10)}
        for digit in str(value):
            if digit in keycodes:
                self.shell(f"input keyevent {keycodes[digit]}", timeout=5)
                time.sleep(0.08)

    def clear_text(self, presses: int = 24) -> None:
        for _ in range(presses):
            self.shell("input keyevent 67", timeout=5)
            time.sleep(0.01)

    def wm_size(self) -> tuple[int, int]:
        out = self.shell("wm size", timeout=8)
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 560, 1000

    def dump_ui(self) -> UiState:
        width, height = self.wm_size()
        self.shell(f"uiautomator dump {UI_DUMP_DEVICE_PATH} >/dev/null 2>&1", timeout=15)
        xml = self.shell(f"cat {UI_DUMP_DEVICE_PATH}", timeout=15)
        nodes: list[UiNode] = []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise MainTransferError(f"uiautomator XML parse failed: {exc}") from exc
        for elem in root.iter("node"):
            nodes.append(
                UiNode(
                    text=elem.attrib.get("text", "") or "",
                    desc=elem.attrib.get("content-desc", "") or "",
                    klass=elem.attrib.get("class", "") or "",
                    bounds=parse_bounds(elem.attrib.get("bounds", "")),
                    clickable=elem.attrib.get("clickable", "false") == "true",
                    enabled=elem.attrib.get("enabled", "true") == "true",
                )
            )
        return UiState(xml=xml, nodes=nodes, width=width, height=height)

    def start_package(self, package: str) -> None:
        proc = self.raw(
            ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=20,
        )
        if proc.returncode != 0 or "monkey aborted" in (proc.stdout or "").lower():
            raise MainTransferError(f"failed to launch package {package}: {proc.stdout or proc.stderr}")
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
                self.log.info("[main-transfer] force-stop GoPay package %s", pkg)
                self.shell(f"am force-stop {shlex.quote(pkg)}", timeout=8)
            except Exception as exc:
                self.log.debug("[main-transfer] force-stop %s failed: %s", pkg, exc)


def parse_bounds(raw: str) -> Bounds:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
    if not match:
        return Bounds(0, 0, 0, 0)
    return Bounds(*(int(part) for part in match.groups()))


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    text = str(value).strip()
    return [text] if text else []


def _resolve_device(gopay_cfg: dict, transfer_cfg: dict) -> str:
    device = str(transfer_cfg.get("device") or transfer_cfg.get("adb_device") or "").strip()
    if device:
        return device

    protected = gopay_cfg.get("protected_emulators") or gopay_cfg.get("protected_emulator") or {}
    if isinstance(protected, dict):
        devices = _as_list(protected.get("devices") or protected.get("device") or protected.get("serials"))
        if devices:
            return devices[0]
        indexes = _as_list(protected.get("indexes") or protected.get("index"))
        if indexes and indexes[0].isdigit():
            return f"emulator-{5554 + int(indexes[0]) * 2}"
    return ""


def _full_phone(country_code: str, phone_number: str) -> str:
    cc = re.sub(r"\D", "", str(country_code or ""))
    phone = re.sub(r"\D", "", str(phone_number or ""))
    if not phone:
        return ""
    if cc and phone.startswith(cc):
        return phone
    return f"{cc}{phone}" if cc else phone


class MainGoPayTransferFlow:
    def __init__(self, adb: Adb, cfg: dict, log: logging.Logger):
        self.adb = adb
        self.cfg = cfg
        self.log = log
        self.package = str(cfg.get("package") or DEFAULT_PACKAGE)
        self.pin = str(cfg.get("pin") or "211314")
        self.timeout = max(30, int(cfg.get("timeout_seconds") or cfg.get("timeout") or 180))

    def wait_state(self, predicate: Callable[[UiState], bool], label: str, timeout: int = 30) -> UiState:
        deadline = time.time() + timeout
        last_text = ""
        while time.time() < deadline:
            state = self.adb.dump_ui()
            last_text = state.text[:400]
            if predicate(state):
                return state
            time.sleep(1)
        raise MainTransferError(f"timeout waiting for {label}; screen={last_text!r}")

    def tap_text(self, state: UiState, *labels: str) -> bool:
        node = state.find(*labels)
        if not node:
            return False
        self.log.info("[main-transfer] tap %r", node.label)
        self.adb.tap(node.bounds.cx, node.bounds.cy)
        return True

    def tap_exact_text(self, state: UiState, *labels: str) -> bool:
        node = state.find(*labels, exact=True)
        if not node:
            return False
        self.log.info("[main-transfer] tap exact %r", node.label)
        self.adb.tap(node.bounds.cx, node.bounds.cy)
        return True

    def tap_lowest_text(self, state: UiState, *labels: str) -> bool:
        lowered = [label.lower() for label in labels if label]
        matches = [
            node for node in state.nodes
            if node.enabled and any(label in node.label.lower() for label in lowered)
        ]
        if not matches:
            return False
        node = max(matches, key=lambda item: item.bounds.cy)
        self.log.info("[main-transfer] tap lowest %r", node.label)
        self.adb.tap(node.bounds.cx, node.bounds.cy)
        return True

    def dismiss_home_popups(self) -> None:
        try:
            state = self.adb.dump_ui()
        except Exception:
            return
        if state.contains("play", "dapet coins", "coins"):
            self.log.info("[main-transfer] closing home popup")
            self.adb.tap_rel(state, 0.91, 0.71)

    def open_transfer_home(self) -> None:
        self.adb.force_stop_gopay(self.package)
        self.adb.start_package(self.package)
        state = self.wait_state(lambda s: s.contains("free transfer", "transfer"), "GoPay home", timeout=35)
        self.dismiss_home_popups()
        state = self.adb.dump_ui()
        if not self.tap_text(state, "Free transfer"):
            self.log.info("[main-transfer] Free transfer text not found; tapping home shortcut fallback")
            self.adb.tap_rel(state, 0.17, 0.61)

    def choose_gopay_transfer(self) -> None:
        state = self.wait_state(
            lambda s: s.contains("Transfer to others") or s.contains("Transfer to new recipient", "Enter name or phone number"),
            "Transfer page",
            timeout=25,
        )
        if state.contains("Transfer to new recipient", "Enter name or phone number"):
            self.log.info("[main-transfer] GoPay recipient page is already open")
            return
        self.log.info("[main-transfer] tapping GoPay under Transfer to others")
        self.adb.tap_rel(state, 0.80, 0.68)

    def enter_recipient(self, full_phone: str) -> None:
        state = self.wait_state(lambda s: s.contains("Transfer to new recipient", "Enter name or phone number"), "GoPay recipient page", timeout=25)
        self.log.info("[main-transfer] recipient page ready; input phone %s", full_phone)
        field = state.find_edit_text()
        if field:
            x = int(state.width * 0.50)
            y = min(field.bounds.top + 95, int(state.height * 0.38))
            self.log.info("[main-transfer] focusing phone search field at %s,%s", x, y)
            self.adb.tap(x, y)
        else:
            node = state.find("Enter name or phone number")
            if node:
                self.adb.tap(node.bounds.cx, node.bounds.cy)
            else:
                self.adb.tap_rel(state, 0.45, 0.38)
        time.sleep(0.8)
        self.adb.clear_text()
        self.adb.text(full_phone)
        self.log.info("[main-transfer] phone entered; waiting for transfer-to result")
        state = self.wait_state(lambda s: s.contains("Tap here to transfer to", full_phone[-6:]), "recipient search result", timeout=20)
        node = state.find("Tap here to transfer to")
        if node:
            self.log.info("[main-transfer] tapping transfer-to result")
            self.adb.tap(node.bounds.cx, node.bounds.cy)
        else:
            self.adb.tap_rel(state, 0.45, 0.71)

    def verify_and_trust(self) -> None:
        state = self.wait_state(
            lambda s: s.contains("Registered phone number", "Verify", "Review transfer", "Rp"),
            "Verify or amount page",
            timeout=25,
        )
        if state.contains("Review transfer", "Rp") and not state.contains("Verify"):
            self.log.info("[main-transfer] verify/trust skipped; amount page is already open")
            return
        if not self.tap_text(state, "Verify"):
            self.adb.tap_rel(state, 0.50, 0.50)
        state = self.wait_state(
            lambda s: s.contains("Do you trust the owner", "Trust, continue", "Trust", "Review transfer", "Rp"),
            "Trust prompt or amount page",
            timeout=25,
        )
        if state.contains("Review transfer", "Rp") and not state.contains("Trust"):
            self.log.info("[main-transfer] trust skipped; amount page is already open")
            return
        if not self.tap_lowest_text(state, "Trust, continue"):
            self.adb.tap_rel(state, 0.50, 0.94)

    def input_amount(self, amount: int) -> None:
        state = self.wait_state(lambda s: s.contains("Review transfer") or s.contains("Rp"), "Amount page", timeout=25)
        self.adb.clear_text(presses=8)
        self.adb.digits(str(amount))
        time.sleep(0.8)
        state = self.adb.dump_ui()
        if not self.tap_text(state, "Review transfer"):
            self.adb.tap_rel(state, 0.50, 0.96)

    def confirm_transfer(self) -> None:
        last_text = ""
        for attempt in range(1, 4):
            state = self.wait_state(
                lambda s: s.contains("Transfer amount", "Admin fee", "Free admin fee", "Transfer"),
                "Review page",
                timeout=25,
            )
            last_text = state.text[:400]
            if state.contains("Enter your PIN", "6-digit PIN"):
                return
            self.log.info("[main-transfer] tapping final Transfer button attempt=%s", attempt)
            if not self.tap_lowest_text(state, "Transfer"):
                self.adb.tap_rel(state, 0.50, 0.96)
            deadline = time.time() + 8
            while time.time() < deadline:
                state = self.adb.dump_ui()
                last_text = state.text[:400]
                if state.contains("Enter your PIN", "6-digit PIN"):
                    return
                if self.gopay_error_visible(state):
                    raise MainTransferError(f"GoPay error after final Transfer tap; screen={last_text!r}")
                time.sleep(1)
            self.log.warning("[main-transfer] final Transfer tap did not open PIN page; retrying")
        raise MainTransferError(f"final Transfer did not open PIN page after retries; screen={last_text!r}")

    def transfer_success_visible(self, state: UiState) -> bool:
        return state.contains("successful", "success", "Transfer details", "Transaction details", "Share receipt")

    def pin_error_visible(self, state: UiState) -> bool:
        return state.contains(
            "wrong PIN",
            "incorrect PIN",
            "invalid PIN",
            "PIN is incorrect",
            "PIN salah",
            "too many attempts",
            "cool down",
            "try again in",
        )

    def gopay_error_visible(self, state: UiState) -> bool:
        return state.contains(
            "Technical Issue",
            "technical error",
            "something went wrong",
            "try again later",
            "unable to process",
            "failed",
            "insufficient balance",
            "not enough balance",
        )

    def technical_issue_visible(self, state: UiState) -> bool:
        return state.contains("Technical Issue", "technical error", "(C07)")

    def dismiss_technical_issue(self, state: UiState) -> None:
        if self.tap_exact_text(state, "Try again", "Retry", "Got it", "Dismiss"):
            return
        self.log.info("[main-transfer] technical issue button not found; tapping fallback bottom area")
        self.adb.tap_rel(state, 0.50, 0.90)

    def enter_pin_and_wait_success(self) -> None:
        state = self.wait_state(lambda s: s.contains("Enter your PIN", "6-digit PIN"), "PIN page", timeout=25)
        self.log.info("[main-transfer] entering PIN")
        self.tap_pin_digits(state, self.pin)
        deadline = time.time() + 35
        last_text = ""
        while time.time() < deadline:
            state = self.adb.dump_ui()
            last_text = state.text[:400]
            if self.transfer_success_visible(state):
                self.log.info("[main-transfer] transfer success screen detected")
                return
            if self.pin_error_visible(state):
                raise MainTransferError(f"transfer PIN rejected or rate-limited; screen={last_text!r}")
            if self.technical_issue_visible(state):
                raise MainTransferTechnicalIssue(f"GoPay technical issue after transfer PIN; screen={last_text!r}")
            if self.gopay_error_visible(state):
                raise MainTransferError(f"GoPay error after transfer PIN; screen={last_text!r}")
            if not state.contains("Enter your PIN", "6-digit PIN"):
                self.log.info("[main-transfer] PIN page left; waiting for explicit transfer result")
            time.sleep(1)
        raise MainTransferError(f"transfer success not confirmed after PIN; screen={last_text!r}")

    def tap_pin_digits(self, state: UiState, pin: str) -> None:
        positions = {
            "1": (0.17, 0.60),
            "2": (0.50, 0.60),
            "3": (0.83, 0.60),
            "4": (0.17, 0.71),
            "5": (0.50, 0.71),
            "6": (0.83, 0.71),
            "7": (0.17, 0.82),
            "8": (0.50, 0.82),
            "9": (0.83, 0.82),
            "0": (0.50, 0.94),
        }
        for digit in str(pin):
            pos = positions.get(digit)
            if not pos:
                continue
            self.adb.tap_rel(state, pos[0], pos[1])
            time.sleep(0.12)

    def run(self, full_phone: str, amount: int) -> dict:
        self.log.info("[main-transfer] start recipient=***%s amount=Rp%s", full_phone[-4:], amount)
        try:
            limit = max(1, int(self.cfg.get("technical_issue_retry_limit") or 5))
            for attempt in range(1, limit + 1):
                try:
                    self.open_transfer_home()
                    self.choose_gopay_transfer()
                    self.enter_recipient(full_phone)
                    self.verify_and_trust()
                    self.input_amount(amount)
                    self.confirm_transfer()
                    self.enter_pin_and_wait_success()
                    return {"ok": True, "recipient": full_phone, "amount": amount, "device": self.adb.device}
                except MainTransferTechnicalIssue as exc:
                    if attempt >= limit:
                        raise MainTransferError(f"GoPay Technical Issue persisted after {limit} main-transfer retries: {exc}") from exc
                    self.log.warning(
                        "[main-transfer] Technical Issue detected; switching Clash node before retry %s/%s",
                        attempt,
                        limit,
                    )
                    try:
                        cfg = clash_verge_rotator.load_config(ROOT / "config.json")
                        if cfg.enabled:
                            selected = clash_verge_rotator.switch_once(cfg, self.log)
                            self.log.info("[main-transfer] Clash node switched after Technical Issue: %s", selected)
                        else:
                            self.log.warning("[main-transfer] Clash Verge rotation disabled; retrying without node switch")
                    except Exception as switch_exc:
                        self.log.warning("[main-transfer] Clash node switch after Technical Issue failed: %s", switch_exc)
                    self.log.info("[main-transfer] waiting 10s before dismissing error and retrying transfer")
                    time.sleep(10)
                    state = self.adb.dump_ui()
                    self.dismiss_technical_issue(state)
                    time.sleep(2)
            raise MainTransferError("main transfer retry loop exited unexpectedly")
        finally:
            self.adb.force_stop_gopay(self.package)


def run_main_transfer(
    gopay_cfg: dict,
    country_code: str,
    phone_number: str,
    log=None,
    amount_override: int | None = None,
) -> dict:
    transfer_cfg = dict((gopay_cfg or {}).get("main_transfer") or {})
    if not _as_bool(transfer_cfg.get("enabled"), default=False):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    if isinstance(log, logging.Logger):
        logger = log
    elif callable(log):
        logger = CallableLogAdapter(log)
    else:
        logger = logging.getLogger("main-transfer")

    full_phone = _full_phone(country_code, phone_number)
    if not full_phone:
        raise MainTransferError("main_transfer recipient phone is empty")

    device = _resolve_device(gopay_cfg, transfer_cfg)
    if not device:
        raise MainTransferError("main_transfer needs device or gopay.protected_emulators.devices/indexes")

    adb_path = Path(str(transfer_cfg.get("adb_path") or DEFAULT_ADB))
    if not adb_path.exists():
        raise MainTransferError(f"adb not found: {adb_path}")

    min_amount = int(transfer_cfg.get("amount_min") or 150)
    max_amount = int(transfer_cfg.get("amount_max") or 300)
    if min_amount > max_amount:
        min_amount, max_amount = max_amount, min_amount
    amount = int(amount_override) if amount_override is not None else random.randint(min_amount, max_amount)

    pin = str(transfer_cfg.get("pin") or (gopay_cfg or {}).get("main_pin") or "211314")
    flow_cfg = dict(transfer_cfg)
    flow_cfg["pin"] = pin
    adb = Adb(adb_path, device, logger)
    flow = MainGoPayTransferFlow(adb, flow_cfg, logger)
    lock = MainTransferDeviceLock(
        device=device,
        enabled=_as_bool(transfer_cfg.get("lock_enabled"), default=True),
        wait_timeout_seconds=int(transfer_cfg.get("lock_wait_timeout_seconds") or 600),
        stale_seconds=int(transfer_cfg.get("lock_stale_seconds") or 900),
        logger=logger,
    )
    with lock:
        return flow.run(full_phone, amount)


def _load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test main-emulator GoPay transfer via ADB.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "config.json"),
        help="Path to root config.json",
    )
    parser.add_argument("--phone-number", required=True, help="Recipient phone, with or without country code")
    parser.add_argument("--country-code", default="", help="Country code override; default reads gopay.country_code")
    parser.add_argument("--amount", type=int, default=0, help="Fixed transfer amount; default uses config range")
    parser.add_argument("--device", default="", help="ADB device override")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main-transfer")
    cfg = _load_config(Path(args.config))
    gopay_cfg = dict(cfg.get("gopay") or {})
    transfer_cfg = dict(gopay_cfg.get("main_transfer") or {})
    transfer_cfg["enabled"] = True
    if args.device:
        transfer_cfg["device"] = args.device
    gopay_cfg["main_transfer"] = transfer_cfg
    country_code = args.country_code or str(gopay_cfg.get("country_code") or "")
    result = run_main_transfer(
        gopay_cfg,
        country_code=country_code,
        phone_number=args.phone_number,
        log=log,
        amount_override=args.amount or None,
    )
    log.info("[main-transfer] result=%s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
