#!/usr/bin/env python3
"""Prepare an Android emulator instance for the GoPay registration flow."""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

import emulator_support


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DEFAULT_LD_DIR = emulator_support.DEFAULT_LD_DIR
DEFAULT_BS_DIR = emulator_support.DEFAULT_BLUESTACKS_DIR
DEFAULT_MT_APK = Path(r"C:\Users\Administrator\Downloads\MT2.26.4.apk")
DEFAULT_GOPAY_APKS = Path(r"C:\Users\Administrator\Downloads\GoPay_2.8.0.apks")
GOPAY_PACKAGE_CANDIDATES = ("com.gojek.gopay", "com.gojek.app", "com.go-jek.ios")


class PrepareError(RuntimeError):
    pass


def kill_process_tree(proc: subprocess.Popen, logger: logging.Logger) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            return
        except Exception as exc:
            logger.warning("Failed to taskkill timed-out process pid=%s: %s", proc.pid, exc)
    try:
        proc.kill()
    except Exception as exc:
        logger.warning("Failed to kill timed-out process pid=%s: %s", proc.pid, exc)


def run_cmd(cmd: list[str], logger: logging.Logger, timeout: int = 120) -> str:
    cmd_text = " ".join(str(x) for x in cmd)
    logger.debug("run: %s", cmd_text)
    proc = subprocess.Popen(
        [str(x) for x in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(proc, logger)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        detail = ((stderr or "") + "\n" + (stdout or "")).strip()
        raise PrepareError(f"command timed out after {timeout}s: {cmd_text} :: {detail[:500]}") from exc
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    if proc.returncode != 0:
        raise PrepareError(f"command failed: {cmd_text} :: {err or out}")
    return out


def ldconsole(ld_dir: Path) -> Path:
    path = ld_dir / "ldconsole.exe"
    if not path.exists():
        raise PrepareError(f"ldconsole.exe not found: {path}")
    return path


def list_instances(ld: Path, logger: logging.Logger) -> list[dict[str, str]]:
    out = run_cmd([str(ld), "list2"], logger, timeout=30)
    items: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        items.append({"index": parts[0], "name": parts[1], "raw": line})
    return items


def find_instance(ld: Path, name: str, logger: logging.Logger) -> str:
    for item in list_instances(ld, logger):
        if item["name"] == name:
            return item["index"]
    return ""


def unique_instance_name(base_name: str, items: list[dict[str, str]]) -> str:
    names = {item["name"] for item in items}
    if base_name not in names:
        return base_name
    prefix = re.sub(r"-\d+$", "", base_name).strip("-") or "gopay-auto"
    for _ in range(20):
        candidate = f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{random.randint(100000, 999999)}"
        if candidate not in names:
            return candidate
    raise PrepareError("cannot generate a unique LDPlayer instance name")


def ensure_instance(args: argparse.Namespace, ld: Path, logger: logging.Logger) -> str:
    if args.index != "":
        return str(args.index)
    name = args.name.strip()
    if not name:
        name = f"gopay-auto-{datetime.now().strftime('%m%d-%H%M%S')}"
        args.name = name
    items = list_instances(ld, logger)
    existing = ""
    for item in items:
        if item["name"] == name:
            existing = item["index"]
            break
    if existing and args.unique_name and args.create:
        new_name = unique_instance_name(name, items)
        logger.info("LDPlayer instance name=%s already exists; using unique name=%s", name, new_name)
        name = new_name
        args.name = name
        existing = ""
    if existing:
        logger.info("Using existing LDPlayer instance name=%s index=%s", name, existing)
        return existing
    if not args.create:
        raise PrepareError(f"LDPlayer instance not found: {name}; pass --create to create it")
    logger.info("Creating LDPlayer instance name=%s", name)
    try:
        run_cmd([str(ld), "add", "--name", name], logger, timeout=120)
    except PrepareError as exc:
        # Some LDPlayer builds create the instance but still return a non-zero
        # exit code with an empty error message. Re-read list2 before failing.
        created = find_instance(ld, name, logger)
        if created:
            logger.warning(
                "ldconsole add returned an error, but instance was created name=%s index=%s: %s",
                name,
                created,
                exc,
            )
            return created
        raise
    time.sleep(2)
    index = find_instance(ld, name, logger)
    if not index:
        raise PrepareError(f"created instance but cannot find it in list2: {name}")
    return index


def ld_adb(ld: Path, index: str, command: str, logger: logging.Logger, timeout: int = 120) -> str:
    return run_cmd([str(ld), "adb", "--index", str(index), "--command", command], logger, timeout=timeout)


def wait_for_android(ld: Path, index: str, logger: logging.Logger, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            state = ld_adb(ld, index, "get-state", logger, timeout=15)
            boot = ld_adb(ld, index, "shell getprop sys.boot_completed", logger, timeout=15).strip()
            if "device" in state and boot == "1":
                logger.info("Android booted for index=%s", index)
                return
            last = f"state={state!r} boot={boot!r}"
        except Exception as exc:
            last = str(exc)
        time.sleep(3)
    raise PrepareError(f"LDPlayer did not boot in {timeout}s: {last}")


def extract_apks(apks_path: Path, out_dir: Path, logger: logging.Logger) -> list[Path]:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(apks_path, "r") as zf:
        names = [name for name in zf.namelist() if name.endswith(".apk")]
        if not names:
            raise PrepareError(f"no .apk entries in {apks_path}")
        for name in names:
            zf.extract(name, out_dir)
    apks = [out_dir / name for name in names]
    apks.sort(key=lambda p: (0 if p.name == "base.apk" else 1, p.name))
    logger.info("Extracted %s split APKs from %s", len(apks), apks_path)
    return apks


def quote_adb_path(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def install_gopay_splits(ld: Path, index: str, apks_path: Path, logger: logging.Logger) -> None:
    extract_dir = LOG_DIR / "gopay_apks_extract" / f"{apks_path.stem}_{index}"
    apks = extract_apks(apks_path, extract_dir, logger)
    args = " ".join(quote_adb_path(path) for path in apks)
    logger.info("Installing GoPay split APKs via adb install-multiple")
    out = ld_adb(ld, index, f"install-multiple -r {args}", logger, timeout=240)
    if "Success" not in out:
        raise PrepareError(f"install-multiple did not report Success: {out[:500]}")
    logger.info("GoPay split APK install succeeded")


def installed_gopay_package(ld: Path, index: str, logger: logging.Logger) -> str:
    out = ld_adb(ld, index, "shell pm list packages", logger, timeout=30)
    packages = set()
    for line in out.splitlines():
        if line.startswith("package:"):
            packages.add(line.split(":", 1)[1].strip())
    for package in GOPAY_PACKAGE_CANDIDATES:
        if package in packages:
            return package
    return ""


def adb_check(adb_path: Path, device: str, args: list[str], logger: logging.Logger, timeout: int = 120) -> str:
    logger.debug("adb: %s -s %s %s", adb_path, device, " ".join(args))
    try:
        return emulator_support.check_adb(adb_path, args, device=device, timeout=timeout, logger=logger)
    except emulator_support.EmulatorSupportError as exc:
        raise PrepareError(str(exc)) from exc


def wait_for_adb_android(adb_path: Path, device: str, logger: logging.Logger, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            state = adb_check(adb_path, device, ["get-state"], logger, timeout=15).strip()
            boot = adb_check(adb_path, device, ["shell", "getprop sys.boot_completed"], logger, timeout=15).strip()
            if "device" in state and boot == "1":
                logger.info("Android booted for device=%s", device)
                return
            last = f"state={state!r} boot={boot!r}"
        except Exception as exc:
            last = str(exc)
        time.sleep(3)
    raise PrepareError(f"Android did not boot in {timeout}s: {last}")


def set_adb_display(
    adb_path: Path,
    device: str,
    width: int,
    height: int,
    dpi: int,
    logger: logging.Logger,
) -> None:
    logger.info("Set ADB display device=%s resolution=%sx%s dpi=%s", device, width, height, dpi)
    adb_check(adb_path, device, ["shell", f"wm size {int(width)}x{int(height)}"], logger, timeout=20)
    adb_check(adb_path, device, ["shell", f"wm density {int(dpi)}"], logger, timeout=20)


def install_gopay_splits_adb(
    adb_path: Path,
    device: str,
    apks_path: Path,
    logger: logging.Logger,
    extract_suffix: str,
) -> None:
    extract_dir = LOG_DIR / "gopay_apks_extract" / f"{apks_path.stem}_{extract_suffix}"
    apks = extract_apks(apks_path, extract_dir, logger)
    logger.info("Installing GoPay split APKs via adb install-multiple device=%s", device)
    proc = emulator_support.run_adb(
        adb_path,
        ["install-multiple", "-r"] + [str(path) for path in apks],
        device=device,
        timeout=300,
    )
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0 or "Success" not in out:
        raise PrepareError(f"install-multiple failed: {out[:500]}")
    logger.info("GoPay split APK install succeeded")


def installed_gopay_package_adb(adb_path: Path, device: str, logger: logging.Logger) -> str:
    out = adb_check(adb_path, device, ["shell", "pm list packages"], logger, timeout=30)
    packages = set()
    for line in out.splitlines():
        if line.startswith("package:"):
            packages.add(line.split(":", 1)[1].strip())
    for package in GOPAY_PACKAGE_CANDIDATES:
        if package in packages:
            return package
    return ""


def adb_serial(ld: Path, index: str, logger: logging.Logger) -> str:
    serial = ld_adb(ld, index, "get-serialno", logger, timeout=30).strip()
    if serial and serial.lower() not in {"unknown", "offline"}:
        return serial
    # LDPlayer usually maps index N to emulator-(5554 + 2N). Keep this as a
    # fallback only; ldconsole get-serialno is the source of truth when it works.
    if str(index).isdigit():
        return f"emulator-{5554 + int(index) * 2}"
    raise PrepareError(f"cannot resolve adb serial for LDPlayer index={index}")


def launch_bluestacks(args: argparse.Namespace, logger: logging.Logger, instance_name: str = "") -> None:
    try:
        player = emulator_support.bluestacks_player(args.bs_dir)
    except emulator_support.EmulatorSupportError as exc:
        raise PrepareError(str(exc)) from exc
    cmd = [str(player)]
    name = str(instance_name or args.name or "").strip()
    if name:
        cmd += ["--instance", name]
    logger.info("Launching BlueStacks%s", f" instance={name}" if name else "")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def enable_bluestacks_adb(args: argparse.Namespace, logger: logging.Logger, instance_name: str = "") -> None:
    try:
        player = emulator_support.bluestacks_player(args.bs_dir)
    except emulator_support.EmulatorSupportError as exc:
        raise PrepareError(str(exc)) from exc
    name = str(instance_name or args.name or "").strip()
    if not name:
        return
    logger.info("Enabling BlueStacks ADB access instance=%s", name)
    subprocess.Popen(
        [str(player), "--instance", name, "--cmd", "enableAdbAccess"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def set_bluestacks_display_config(
    args: argparse.Namespace,
    logger: logging.Logger,
    instance_name: str,
) -> None:
    name = str(instance_name or "").strip()
    if not name:
        return
    try:
        conf_path = emulator_support.bluestacks_conf_path(args.bs_dir)
    except emulator_support.EmulatorSupportError as exc:
        raise PrepareError(str(exc)) from exc
    updates = {
        f"bst.instance.{name}.fb_width": str(int(args.width)),
        f"bst.instance.{name}.fb_height": str(int(args.height)),
        f"bst.instance.{name}.dpi": str(int(args.dpi)),
        f"bst.instance.{name}.custom_resolution_selected": "1",
    }
    text = conf_path.read_text(encoding="utf-8", errors="replace")
    for key, value in updates.items():
        line = f'{key}="{value}"'
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(line, text)
        else:
            text = text.rstrip() + "\n" + line + "\n"
    conf_path.write_text(text, encoding="utf-8")
    logger.info(
        "Set BlueStacks config instance=%s resolution=%sx%s dpi=%s",
        name,
        args.width,
        args.height,
        args.dpi,
    )


def ensure_bluestacks_instance(args: argparse.Namespace, logger: logging.Logger) -> emulator_support.BlueStacksInstance:
    requested = bool(str(args.name or "").strip() or str(args.index or "").strip())
    instance = None if (args.create and not requested) else emulator_support.find_bluestacks_instance(args.name, bs_dir=args.bs_dir, index=args.index)
    if instance:
        logger.info(
            "Using BlueStacks instance name=%s display=%s adb=%s",
            instance.name,
            instance.display_name or "<none>",
            instance.connect_serial or "<unknown>",
        )
        return instance
    if not args.create:
        wanted = args.name or args.index or "<first>"
        raise PrepareError(f"BlueStacks instance not found: {wanted}; pass --create to create one")
    try:
        create_timeout = max(int(args.boot_timeout or 0), 420)
        created = emulator_support.create_bluestacks_instance(
            bs_dir=args.bs_dir,
            image_name=getattr(args, "bs_image", "") or emulator_support.DEFAULT_BLUESTACKS_IMAGE,
            clone_from=getattr(args, "bs_clone_from", "") or "",
            timeout=create_timeout,
            logger=logger,
        )
    except emulator_support.EmulatorSupportError as exc:
        raise PrepareError(str(exc)) from exc
    args.name = created.name
    logger.info(
        "Created BlueStacks instance name=%s display=%s adb=%s",
        created.name,
        created.display_name or "<none>",
        created.connect_serial or "<unknown>",
    )
    return created


def prepare_ldplayer(args: argparse.Namespace, logger: logging.Logger) -> dict[str, str]:
    ld_dir = Path(args.ld_dir)
    ld = ldconsole(ld_dir)
    mt_apk = Path(args.mt_apk)
    gopay_apks = Path(args.gopay_apks)
    if not mt_apk.exists():
        raise PrepareError(f"MT APK not found: {mt_apk}")
    if not gopay_apks.exists():
        raise PrepareError(f"GoPay APKS not found: {gopay_apks}")

    index = ensure_instance(args, ld, logger)
    logger.info("Set LDPlayer resolution index=%s resolution=%s,%s,%s", index, args.width, args.height, args.dpi)
    run_cmd(
        [
            str(ld),
            "modify",
            "--index",
            str(index),
            "--resolution",
            f"{args.width},{args.height},{args.dpi}",
            "--root",
            "1",
        ],
        logger,
        timeout=60,
    )

    logger.info("Launching LDPlayer index=%s", index)
    run_cmd([str(ld), "launch", "--index", str(index)], logger, timeout=60)
    wait_for_android(ld, index, logger, timeout=args.boot_timeout)
    device = adb_serial(ld, index, logger)
    logger.info("LDPlayer adb device=%s", device)

    logger.info("Installing MT Manager: %s", mt_apk)
    run_cmd([str(ld), "installapp", "--index", str(index), "--filename", str(mt_apk)], logger, timeout=240)

    logger.info("Pushing GoPay APKS to /sdcard/Pictures/")
    ld_adb(ld, index, "shell mkdir -p /sdcard/Pictures", logger, timeout=30)
    ld_adb(
        ld,
        index,
        f"push {quote_adb_path(gopay_apks)} /sdcard/Pictures/{gopay_apks.name}",
        logger,
        timeout=240,
    )

    install_gopay_splits(ld, index, gopay_apks, logger)
    package = installed_gopay_package(ld, index, logger)
    if package:
        logger.info("Installed GoPay package=%s", package)
    else:
        logger.warning("GoPay package not found in known package list; install may still have succeeded")

    if args.open_mt:
        logger.info("Opening MT Manager")
        run_cmd([str(ld), "runapp", "--index", str(index), "--packagename", "bin.mt.plus"], logger, timeout=60)

    logger.info("Preparation done. LDPlayer index=%s name=%s device=%s", index, args.name or "<existing>", device)
    return {"index": str(index), "name": str(args.name or ""), "device": device, "package": package}


def prepare_adb_emulator(args: argparse.Namespace, logger: logging.Logger) -> dict[str, str]:
    mt_apk = Path(args.mt_apk)
    gopay_apks = Path(args.gopay_apks)
    if not mt_apk.exists():
        raise PrepareError(f"MT APK not found: {mt_apk}")
    if not gopay_apks.exists():
        raise PrepareError(f"GoPay APKS not found: {gopay_apks}")

    emulator = emulator_support.normalize_emulator(args.emulator)
    try:
        adb_path = emulator_support.find_adb_path(
            args.adb_path,
            emulator=emulator,
            ld_dir=args.ld_dir,
            bs_dir=args.bs_dir,
        )
        device_hint = args.device
        bs_instance: emulator_support.BlueStacksInstance | None = None
        if emulator == "bluestacks":
            bs_instance = ensure_bluestacks_instance(args, logger)
            set_bluestacks_display_config(args, logger, bs_instance.name)
            if bs_instance.connect_serial and not device_hint:
                device_hint = bs_instance.connect_serial
            if not args.no_launch:
                launch_bluestacks(args, logger, bs_instance.name)
                time.sleep(1)
                enable_bluestacks_adb(args, logger, bs_instance.name)
                time.sleep(2)
        wait_timeout = max(int(args.boot_timeout or 0), 420) if emulator == "bluestacks" else args.boot_timeout
        device = emulator_support.connect_adb_device_wait(
            adb_path,
            device_hint,
            ports=args.connect_ports,
            timeout=wait_timeout,
            logger=logger,
        )
    except emulator_support.EmulatorSupportError as exc:
        raise PrepareError(str(exc)) from exc

    wait_timeout = max(int(args.boot_timeout or 0), 420) if emulator == "bluestacks" else args.boot_timeout
    wait_for_adb_android(adb_path, device, logger, timeout=wait_timeout)
    logger.info("ADB emulator ready emulator=%s adb=%s device=%s", emulator, adb_path, device)
    set_adb_display(adb_path, device, args.width, args.height, args.dpi, logger)

    if emulator == "bluestacks" and not args.open_mt:
        logger.info("Skipping MT Manager install for BlueStacks; GoPay split APKs install directly via adb")
    else:
        logger.info("Installing MT Manager via adb: %s", mt_apk)
        out = adb_check(adb_path, device, ["install", "-r", str(mt_apk)], logger, timeout=240)
        if "Success" not in out:
            logger.warning("MT Manager install did not print Success: %s", out[:300])

    if emulator == "bluestacks" and not args.open_mt:
        logger.info("Skipping GoPay APKS push for BlueStacks; installing host split APKs directly")
    else:
        logger.info("Pushing GoPay APKS to /sdcard/Pictures/")
        adb_check(adb_path, device, ["shell", "mkdir -p /sdcard/Pictures"], logger, timeout=30)
        adb_check(
            adb_path,
            device,
            ["push", str(gopay_apks), f"/sdcard/Pictures/{gopay_apks.name}"],
            logger,
            timeout=240,
        )

    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", device or emulator)
    install_gopay_splits_adb(adb_path, device, gopay_apks, logger, suffix)
    package = installed_gopay_package_adb(adb_path, device, logger)
    if package:
        logger.info("Installed GoPay package=%s", package)
    else:
        logger.warning("GoPay package not found in known package list; install may still have succeeded")

    if args.open_mt:
        logger.info("Opening MT Manager")
        adb_check(
            adb_path,
            device,
            ["shell", "monkey -p bin.mt.plus -c android.intent.category.LAUNCHER 1"],
            logger,
            timeout=60,
        )

    identity = bs_instance.name if bs_instance else str(args.index or "")
    logger.info("Preparation done. emulator=%s instance=%s device=%s", emulator, identity or "<none>", device)
    return {"index": str(identity or ""), "name": str(args.name or identity or ""), "device": device, "package": package}


def prepare(args: argparse.Namespace, logger: logging.Logger) -> dict[str, str]:
    emulator = emulator_support.normalize_emulator(getattr(args, "emulator", "ldplayer"))
    args.emulator = emulator
    if not hasattr(args, "connect_ports"):
        args.connect_ports = list(emulator_support.DEFAULT_CONNECT_PORTS)
    if emulator == "ldplayer":
        return prepare_ldplayer(args, logger)
    if emulator in {"bluestacks", "adb"}:
        return prepare_adb_emulator(args, logger)
    raise PrepareError(f"unsupported emulator type: {emulator}")


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare an Android emulator for GoPay ADB registration.")
    parser.add_argument("--emulator", default="ldplayer", choices=("ldplayer", "bluestacks", "adb"))
    parser.add_argument("--ld-dir", default=str(DEFAULT_LD_DIR), help="LDPlayer9 directory")
    parser.add_argument("--bs-dir", default=str(DEFAULT_BS_DIR), help="BlueStacks install directory")
    parser.add_argument("--bs-image", default=emulator_support.DEFAULT_BLUESTACKS_IMAGE, help="BlueStacks image for fresh instance creation")
    parser.add_argument("--bs-clone-from", default="", help="Clone this BlueStacks instance instead of creating a fresh one")
    parser.add_argument("--adb-path", default="", help="ADB path; BlueStacks defaults to HD-Adb.exe")
    parser.add_argument("--device", default="", help="ADB serial or host:port to use/connect")
    parser.add_argument("--connect-port", action="append", type=int, default=[], help="ADB localhost port to try")
    parser.add_argument("--no-launch", action="store_true", help="Do not launch BlueStacks before connecting")
    parser.add_argument("--name", default="", help="Emulator instance name to create/use")
    parser.add_argument("--index", default="", help="Use an existing LDPlayer instance index")
    parser.add_argument("--create", action="store_true", help="Create LDPlayer instance when --name does not exist")
    parser.add_argument("--unique-name", action="store_true", help="When creating LDPlayer, auto-suffix the name if it already exists")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--dpi", type=int, default=480)
    parser.add_argument("--mt-apk", default=str(DEFAULT_MT_APK))
    parser.add_argument("--gopay-apks", default=str(DEFAULT_GOPAY_APKS))
    parser.add_argument("--boot-timeout", type=int, default=180)
    parser.add_argument("--open-mt", action="store_true", help="Open MT Manager after installation")
    parser.add_argument("--print-device", action="store_true", help="Print PREPARED_DEVICE=<serial> after success")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    args.connect_ports = args.connect_port or list(emulator_support.DEFAULT_CONNECT_PORTS)
    return args


def main() -> int:
    args = build_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("gopay_prepare")
    try:
        result = prepare(args, logger)
        if args.print_device:
            print(f"PREPARED_INDEX={result.get('index', '')}")
            print(f"PREPARED_DEVICE={result.get('device', '')}")
        return 0
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    except PrepareError as exc:
        logger.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
