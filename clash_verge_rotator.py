#!/usr/bin/env python3
"""Rotate a Clash Verge proxy group through the Mihomo external controller."""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_INCLUDE_KEYWORDS = [
    "香港",
    "日本",
    "美国",
    "台湾",
]
DEFAULT_EXCLUDE_KEYWORDS = [
    "DIRECT",
    "REJECT",
    "自动",
    "故障",
    "动态",
    "剩余",
    "重置",
    "套餐",
    "官网",
    "注意",
    "不推荐",
    "test",
]


class ClashVergeError(RuntimeError):
    pass


@dataclass
class RotationConfig:
    enabled: bool = False
    controller: str = "http://127.0.0.1:9097"
    secret: str = ""
    group_name: str = "SDK DNS"
    interval_minutes: float = 30.0
    switch_at_start: bool = True
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None

    @property
    def include(self) -> list[str]:
        return self.include_keywords or DEFAULT_INCLUDE_KEYWORDS

    @property
    def exclude(self) -> list[str]:
        return self.exclude_keywords or DEFAULT_EXCLUDE_KEYWORDS


def load_config(path: Path = DEFAULT_CONFIG) -> RotationConfig:
    if not path.exists():
        return RotationConfig()
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    raw = data.get("clash_verge_rotation") or {}
    if not isinstance(raw, dict):
        raw = {}
    return RotationConfig(
        enabled=bool(raw.get("enabled", False)),
        controller=str(raw.get("controller") or "http://127.0.0.1:9097").rstrip("/"),
        secret=str(raw.get("secret") or ""),
        group_name=str(raw.get("group_name") or "SDK DNS"),
        interval_minutes=float(raw.get("interval_minutes") or 30),
        switch_at_start=bool(raw.get("switch_at_start", True)),
        include_keywords=[str(item) for item in raw.get("include_keywords") or DEFAULT_INCLUDE_KEYWORDS],
        exclude_keywords=[str(item) for item in raw.get("exclude_keywords") or DEFAULT_EXCLUDE_KEYWORDS],
    )


class ClashVergeClient:
    def __init__(self, cfg: RotationConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("clash_verge_rotator")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.cfg.controller}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if self.cfg.secret:
            headers["Authorization"] = f"Bearer {self.cfg.secret}"
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ClashVergeError(f"{method} {url} failed HTTP {exc.code}: {detail[:300]}") from exc
        except Exception as exc:
            raise ClashVergeError(f"{method} {url} failed: {exc}") from exc
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClashVergeError(f"{method} {url} returned non-JSON: {text[:300]}") from exc

    def get_group(self) -> dict[str, Any]:
        group = urllib.parse.quote(self.cfg.group_name, safe="")
        data = self._request("GET", f"/proxies/{group}")
        if not isinstance(data, dict):
            raise ClashVergeError(f"proxy group response is not an object: {data!r}")
        return data

    def set_group_node(self, node_name: str) -> None:
        group = urllib.parse.quote(self.cfg.group_name, safe="")
        self._request("PUT", f"/proxies/{group}", {"name": node_name})


def keyword_hit(name: str, keywords: list[str]) -> bool:
    lower = name.lower()
    return any(str(keyword).lower() in lower for keyword in keywords if str(keyword))


def candidate_nodes(group: dict[str, Any], cfg: RotationConfig) -> list[str]:
    all_nodes = group.get("all") or []
    if not isinstance(all_nodes, list):
        raise ClashVergeError("proxy group response missing list field: all")
    candidates: list[str] = []
    for item in all_nodes:
        name = str(item)
        if not name:
            continue
        if cfg.include and not keyword_hit(name, cfg.include):
            continue
        if cfg.exclude and keyword_hit(name, cfg.exclude):
            continue
        candidates.append(name)
    return candidates


def switch_once(
    cfg: RotationConfig | None = None,
    logger: logging.Logger | None = None,
) -> str:
    cfg = cfg or load_config()
    log = logger or logging.getLogger("clash_verge_rotator")
    client = ClashVergeClient(cfg, log)
    group = client.get_group()
    current = str(group.get("now") or "")
    candidates = candidate_nodes(group, cfg)
    if not candidates:
        raise ClashVergeError(f"no candidate nodes after filtering group={cfg.group_name!r}")
    pool = [node for node in candidates if node != current] or candidates
    selected = random.choice(pool)
    log.info(
        "Switching Clash group=%s candidates=%s current=%s selected=%s",
        cfg.group_name,
        len(candidates),
        current or "<empty>",
        selected,
    )
    client.set_group_node(selected)
    return selected


def run_loop(
    cfg: RotationConfig | None = None,
    logger: logging.Logger | None = None,
    stop_event: Event | None = None,
) -> None:
    cfg = cfg or load_config()
    log = logger or logging.getLogger("clash_verge_rotator")
    stop_event = stop_event or Event()
    interval_seconds = max(1.0, float(cfg.interval_minutes) * 60.0)

    if cfg.switch_at_start:
        try:
            switch_once(cfg, log)
        except Exception as exc:
            log.warning("Clash switch at start failed: %s", exc)

    while not stop_event.wait(interval_seconds):
        try:
            switch_once(cfg, log)
        except Exception as exc:
            log.warning("Scheduled Clash switch failed: %s", exc)


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rotate Clash Verge proxy group nodes.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--once", action="store_true", help="Switch once and exit")
    parser.add_argument("--loop", action="store_true", help="Switch repeatedly")
    parser.add_argument("--controller", default="")
    parser.add_argument("--secret", default="")
    parser.add_argument("--group-name", default="")
    parser.add_argument("--interval-minutes", type=float, default=0)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = build_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(Path(args.config))
    if args.controller:
        cfg.controller = args.controller.rstrip("/")
    if args.secret:
        cfg.secret = args.secret
    if args.group_name:
        cfg.group_name = args.group_name
    if args.interval_minutes:
        cfg.interval_minutes = args.interval_minutes

    if not args.once and not args.loop:
        args.once = True
    try:
        if args.once:
            switch_once(cfg)
            return 0
        run_loop(cfg)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger("clash_verge_rotator").error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
