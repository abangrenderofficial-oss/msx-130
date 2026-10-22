"""Opt-in MusiX ↔ PayPing verified affiliate bridge.

The separate Telegram download and payment flows remain untouched.
Only bot-authenticated /affiliate and /start referral events are sent to PayPing.
No API key means no commission claim (never fabricate successful tracking).
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx

from services.logger import logger as logging

log = logging.bind(service="payping_affiliate")
BOT_ID = "musix"


class AffiliateBridgeError(RuntimeError):
    pass


def _connection() -> tuple[str, str] | None:
    origin = (os.getenv("PAYPING_API_BASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("PAYPING_BOT_API_KEY") or "").strip()
    parsed = urlsplit(origin)
    if not key or not (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password):
        return None
    return origin, key


def configured() -> bool:
    return _connection() is not None


async def _send(action: str, payload: dict) -> dict:
    creds = _connection()
    if creds is None:
        raise AffiliateBridgeError("PayPing affiliate bridge is not configured")
    origin, key = creds
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=2.5)) as client:
        try:
            response = await client.post(
                origin + "/api/bot-affiliate/" + BOT_ID + "/" + action,
                json=payload,
                headers={"x-payping-bot-key": key},
            )
        except httpx.HTTPError as exc:
            raise AffiliateBridgeError("PayPing connection failed") from exc
    try:
        answer = response.json()
    except ValueError as exc:
        raise AffiliateBridgeError("PayPing returned invalid JSON") from exc
    if response.status_code != 200 or not isinstance(answer, dict) or not answer.get("ok"):
        msg = (answer.get("message") if isinstance(answer, dict) else "") or "Affiliate request rejected"
        raise AffiliateBridgeError(str(msg)[:140])
    return dict(answer.get("data") or {})


async def register(telegram_user_id: int, username: str | None) -> dict:
    return await _send(
        "register",
        {"telegramUserId": str(telegram_user_id), "telegramUsername": username or ""},
    )


async def record_join(telegram_user_id: int, referrer_user_id: int) -> dict:
    if telegram_user_id == referrer_user_id:
        raise AffiliateBridgeError("Self referral not allowed")
    # Match PayPing's registered bot referral code for /start ref_<id>
    return await _send(
        "join",
        {"telegramUserId": str(telegram_user_id), "referralCode": "a" + str(referrer_user_id)},
    )


async def link_account(telegram_user_id: int, username: str | None, code: str) -> dict:
    return await _send(
        "link",
        {"telegramUserId": str(telegram_user_id), "telegramUsername": username or "", "code": code},
    )
