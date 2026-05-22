#!/usr/bin/env python3
"""Continuous worker pool for GoPay registration runs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import clash_verge_rotator
import emulator_support
import gopay_prepare_emulator as prep


ROOT = Path(__file__).resolve().parent
BATCH_LOG_DIR = ROOT / "logs" / "batch_register"
DEFAULT_LD_DIR = emulator_support.DEFAULT_LD_DIR
DEFAULT_BS_DIR = emulator_support.DEFAULT_BLUESTACKS_DIR
DEFAULT_MT_APK = Path(r"C:\Users\Administrator\Downloads\MT2.26.4.apk")
DEFAULT_GOPAY_APKS = Path(r"C:\Users\Administrator\Downloads\GoPay_2.8.0.apks")


prepare_lock = threading.Lock()
print_lock = threading.Lock()
stop_event = threading.Event()
active_proc_lock = threading.Lock()
active_procs: dict[int, subprocess.Popen] = {}
prompt_lock = threading.Lock()


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


def protected_emulators_from_config(cfg: dict) -> dict[str, list[str]]:
    gopay_cfg = cfg.get("gopay") or {}
    protected = gopay_cfg.get("protected_emulators") or gopay_cfg.get("protected_emulator") or {}
    if isinstance(protected, list):
        return {"names": [], "indexes": [], "devices": config_string_list(protected)}
    if not isinstance(protected, dict):
        return {"names": [], "indexes": [], "devices": config_string_list(protected)}
    return {
        "names": config_string_list(protected.get("names") or protected.get("name")),
        "indexes": config_string_list(protected.get("indexes") or protected.get("index")),
        "devices": config_string_list(
            protected.get("devices")
            or protected.get("device")
            or protected.get("serials")
            or protected.get("serial")
        ),
    }


def is_protected_index(args: argparse.Namespace, index: str) -> bool:
    protected = {str(item).strip() for item in getattr(args, "protected_indexes", []) if str(item).strip()}
    return str(index or "").strip() in protected


def start_clash_rotation_thread(cfg: dict, logger: logging.Logger) -> tuple[threading.Thread | None, threading.Event | None]:
    rotation_cfg = cfg.get("clash_verge_rotation") or {}
    if not isinstance(rotation_cfg, dict) or not rotation_cfg.get("enabled", False):
        return None, None
    rotator_cfg = clash_verge_rotator.RotationConfig(
        enabled=True,
        controller=str(rotation_cfg.get("controller") or "http://127.0.0.1:9097").rstrip("/"),
        secret=str(rotation_cfg.get("secret") or ""),
        group_name=str(rotation_cfg.get("group_name") or "SDK DNS"),
        interval_minutes=float(rotation_cfg.get("interval_minutes") or 30),
        switch_at_start=bool(rotation_cfg.get("switch_at_start", True)),
        include_keywords=[str(item) for item in rotation_cfg.get("include_keywords") or clash_verge_rotator.DEFAULT_INCLUDE_KEYWORDS],
        exclude_keywords=[str(item) for item in rotation_cfg.get("exclude_keywords") or clash_verge_rotator.DEFAULT_EXCLUDE_KEYWORDS],
    )
    stop = threading.Event()
    if rotator_cfg.switch_at_start:
        try:
            clash_verge_rotator.switch_once(rotator_cfg, logger)
        except Exception as exc:
            logger.warning("Clash Verge switch at batch start failed: %s", exc)
        rotator_cfg.switch_at_start = False
    thread = threading.Thread(
        target=clash_verge_rotator.run_loop,
        args=(rotator_cfg, logger, stop),
        name="clash-verge-rotator",
        daemon=True,
    )
    logger.info(
        "Starting Clash Verge rotation group=%s interval=%smin controller=%s",
        rotator_cfg.group_name,
        rotator_cfg.interval_minutes,
        rotator_cfg.controller,
    )
    thread.start()
    return thread, stop


class WorkerLogger:
    def __init__(self, base_logger: logging.Logger, file_path: Path):
        self.base_logger = base_logger
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, msg: str, *args) -> None:
        text = msg % args if args else msg
        line = f"{datetime.now().strftime('%H:%M:%S')} [{level}] {text}"
        with print_lock:
            getattr(self.base_logger, level.lower())(text)
        with self.file_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def info(self, msg: str, *args) -> None:
        self._write("INFO", msg, *args)

    def warning(self, msg: str, *args) -> None:
        self._write("WARNING", msg, *args)

    def error(self, msg: str, *args) -> None:
        self._write("ERROR", msg, *args)

    def debug(self, msg: str, *args) -> None:
        if self.base_logger.isEnabledFor(logging.DEBUG):
            self._write("DEBUG", msg, *args)


def setup_logging(verbose: bool) -> logging.Logger:
    BATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gopay-batch")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    file_handler = logging.FileHandler(BATCH_LOG_DIR / "batch.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


def stop_requested(stop_file: Path) -> bool:
    return stop_event.is_set() or bool(stop_file and stop_file.exists())


def clear_stale_stop_file(args: argparse.Namespace, logger: logging.Logger) -> None:
    stop_file = Path(args.stop_file)
    if args.honor_existing_stop_file or not stop_file.exists():
        return
    try:
        stop_file.unlink()
        logger.info("Removed stale stop file at startup: %s", stop_file)
    except Exception as exc:
        logger.warning("Failed to remove stale stop file %s: %s", stop_file, exc)


def _register_proc(proc: subprocess.Popen) -> None:
    with active_proc_lock:
        active_procs[id(proc)] = proc


def _unregister_proc(proc: subprocess.Popen) -> None:
    with active_proc_lock:
        active_procs.pop(id(proc), None)


def terminate_active_register_processes(logger: logging.Logger) -> None:
    with active_proc_lock:
        procs = list(active_procs.values())
    for proc in procs:
        if proc.poll() is None:
            logger.warning("Terminating active register subprocess pid=%s", proc.pid)
            try:
                proc.terminate()
            except Exception as exc:
                logger.warning("Failed to terminate register subprocess pid=%s: %s", proc.pid, exc)


def cleanup_instance(ld: Path, index: str, logger: WorkerLogger, args: argparse.Namespace) -> None:
    if getattr(args, "emulator", "ldplayer") != "ldplayer":
        logger.info("Skipping emulator cleanup for %s; reuse/stop it manually if needed", args.emulator)
        return
    if not index:
        return
    if is_protected_index(args, index):
        logger.error("Refusing to stop/remove protected LDPlayer index=%s", index)
        return
    try:
        logger.info("Stopping LDPlayer index=%s", index)
        prep.run_cmd([str(ld), "quit", "--index", str(index)], logger, timeout=60)
    except Exception as exc:
        logger.warning("LDPlayer quit failed index=%s: %s", index, exc)
    time.sleep(2)
    try:
        logger.info("Removing LDPlayer index=%s", index)
        prep.run_cmd([str(ld), "remove", "--index", str(index)], logger, timeout=120)
    except Exception as exc:
        logger.warning("LDPlayer remove failed index=%s: %s", index, exc)


def recover_prepare_index(ld: Path, prep_args: argparse.Namespace | None, logger: WorkerLogger) -> str:
    if ld is None:
        return ""
    if prep_args is None:
        return ""
    name = str(getattr(prep_args, "name", "") or "").strip()
    if not name:
        return ""
    try:
        index = prep.find_instance(ld, name, logger)
    except Exception as exc:
        logger.warning("Failed to recover LDPlayer index for name=%s after prepare failure: %s", name, exc)
        return ""
    if index:
        logger.warning("Recovered LDPlayer index=%s for failed prepare name=%s; cleanup can continue", index, name)
    return str(index or "")


def _read_keep_key(timeout_seconds: float) -> bool:
    deadline = time.time() + max(0.0, timeout_seconds)
    if os.name == "nt":
        try:
            import msvcrt
        except Exception:
            msvcrt = None
        while time.time() < deadline:
            if msvcrt and msvcrt.kbhit():
                ch = msvcrt.getwch()
                if str(ch).lower() == "k":
                    return True
            time.sleep(0.1)
        return False

    try:
        import select
    except Exception:
        time.sleep(max(0.0, timeout_seconds))
        return False
    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        readable, _, _ = select.select([sys.stdin], [], [], min(0.2, remaining))
        if readable:
            text = sys.stdin.readline().strip().lower()
            return text in {"k", "keep", "y", "yes"}
    return False


def confirm_keep_instance(args: argparse.Namespace, index: str, success: bool, logger: WorkerLogger) -> bool:
    if not args.confirm_keep_window:
        return False
    timeout = max(1.0, float(args.confirm_keep_timeout))
    with prompt_lock:
        logger.warning(
            "LDPlayer index=%s finished success=%s. Press K within %.0fs to keep this emulator for debugging.",
            index,
            success,
            timeout,
        )
        keep = _read_keep_key(timeout)
        if keep:
            logger.warning("Keeping LDPlayer index=%s because user pressed K", index)
            return True
        logger.info("No keep confirmation for LDPlayer index=%s; continuing cleanup", index)
        return False


def build_prepare_args(args: argparse.Namespace, worker_id: int, run_no: int) -> argparse.Namespace:
    suffix = f"w{worker_id:02d}-r{run_no:05d}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{random.randint(100000, 999999)}"
    return argparse.Namespace(
        emulator=args.emulator,
        ld_dir=args.ld_dir,
        bs_dir=args.bs_dir,
        bs_image=args.bs_image,
        bs_clone_from=args.bs_clone_from,
        adb_path=args.adb_path,
        device=(args.devices[(worker_id - 1) % len(args.devices)] if args.devices else ""),
        connect_ports=args.connect_ports,
        no_launch=args.no_launch_emulator,
        name=(f"{args.instance_prefix}-{suffix}" if args.emulator == "ldplayer" else ""),
        index="",
        create=True,
        unique_name=True,
        width=args.width,
        height=args.height,
        dpi=args.dpi,
        mt_apk=args.mt_apk,
        gopay_apks=args.gopay_apks,
        boot_timeout=args.boot_timeout,
        open_mt=args.open_mt,
        print_device=False,
        verbose=args.verbose,
    )


def build_prepare_command(prep_args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(ROOT / "gopay_prepare_emulator.py"),
        "--emulator",
        str(prep_args.emulator),
        "--ld-dir",
        str(prep_args.ld_dir),
        "--bs-dir",
        str(prep_args.bs_dir),
        "--bs-image",
        str(prep_args.bs_image),
        "--name",
        str(prep_args.name),
        "--width",
        str(prep_args.width),
        "--height",
        str(prep_args.height),
        "--dpi",
        str(prep_args.dpi),
        "--mt-apk",
        str(prep_args.mt_apk),
        "--gopay-apks",
        str(prep_args.gopay_apks),
        "--boot-timeout",
        str(prep_args.boot_timeout),
        "--print-device",
    ]
    if prep_args.adb_path:
        cmd += ["--adb-path", str(prep_args.adb_path)]
    if prep_args.bs_clone_from:
        cmd += ["--bs-clone-from", str(prep_args.bs_clone_from)]
    if prep_args.device:
        cmd += ["--device", str(prep_args.device)]
    for port in getattr(prep_args, "connect_ports", []) or []:
        cmd += ["--connect-port", str(port)]
    if getattr(prep_args, "no_launch", False):
        cmd.append("--no-launch")
    if prep_args.index:
        cmd += ["--index", str(prep_args.index)]
    if prep_args.create:
        cmd.append("--create")
    if prep_args.unique_name:
        cmd.append("--unique-name")
    if prep_args.open_mt:
        cmd.append("--open-mt")
    if prep_args.verbose:
        cmd.append("--verbose")
    return cmd


def run_prepare(prep_args: argparse.Namespace, logger: WorkerLogger, timeout: int) -> dict[str, str]:
    cmd = build_prepare_command(prep_args)
    logger.info("Starting prepare subprocess: %s", " ".join(cmd))
    prepared: dict[str, str] = {}
    with logger.file_path.open("a", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        line_queue: queue.Queue[str] = queue.Queue()

        def _reader() -> None:
            assert proc.stdout is not None
            for stdout_line in proc.stdout:
                line_queue.put(stdout_line)

        reader = threading.Thread(target=_reader, name="prepare-log-reader", daemon=True)
        reader.start()

        def handle_line(line: str) -> None:
            f.write(line)
            f.flush()
            text = line.rstrip()
            with print_lock:
                print(f"[{logger.file_path.parent.name}] {text}")
            if text.startswith("PREPARED_INDEX="):
                prepared["index"] = text.split("=", 1)[1].strip()
            elif text.startswith("PREPARED_DEVICE="):
                prepared["device"] = text.split("=", 1)[1].strip()

        try:
            start = time.time()
            while proc.poll() is None:
                try:
                    handle_line(line_queue.get(timeout=0.5))
                except queue.Empty:
                    pass
                if timeout > 0 and time.time() - start > timeout:
                    logger.warning("Prepare subprocess timeout after %ss; terminating", timeout)
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        logger.warning("Prepare subprocess did not terminate; killing")
                        proc.kill()
                    raise RuntimeError(f"prepare subprocess timeout after {timeout}s")
            code = proc.wait()
            while True:
                try:
                    handle_line(line_queue.get_nowait())
                except queue.Empty:
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
        if code != 0:
            raise RuntimeError(f"prepare subprocess exited code={code}")
    if not prepared.get("index") or not prepared.get("device"):
        raise RuntimeError(f"prepare subprocess did not report index/device: {prepared}")
    return prepared


def build_register_command(args: argparse.Namespace, device: str, run_dir: Path) -> list[str]:
    cmd = [
        sys.executable,
        str(ROOT / "gopay_register_adb.py"),
        "--config",
        args.config,
        "--emulator",
        args.emulator,
        "--skip-prepare-emulator",
        "--device",
        device,
        "--bs-dir",
        str(args.bs_dir),
        "--step-dir",
        str(run_dir / "steps"),
    ]
    if args.adb_path:
        cmd += ["--adb-path", str(args.adb_path)]
    for port in getattr(args, "connect_ports", []) or []:
        cmd += ["--connect-port", str(port)]
    if args.register_verbose:
        cmd.append("--verbose")
    for extra in args.register_arg:
        cmd.extend(extra.split())
    return cmd


def run_register(cmd: list[str], log_path: Path, logger: WorkerLogger, timeout: int) -> int:
    if stop_event.is_set():
        logger.warning("Stop requested before register subprocess start")
        return 130
    logger.info("Starting register subprocess: %s", " ".join(cmd))
    with log_path.open("a", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _register_proc(proc)
        try:
            assert proc.stdout is not None
            start = time.time()
            for line in proc.stdout:
                f.write(line)
                f.flush()
                with print_lock:
                    print(f"[{logger.file_path.parent.name}] {line.rstrip()}")
                if stop_event.is_set():
                    logger.warning("Stop requested; terminating register subprocess")
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return 130
                if timeout > 0 and time.time() - start > timeout:
                    logger.warning("Register subprocess timeout after %ss; terminating", timeout)
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return 124
            return proc.wait()
        finally:
            _unregister_proc(proc)


def worker_loop(worker_id: int, args: argparse.Namespace, base_logger: logging.Logger) -> None:
    ld = prep.ldconsole(Path(args.ld_dir)) if args.emulator == "ldplayer" else None
    stop_file = Path(args.stop_file)
    run_no = 0
    while True:
        if stop_requested(stop_file):
            base_logger.info("worker-%02d stop file detected before next run", worker_id)
            return
        if args.runs_per_worker > 0 and run_no >= args.runs_per_worker:
            base_logger.info("worker-%02d finished requested runs=%s", worker_id, args.runs_per_worker)
            return
        run_no += 1
        run_id = f"w{worker_id:02d}_r{run_no:05d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_dir = BATCH_LOG_DIR / f"worker-{worker_id:02d}" / run_id
        logger = WorkerLogger(base_logger, run_dir / "worker.log")
        index = ""
        device = ""
        success = False
        prep_args = None
        try:
            logger.info("Run started worker=%02d run=%05d", worker_id, run_no)
            prep_args = build_prepare_args(args, worker_id, run_no)
            if args.parallel_prepare:
                result = run_prepare(prep_args, logger, args.prepare_timeout)
            else:
                with prepare_lock:
                    result = run_prepare(prep_args, logger, args.prepare_timeout)
            index = str(result.get("index") or "")
            device = str(result.get("device") or "")
            logger.info("Prepared index=%s device=%s", index, device)
            if is_protected_index(args, index):
                logger.error("Prepared protected LDPlayer index=%s; refusing registration", index)
                return
            if stop_requested(stop_file):
                logger.warning("Stop requested after prepare; skipping registration")
                return
            cmd = build_register_command(args, device, run_dir)
            code = run_register(cmd, run_dir / "register.log", logger, args.register_timeout)
            success = code == 0
            logger.info("Register subprocess exited code=%s success=%s", code, success)
        except Exception as exc:
            logger.error("Run failed: %s", exc)
            if not index:
                index = recover_prepare_index(ld, prep_args, logger)
        finally:
            if index and (success or not args.keep_failed):
                if confirm_keep_instance(args, index, success, logger):
                    logger.warning("Skipping cleanup for LDPlayer index=%s", index)
                else:
                    cleanup_instance(ld, index, logger, args)
            elif index:
                logger.warning("Keeping failed LDPlayer index=%s for debugging", index)
            if args.delay_between_runs > 0:
                if stop_requested(stop_file):
                    logger.info("Stop requested; not sleeping before next run")
                    return
                logger.info("Sleeping %.1fs before next run", args.delay_between_runs)
                stop_event.wait(args.delay_between_runs)


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run continuous parallel GoPay registrations.")
    parser.add_argument("--workers", type=int, default=2, help="Number of concurrent workers")
    parser.add_argument("--loop", action="store_true", help="Run forever until stop file exists or interrupted")
    parser.add_argument("--runs-per-worker", type=int, default=1, help="Runs per worker; 0 means infinite")
    parser.add_argument("--delay-between-runs", type=float, default=10.0)
    parser.add_argument("--stagger-start", type=float, default=5.0, help="Delay between starting workers")
    parser.add_argument("--stop-file", default=str(BATCH_LOG_DIR / "stop_batch.txt"))
    parser.add_argument("--honor-existing-stop-file", action="store_true", help="Do not clear an old stop file at startup")
    parser.add_argument("--keep-failed", action="store_true", help="Do not delete emulator for failed runs")
    parser.add_argument("--confirm-keep-window", action="store_true", help="After each run, wait briefly for K to keep the emulator before cleanup")
    parser.add_argument("--confirm-keep-timeout", type=float, default=5.0, help="Seconds to wait for --confirm-keep-window")
    parser.add_argument("--parallel-prepare", action="store_true", help="Allow LDPlayer prepare steps to run concurrently")
    parser.add_argument("--prepare-timeout", type=int, default=420, help="Per-emulator prepare subprocess timeout seconds; 0 disables")
    parser.add_argument("--register-timeout", type=int, default=1800, help="Per-register subprocess timeout seconds; 0 disables")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--emulator", default="", choices=("", "ldplayer", "bluestacks", "adb"), help="Emulator adapter")
    parser.add_argument("--ld-dir", default=str(DEFAULT_LD_DIR))
    parser.add_argument("--bs-dir", default=str(DEFAULT_BS_DIR))
    parser.add_argument("--bs-image", default=emulator_support.DEFAULT_BLUESTACKS_IMAGE)
    parser.add_argument("--bs-clone-from", default="", help="Clone this BlueStacks instance instead of creating a fresh one")
    parser.add_argument("--adb-path", default="", help="ADB path; BlueStacks defaults to HD-Adb.exe")
    parser.add_argument("--device", action="append", default=[], help="ADB device/host:port to assign to workers")
    parser.add_argument("--connect-port", action="append", type=int, default=[], help="ADB localhost port to try")
    parser.add_argument("--no-launch-emulator", action="store_true", help="Do not launch BlueStacks during preparation")
    parser.add_argument("--mt-apk", default=str(DEFAULT_MT_APK))
    parser.add_argument("--gopay-apks", default=str(DEFAULT_GOPAY_APKS))
    parser.add_argument("--instance-prefix", default="gopay-auto")
    parser.add_argument("--protected-emulator-index", action="append", default=[], help="LDPlayer index that must never be stopped/removed")
    parser.add_argument("--protected-emulator-name", action="append", default=[], help="LDPlayer name reserved outside batch registrations")
    parser.add_argument("--protected-device", action="append", default=[], help="ADB serial reserved outside batch registrations")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--dpi", type=int, default=480)
    parser.add_argument("--boot-timeout", type=int, default=180)
    parser.add_argument("--open-mt", action="store_true")
    parser.add_argument("--register-verbose", action="store_true")
    parser.add_argument("--register-arg", action="append", default=[], help="Extra quoted argument string passed to gopay_register_adb.py")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.loop and args.runs_per_worker == 1:
        args.runs_per_worker = 0
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    cfg = load_config(Path(args.config))
    args.loaded_config = cfg
    prepare_cfg = ((cfg.get("gopay") or {}).get("prepare_emulator") or {})
    args.emulator = emulator_support.normalize_emulator(
        args.emulator or str(prepare_cfg.get("emulator") or prepare_cfg.get("type") or "ldplayer")
    )
    args.bs_dir = str(args.bs_dir or prepare_cfg.get("bs_dir") or prepare_cfg.get("bluestacks_dir") or DEFAULT_BS_DIR)
    args.bs_image = str(args.bs_image or prepare_cfg.get("bs_image") or prepare_cfg.get("image") or emulator_support.DEFAULT_BLUESTACKS_IMAGE)
    args.bs_clone_from = str(args.bs_clone_from or prepare_cfg.get("bs_clone_from") or prepare_cfg.get("clone_from") or "")
    args.adb_path = str(args.adb_path or prepare_cfg.get("adb_path") or "")
    args.devices = config_string_list(args.device) or config_string_list(
        prepare_cfg.get("devices") or prepare_cfg.get("device") or prepare_cfg.get("adb_device")
    )
    args.connect_ports = args.connect_port or emulator_support.parse_ports(
        prepare_cfg.get("connect_ports") or prepare_cfg.get("connect_port")
    )
    args.no_launch_emulator = bool(
        args.no_launch_emulator or prepare_cfg.get("no_launch") or prepare_cfg.get("no_launch_emulator")
    )
    if args.emulator != "ldplayer" and args.workers > 1 and len(args.devices) < args.workers:
        parser.error("non-LDPlayer batch mode needs one --device per worker, or use --workers 1")
    protected_cfg = protected_emulators_from_config(cfg)
    args.protected_indexes = list(dict.fromkeys(
        config_string_list(args.protected_emulator_index) + protected_cfg["indexes"]
    ))
    args.protected_names = list(dict.fromkeys(
        config_string_list(args.protected_emulator_name) + protected_cfg["names"]
    ))
    args.protected_devices = list(dict.fromkeys(
        config_string_list(args.protected_device) + protected_cfg["devices"]
    ))
    return args


def main() -> int:
    args = build_args()
    logger = setup_logging(args.verbose)
    clear_stale_stop_file(args, logger)
    logger.info(
        "Batch starting workers=%s runs_per_worker=%s delay=%ss stop_file=%s",
        args.workers,
        "infinite" if args.runs_per_worker == 0 else args.runs_per_worker,
        args.delay_between_runs,
        args.stop_file,
    )
    threads: list[threading.Thread] = []
    rotation_thread, rotation_stop = start_clash_rotation_thread(args.loaded_config, logger)
    try:
        for worker_id in range(1, args.workers + 1):
            t = threading.Thread(target=worker_loop, args=(worker_id, args, logger), name=f"worker-{worker_id:02d}")
            t.start()
            threads.append(t)
            if worker_id < args.workers and args.stagger_start > 0:
                time.sleep(args.stagger_start)
        for t in threads:
            t.join()
        if rotation_stop is not None:
            rotation_stop.set()
        if rotation_thread is not None:
            rotation_thread.join(timeout=5)
        logger.info("Batch finished")
        return 0
    except KeyboardInterrupt:
        logger.warning("Interrupted; stopping workers and active register subprocesses")
        stop_event.set()
        if rotation_stop is not None:
            rotation_stop.set()
        terminate_active_register_processes(logger)
        for t in threads:
            t.join(timeout=5)
        if rotation_thread is not None:
            rotation_thread.join(timeout=5)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
