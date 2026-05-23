#!/usr/bin/env python3
"""Shared emulator and ADB helpers for registration tooling."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol


DEFAULT_LD_DIR = Path(r"E:\leidian\LDPlayer9")
DEFAULT_BLUESTACKS_DIR = Path(r"C:\Program Files\BlueStacks_nxt_cn")
DEFAULT_CONNECT_PORTS = (5555, 5557, 5559, 5561, 5575, 5585, 5595, 5605, 5615, 5625, 7555)
DEFAULT_BLUESTACKS_IMAGE = "Pie64"


@dataclass
class BlueStacksInstance:
    name: str
    display_name: str = ""
    adb_port: int | None = None
    status_adb_port: int | None = None

    @property
    def connect_serial(self) -> str:
        port = self.adb_port or self.status_adb_port
        return f"127.0.0.1:{port}" if port else ""


class LoggerLike(Protocol):
    def info(self, msg: str, *args) -> None: ...
    def warning(self, msg: str, *args) -> None: ...
    def debug(self, msg: str, *args) -> None: ...


class EmulatorSupportError(RuntimeError):
    pass


def normalize_emulator(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "").replace("_", "")
    if text in {"ld", "ldplayer", "leidian"}:
        return "ldplayer"
    if text in {"bs", "bstack", "bstacks", "bluestack", "bluestacks"}:
        return "bluestacks"
    if text in {"adb", "generic", "android"}:
        return "adb"
    return text or "ldplayer"


def parse_ports(value, default: Iterable[int] = DEFAULT_CONNECT_PORTS) -> list[int]:
    if value is None or value == "":
        return list(default)
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    ports: list[int] = []
    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue
        if ":" in text:
            text = text.rsplit(":", 1)[1]
        try:
            port = int(text)
        except ValueError:
            continue
        if port not in ports:
            ports.append(port)
    return ports or list(default)


def adb_candidates(
    *,
    value: str = "",
    emulator: str = "",
    ld_dir: str | Path = "",
    bs_dir: str | Path = "",
) -> list[Path]:
    emulator = normalize_emulator(emulator)
    candidates: list[Path] = []
    if value:
        candidates.append(Path(value))
    if emulator == "bluestacks":
        if bs_dir:
            candidates.append(Path(bs_dir) / "HD-Adb.exe")
        candidates.append(DEFAULT_BLUESTACKS_DIR / "HD-Adb.exe")
    if ld_dir:
        candidates.append(Path(ld_dir) / "adb.exe")
    candidates.append(DEFAULT_LD_DIR / "adb.exe")
    env_path = os.environ.get("PATH", "")
    exe = "adb.exe" if os.name == "nt" else "adb"
    for folder in env_path.split(os.pathsep):
        if folder:
            candidates.append(Path(folder) / exe)
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def find_adb_path(
    value: str = "",
    *,
    emulator: str = "",
    ld_dir: str | Path = "",
    bs_dir: str | Path = "",
) -> Path:
    for candidate in adb_candidates(value=value, emulator=emulator, ld_dir=ld_dir, bs_dir=bs_dir):
        if candidate.exists():
            return candidate
    raise EmulatorSupportError("adb not found; pass --adb-path or configure the emulator directory")


def parse_adb_devices(output: str) -> list[str]:
    devices: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def run_adb(
    adb_path: Path,
    args: list[str],
    *,
    device: str = "",
    timeout: int = 20,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    cmd = [str(adb_path)]
    if device and args[:1] != ["connect"]:
        cmd += ["-s", device]
    cmd += [str(arg) for arg in args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        timeout=timeout,
        check=False,
    )


def adb_error_text(proc: subprocess.CompletedProcess) -> str:
    stdout = proc.stdout if isinstance(proc.stdout, str) else ""
    stderr = proc.stderr if isinstance(proc.stderr, str) else ""
    return (stderr or stdout or "").strip()


def is_transient_adb_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "device offline",
            "error: closed",
            "server not running",
            "daemon not running",
            "cannot connect to daemon",
            "device not found",
        )
    )


def recover_adb(adb_path: Path, device: str = "", logger: LoggerLike | None = None) -> None:
    if logger:
        logger.info("Recovering ADB connection%s", f" for {device}" if device else "")
    try:
        run_adb(adb_path, ["kill-server"], timeout=10)
    except subprocess.TimeoutExpired:
        if logger:
            logger.warning("adb kill-server timed out during recovery")
    time.sleep(1)
    try:
        run_adb(adb_path, ["start-server"], timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise EmulatorSupportError("adb start-server timed out during recovery") from exc
    if ":" in str(device or ""):
        try:
            run_adb(adb_path, ["connect", device], timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise EmulatorSupportError(f"adb connect timed out during recovery for {device}") from exc
    if device:
        try:
            run_adb(adb_path, ["wait-for-device"], device=device, timeout=20)
        except subprocess.TimeoutExpired as exc:
            raise EmulatorSupportError(f"adb wait-for-device timed out during recovery for {device}") from exc
    time.sleep(0.5)


def check_adb(
    adb_path: Path,
    args: list[str],
    *,
    device: str = "",
    timeout: int = 20,
    retries: int = 2,
    logger: LoggerLike | None = None,
) -> str:
    last_text = ""
    for attempt in range(retries + 1):
        proc = run_adb(adb_path, args, device=device, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout or ""
        last_text = adb_error_text(proc)
        if attempt >= retries or not is_transient_adb_error(last_text):
            break
        if logger:
            logger.warning("ADB command failed transiently (%s); retrying %s/%s", last_text, attempt + 1, retries)
        recover_adb(adb_path, device=device, logger=logger)
    raise EmulatorSupportError(
        f"adb command failed: {' '.join(args)} :: {last_text}"
    )


def connect_adb_device(
    adb_path: Path,
    requested: str = "",
    *,
    ports: Iterable[int] = DEFAULT_CONNECT_PORTS,
    logger: LoggerLike | None = None,
    protected_devices: set[str] | None = None,
) -> str:
    protected_devices = protected_devices or set()
    requested = str(requested or "").strip()
    if requested:
        if logger:
            logger.info("Using requested ADB device %s", requested)
        if requested.strip().lower() in protected_devices:
            raise EmulatorSupportError(
                f"refusing to use protected ADB device {requested}; choose another emulator/device"
            )
        if ":" in requested:
            try:
                run_adb(adb_path, ["connect", requested], timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise EmulatorSupportError(f"adb connect timed out for {requested}") from exc
            time.sleep(0.8)
        try:
            check = run_adb(adb_path, ["get-state"], device=requested, timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise EmulatorSupportError(f"adb get-state timed out for {requested}") from exc
        if check.returncode != 0 or "device" not in (check.stdout or ""):
            raise EmulatorSupportError(
                f"requested ADB device is not ready: {requested} :: "
                f"{(check.stderr or check.stdout or '').strip()}"
            )
        return requested

    out = check_adb(adb_path, ["devices"], timeout=15)
    devices = [device for device in parse_adb_devices(out) if device.strip().lower() not in protected_devices]
    if devices:
        if logger:
            logger.info("Using connected ADB device %s", devices[0])
        return devices[0]

    tried: list[str] = []
    for port in ports:
        target = f"127.0.0.1:{int(port)}"
        tried.append(target)
        if logger:
            logger.info("Trying adb connect %s", target)
        try:
            run_adb(adb_path, ["connect", target], timeout=10)
        except subprocess.TimeoutExpired:
            if logger:
                logger.info("adb connect timed out for %s", target)
            continue
        time.sleep(0.8)
        out = check_adb(adb_path, ["devices"], timeout=15)
        devices = [device for device in parse_adb_devices(out) if device.strip().lower() not in protected_devices]
        if devices:
            if logger:
                logger.info("Connected ADB device %s", devices[0])
            return devices[0]

    raise EmulatorSupportError(
        "No ADB device connected. Open the emulator and enable ADB debugging, then retry. "
        f"Tried: {', '.join(tried)}"
    )


def connect_adb_device_wait(
    adb_path: Path,
    requested: str = "",
    *,
    ports: Iterable[int] = DEFAULT_CONNECT_PORTS,
    timeout: int = 120,
    logger: LoggerLike | None = None,
    protected_devices: set[str] | None = None,
) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            return connect_adb_device(
                adb_path,
                requested,
                ports=ports,
                logger=logger,
                protected_devices=protected_devices,
            )
        except EmulatorSupportError as exc:
            last = str(exc)
            if logger:
                logger.info("Waiting for ADB device %s: %s", requested or "<auto>", last)
            time.sleep(3)
    raise EmulatorSupportError(f"ADB device was not ready within {timeout}s: {last}")


def bluestacks_player(bs_dir: str | Path = "") -> Path:
    base = Path(bs_dir) if bs_dir else DEFAULT_BLUESTACKS_DIR
    path = base / "HD-Player.exe"
    if not path.exists():
        raise EmulatorSupportError(f"HD-Player.exe not found: {path}")
    return path


def bluestacks_manager(bs_dir: str | Path = "") -> Path:
    base = Path(bs_dir) if bs_dir else DEFAULT_BLUESTACKS_DIR
    path = base / "HD-MultiInstanceManager.exe"
    if not path.exists():
        raise EmulatorSupportError(f"HD-MultiInstanceManager.exe not found: {path}")
    return path


def _registry_value(key_path: str, value_name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
    except Exception:
        return ""
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
                return str(value or "")
        except OSError:
            continue
    return ""


def bluestacks_data_root(bs_dir: str | Path = "") -> Path:
    user_dir = _registry_value(r"SOFTWARE\BlueStacks_nxt_cn", "UserDefinedDir")
    if user_dir:
        return Path(user_dir)
    data_dir = _registry_value(r"SOFTWARE\BlueStacks_nxt_cn", "DataDir")
    if data_dir:
        data_path = Path(data_dir)
        return data_path.parent if data_path.name.lower() == "engine" else data_path
    base = Path(bs_dir) if bs_dir else DEFAULT_BLUESTACKS_DIR
    return base.parent / "BlueStacks_nxt_cn"


def bluestacks_conf_path(bs_dir: str | Path = "") -> Path:
    candidates = [
        bluestacks_data_root(bs_dir) / "bluestacks.conf",
        Path(r"E:\programs\BlueStacks_nxt_cn\bluestacks.conf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise EmulatorSupportError(
        "BlueStacks config not found; check the BlueStacks data directory"
    )


def _parse_conf_value(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def parse_bluestacks_conf(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _parse_conf_value(value)
    return values


def bluestacks_instances(bs_dir: str | Path = "") -> list[BlueStacksInstance]:
    conf = parse_bluestacks_conf(bluestacks_conf_path(bs_dir))
    installed = [
        item.strip()
        for item in conf.get("bst.installed_images", "").split(",")
        if item.strip()
    ]
    names: set[str] = set(installed)
    prefix = "bst.instance."
    for key in conf:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        if "." in rest:
            names.add(rest.split(".", 1)[0])

    instances: list[BlueStacksInstance] = []
    for name in sorted(names):
        if not name:
            continue
        display = conf.get(f"bst.instance.{name}.display_name", "")
        adb_port = _int_or_none(conf.get(f"bst.instance.{name}.adb_port"))
        status_adb_port = _int_or_none(conf.get(f"bst.instance.{name}.status.adb_port"))
        instances.append(
            BlueStacksInstance(
                name=name,
                display_name=display,
                adb_port=adb_port,
                status_adb_port=status_adb_port,
            )
        )
    return instances


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def find_bluestacks_instance(
    name: str = "",
    *,
    bs_dir: str | Path = "",
    index: str = "",
) -> BlueStacksInstance | None:
    instances = bluestacks_instances(bs_dir)
    ident = str(name or "").strip().lower()
    if ident:
        for instance in instances:
            if instance.name.lower() == ident or instance.display_name.lower() == ident:
                return instance
        return None
    index_text = str(index or "").strip()
    if index_text.isdigit():
        idx = int(index_text)
        if 0 <= idx < len(instances):
            return instances[idx]
    return instances[0] if instances else None


def wait_for_bluestacks_instance_count(
    before_names: set[str],
    *,
    before_ports: set[int] | None = None,
    bs_dir: str | Path = "",
    timeout: int = 240,
) -> BlueStacksInstance:
    before_ports = before_ports or set()
    deadline = time.time() + timeout
    last_instances: list[BlueStacksInstance] = []
    pending_created: BlueStacksInstance | None = None
    while time.time() < deadline:
        try:
            last_instances = bluestacks_instances(bs_dir)
        except EmulatorSupportError:
            last_instances = []
        created = [
            item
            for item in last_instances
            if item.name not in before_names
            and item.connect_serial
            and ((item.adb_port or item.status_adb_port) not in before_ports)
        ]
        if created:
            return sorted(created, key=lambda item: item.name)[-1]
        new_instances = [item for item in last_instances if item.name not in before_names]
        if new_instances:
            pending_created = sorted(new_instances, key=lambda item: item.name)[-1]
            pending_port = pending_created.adb_port or pending_created.status_adb_port
            if pending_created.connect_serial and pending_port not in before_ports:
                return pending_created
        time.sleep(3)
    if pending_created:
        raise EmulatorSupportError(
            "BlueStacks instance was created but no ADB port appeared within "
            f"{timeout}s: {pending_created.name}"
        )
    known = ", ".join(item.name for item in last_instances) or "<none>"
    raise EmulatorSupportError(
        f"BlueStacks instance was not created within {timeout}s; known instances: {known}"
    )


def create_bluestacks_instance(
    *,
    bs_dir: str | Path = "",
    image_name: str = DEFAULT_BLUESTACKS_IMAGE,
    clone_from: str = "",
    timeout: int = 240,
    logger: LoggerLike | None = None,
) -> BlueStacksInstance:
    manager = bluestacks_manager(bs_dir)
    before_instances = bluestacks_instances(bs_dir)
    before = {item.name for item in before_instances}
    before_ports = {item.status_adb_port or item.adb_port for item in before_instances}
    before_ports.discard(None)
    if clone_from:
        cmd = [
            str(manager),
            "--cmd",
            "createCloneInstance",
            "--cloneFrom",
            str(clone_from),
        ]
        label = f"cloneFrom={clone_from}"
    else:
        cmd = [
            str(manager),
            "--cmd",
            "createFreshInstance",
            "--imageName",
            str(image_name or DEFAULT_BLUESTACKS_IMAGE),
        ]
        label = f"imageName={image_name or DEFAULT_BLUESTACKS_IMAGE}"
    if logger:
        logger.info("Creating BlueStacks instance via multi-instance manager %s", label)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return wait_for_bluestacks_instance_count(
        before,
        before_ports=before_ports,
        bs_dir=bs_dir,
        timeout=timeout,
    )
