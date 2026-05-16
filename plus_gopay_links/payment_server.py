#!/usr/bin/env python3
"""Segmented gRPC wrapper for the GoPay payment flow."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import threading
import time
import uuid
from concurrent import futures
from dataclasses import dataclass
from typing import Any

import grpc

import payment_pb2
import payment_pb2_grpc
from gopay import (
    DEFAULT_STRIPE_PK,
    GoPayCharger,
    GoPayError,
    OTPCancelled,
    _build_chatgpt_session,
    build_fingerprint_profile,
    _fingerprint_log_summary,
    _mask_proxy_url,
    _load_cfg,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _billing_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    billing = cfg.get("billing") or {}
    if billing:
        return dict(billing)
    cards = cfg.get("cards") or []
    if cards and isinstance(cards[0], dict):
        card0 = cards[0]
        out = dict(card0.get("address") or {})
        for key in ("name", "email"):
            if card0.get(key):
                out[key] = card0[key]
        return out
    return {}


def _normalize_listen(value: str) -> str:
    value = (value or ":50051").strip()
    if value.startswith(":"):
        return "[::]" + value
    return value


def _close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _redact_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return "<empty>"
    return f"***{digits[-4:]} len={len(digits)}"


@dataclass
class PendingFlow:
    charger: GoPayCharger
    state: dict[str, Any]
    expires_at: float

    def close(self) -> None:
        self.charger.close()


class FlowStore:
    def __init__(self, ttl_seconds: int):
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._lock = threading.Lock()
        self._flows: dict[str, PendingFlow] = {}
        self._closed = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="payment-flow-reaper", daemon=True)
        self._reaper.start()

    def put(self, charger: GoPayCharger, state: dict[str, Any]) -> tuple[str, int]:
        flow_id = uuid.uuid4().hex
        expires_at = time.time() + self._ttl_seconds
        with self._lock:
            self._flows[flow_id] = PendingFlow(charger=charger, state=state, expires_at=expires_at)
        return flow_id, int(expires_at)

    def pop(self, flow_id: str) -> PendingFlow | None:
        with self._lock:
            return self._flows.pop(flow_id, None)

    def get(self, flow_id: str) -> PendingFlow | None:
        with self._lock:
            return self._flows.get(flow_id)

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            flows = list(self._flows.values())
            self._flows.clear()
        for flow in flows:
            flow.close()

    def _reap_loop(self) -> None:
        while not self._closed.wait(30):
            now = time.time()
            expired: list[PendingFlow] = []
            with self._lock:
                for flow_id, flow in list(self._flows.items()):
                    if flow.expires_at <= now:
                        expired.append(self._flows.pop(flow_id))
            for flow in expired:
                logger.info("[payment] closing expired flow")
                flow.close()


class PaymentService(payment_pb2_grpc.PaymentServiceServicer):
    def __init__(self, cfg: dict[str, Any], flow_ttl_seconds: int):
        self._cfg = cfg
        self._flows = FlowStore(flow_ttl_seconds)

    def close(self) -> None:
        self._flows.close()

    def StartGoPay(self, request, context):
        if not request.session_token:
            return payment_pb2.StartGoPayResponse(
                success=False,
                error_message="session_token is required",
            )

        charger = None
        cs_session = None
        try:
            logger.info(
                "[payment] StartGoPay request country=%s phone=%s pin=%s proxy=%s token_len=%s",
                request.country_code or "<empty>",
                _redact_phone(request.phone_number),
                "yes" if request.pin else "no",
                _mask_proxy_url(request.proxy_url or ""),
                len(request.session_token or ""),
            )
            cfg = copy.deepcopy(self._cfg)
            fresh_checkout = cfg.get("fresh_checkout") or {}
            auth_cfg = dict(fresh_checkout.get("auth") or {})

            # 兼容老格式：session_token 字段可以是纯 token，也可以是
            # JSON auth payload，里面带 session_token/access_token/device_id。
            _token = request.session_token.strip()
            auth_payload = None
            if _token.startswith("{"):
                try:
                    parsed = json.loads(_token)
                    if isinstance(parsed, dict):
                        auth_payload = parsed
                except Exception:
                    auth_payload = None

            if auth_payload is not None:
                for key in (
                    "session_token",
                    "access_token",
                    "device_id",
                    "cookie_header",
                    "refresh_token",
                ):
                    value = str(auth_payload.get(key) or "").strip()
                    if value:
                        auth_cfg[key] = value
                auth_cfg["mode"] = "access_token"
                auth_cfg["prefer_session_refresh"] = bool(
                    auth_payload.get("prefer_session_refresh", True)
                )
                logger.info(
                    "[payment] auth payload account_id=%s email=%s session=%s access=%s device=%s",
                    auth_payload.get("account_id", ""),
                    auth_payload.get("email", ""),
                    "yes" if auth_cfg.get("session_token") else "no",
                    "yes" if auth_cfg.get("access_token") else "no",
                    "yes" if auth_cfg.get("device_id") else "no",
                )
            elif _token.startswith("eyJ") and _token.count(".") == 2:
                # JWT access_token：直接用作 Bearer，不需要 session_token cookie
                auth_cfg["access_token"] = _token
                auth_cfg.pop("session_token", None)
                auth_cfg.pop("cookie_header", None)
            else:
                # session_token cookie
                auth_cfg["session_token"] = _token
                auth_cfg.pop("cookie_header", None)
                auth_cfg["prefer_session_refresh"] = auth_cfg.get("prefer_session_refresh", True)

            fingerprint_profile = build_fingerprint_profile(cfg.get("fingerprint") or {})
            logger.info("[payment] fingerprint profile %s", _fingerprint_log_summary(fingerprint_profile))
            cs_session = _build_chatgpt_session(auth_cfg, fingerprint_profile=fingerprint_profile)

            gopay_cfg = dict(cfg.get("gopay") or {})
            if request.country_code:
                gopay_cfg["country_code"] = request.country_code
            if request.phone_number:
                gopay_cfg["phone_number"] = request.phone_number
            if request.pin:
                gopay_cfg["pin"] = request.pin

            stripe_pk = (
                (cfg.get("stripe") or {}).get("publishable_key")
                or auth_cfg.get("stripe_pk")
                or DEFAULT_STRIPE_PK
            )
            runtime_cfg = dict(cfg.get("runtime") or {})
            proxy = request.proxy_url.strip() or (cfg.get("proxy") or "").strip() or None
            payment_proxy = (
                (cfg.get("payment_proxy") or "")
                or ((cfg.get("gopay") or {}).get("payment_proxy") or "")
            ).strip() or None
            logger.info("[payment] proxy %s", _mask_proxy_url(proxy or ""))
            logger.info("[payment] payment_proxy %s", _mask_proxy_url(payment_proxy or ""))
            charger = GoPayCharger(
                cs_session,
                gopay_cfg,
                otp_provider=lambda: (_ for _ in ()).throw(OTPCancelled("external OTP required")),
                proxy=proxy,
                payment_proxy=payment_proxy,
                runtime_cfg=runtime_cfg,
                fingerprint_profile=fingerprint_profile,
                log=logger.info,
            )

            logger.info("[payment] StartGoPay start")
            state = charger.start_until_otp(stripe_pk=stripe_pk, billing=_billing_from_config(cfg))
            flow_id, expires_at = self._flows.put(charger, state)
            charger = None
            cs_session = None
            logger.info("[payment] StartGoPay waiting_otp flow=%s", flow_id[:8])
            return payment_pb2.StartGoPayResponse(
                success=True,
                flow_id=flow_id,
                snap_token=str(state.get("snap_token") or ""),
                issued_after_unix=int(state.get("issued_after_unix") or 0),
                expires_at_unix=expires_at,
            )
        except GoPayError as exc:
            logger.error("[payment] StartGoPay failed: %s", exc)
            return payment_pb2.StartGoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] StartGoPay crashed")
            return payment_pb2.StartGoPayResponse(success=False, error_message=str(exc)[:500])
        finally:
            if charger is not None:
                charger.close()
            elif cs_session is not None:
                _close_session(cs_session)

    def CompleteGoPay(self, request, context):
        if not request.flow_id:
            return payment_pb2.GoPayResponse(success=False, error_message="flow_id is required")
        if not request.otp:
            return payment_pb2.GoPayResponse(success=False, error_message="otp is required")

        flow = self._flows.pop(request.flow_id)
        if flow is None:
            return payment_pb2.GoPayResponse(success=False, error_message="payment flow not found or expired")

        try:
            logger.info("[payment] CompleteGoPay flow=%s", request.flow_id[:8])
            result = flow.charger.complete_after_otp(flow.state, request.otp)
            state = str(result.get("state") or "")
            success = state == "succeeded"
            return payment_pb2.GoPayResponse(
                success=success,
                error_message="" if success else f"payment state={state or 'unknown'}",
                charge_ref=str(result.get("charge_ref") or ""),
                snap_token=str(result.get("snap_token") or ""),
            )
        except GoPayError as exc:
            logger.error("[payment] CompleteGoPay failed: %s", exc)
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] CompleteGoPay crashed")
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        finally:
            flow.close()

    def ResendGoPayOtp(self, request, context):
        if not request.flow_id:
            return payment_pb2.GoPayResponse(success=False, error_message="flow_id is required")

        flow = self._flows.get(request.flow_id)
        if flow is None:
            return payment_pb2.GoPayResponse(success=False, error_message="payment flow not found or expired")

        try:
            logger.info("[payment] ResendGoPayOtp flow=%s", request.flow_id[:8])
            flow.charger.resend_linking_otp(flow.state)
            return payment_pb2.GoPayResponse(
                success=True,
                snap_token=str(flow.state.get("snap_token") or ""),
            )
        except GoPayError as exc:
            logger.error("[payment] ResendGoPayOtp failed: %s", exc)
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] ResendGoPayOtp crashed")
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])

    def CancelGoPay(self, request, context):
        if not request.flow_id:
            return payment_pb2.CancelGoPayResponse(success=False, error_message="flow_id is required")
        flow = self._flows.pop(request.flow_id)
        if flow is not None:
            flow.close()
        logger.info("[payment] CancelGoPay flow=%s found=%s", request.flow_id[:8], flow is not None)
        return payment_pb2.CancelGoPayResponse(success=True)


def serve(config_path: str, listen: str, flow_ttl_seconds: int):
    cfg = _load_cfg(config_path)
    service = PaymentService(cfg, flow_ttl_seconds=flow_ttl_seconds)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )
    payment_pb2_grpc.add_PaymentServiceServicer_to_server(service, server)
    resend_handler = grpc.method_handlers_generic_handler(
        "payment.PaymentService",
        {
            "ResendGoPayOtp": grpc.unary_unary_rpc_method_handler(
                service.ResendGoPayOtp,
                request_deserializer=payment_pb2.CancelGoPayRequest.FromString,
                response_serializer=payment_pb2.GoPayResponse.SerializeToString,
            )
        },
    )
    server.add_generic_rpc_handlers((resend_handler,))
    listen_addr = _normalize_listen(listen)
    server.add_insecure_port(listen_addr)
    server.start()
    logger.info("[payment] gRPC listening on %s flow_ttl=%ss", listen_addr, flow_ttl_seconds)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("[payment] shutting down")
        server.stop(grace=5)
    finally:
        service.close()


def main():
    parser = argparse.ArgumentParser(description="GoPay payment gRPC service")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--listen", default=":50051")
    parser.add_argument("--flow-ttl", type=int, default=600)
    args = parser.parse_args()

    serve(
        config_path=args.config,
        listen=args.listen,
        flow_ttl_seconds=args.flow_ttl,
    )


if __name__ == "__main__":
    main()
