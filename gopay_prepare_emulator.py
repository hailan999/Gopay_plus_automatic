#!/usr/bin/env python3
"""Prepare an LDPlayer instance for the GoPay registration flow."""

from __future__ import annotations

import argparse
import logging
import random
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DEFAULT_LD_DIR = Path(r"E:\leidian\LDPlayer9")
DEFAULT_MT_APK = Path(r"C:\Users\Administrator\Downloads\MT2.26.4.apk")
DEFAULT_GOPAY_APKS = Path(r"C:\Users\Administrator\Downloads\GoPay_2.7.0.apks")
GOPAY_PACKAGE_CANDIDATES = ("com.gojek.gopay", "com.gojek.app", "com.go-jek.ios")


class PrepareError(RuntimeError):
    pass


def run_cmd(cmd: list[str], logger: logging.Logger, timeout: int = 120) -> str:
    logger.debug("run: %s", " ".join(str(x) for x in cmd))
    proc = subprocess.run(
        [str(x) for x in cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise PrepareError(f"command failed: {' '.join(str(x) for x in cmd)} :: {err or out}")
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


def adb_serial(ld: Path, index: str, logger: logging.Logger) -> str:
    serial = ld_adb(ld, index, "get-serialno", logger, timeout=30).strip()
    if serial and serial.lower() not in {"unknown", "offline"}:
        return serial
    # LDPlayer usually maps index N to emulator-(5554 + 2N). Keep this as a
    # fallback only; ldconsole get-serialno is the source of truth when it works.
    if str(index).isdigit():
        return f"emulator-{5554 + int(index) * 2}"
    raise PrepareError(f"cannot resolve adb serial for LDPlayer index={index}")


def prepare(args: argparse.Namespace, logger: logging.Logger) -> dict[str, str]:
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


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare LDPlayer for GoPay ADB registration.")
    parser.add_argument("--ld-dir", default=str(DEFAULT_LD_DIR), help="LDPlayer9 directory")
    parser.add_argument("--name", default="", help="LDPlayer instance name to create/use")
    parser.add_argument("--index", default="", help="Use an existing LDPlayer instance index")
    parser.add_argument("--create", action="store_true", help="Create instance when --name does not exist")
    parser.add_argument("--unique-name", action="store_true", help="When creating, auto-suffix the name if it already exists")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--dpi", type=int, default=480)
    parser.add_argument("--mt-apk", default=str(DEFAULT_MT_APK))
    parser.add_argument("--gopay-apks", default=str(DEFAULT_GOPAY_APKS))
    parser.add_argument("--boot-timeout", type=int, default=180)
    parser.add_argument("--open-mt", action="store_true", help="Open MT Manager after installation")
    parser.add_argument("--print-device", action="store_true", help="Print PREPARED_DEVICE=<serial> after success")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


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
