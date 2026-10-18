"""Read-only PayPing status adapter for private Music Bot admin monitoring.

The adapter is deliberately inert until both PayPing environment variables are
configured. It never consumes quota, applies a lock, triggers a reminder,
updates payments or sends a blocked reply to any Telegram user.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlparse

import httpx

from services.logger import logger as logging

logging = logging.bind(service="admin_monitor_access")


@dataclass(frozen=True)
class MonitorAccess:
    connected: bool = False
    allowed: bool | None = None
    supporter: bool | None = None
    tier: str | None = None
    mode: str | None = None
    reason: str | None = None
    payping_uses: int | None = None
    unavailable: bool = False


def _endpoint() -> tuple[str, str] | None:
    base = (os.getenv("PAYPING_API_BASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("PAYPING_BOT_API_KEY") or "").strip()
    if not base or not key:
        return None
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        logging.warning("PayPing monitor not activated: HTTPS base URL required")
        return None
    return base + "/api/bot-access/musix/check", key


def _int_nonnegative(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        v = int(value)
        return v if v >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


async def payping_monitor_status(
    user_id: int, *, first_name: str = "", username: str = ""
) -> MonitorAccess:
    endpoint = _endpoint()
    if endpoint is None:
        return MonitorAccess()

    url, key = endpoint
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(2.5, connect=1.2),
            follow_redirects=False,
        ) as client:
            response = await client.post(
                url,
                headers={"x-payping-bot-key": key},
                json={
                    "telegramUserId": str(int(user_id)),
                    "firstName": str(first_name or "")[:80],
                    "username": str(username or "")[:80],
                    "notifyBlocked": False,
                },
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("Unexpected PayPing status payload")
        if not isinstance(payload.get("allowed"), bool):
            raise ValueError("No access decision from PayPing")
        supporter = payload.get("supporter")
        if not isinstance(supporter, bool):
            supporter = None
        tier = (
            payload.get("supportTier")
            or payload.get("tierName")
            or payload.get("support_tier")
        )
        if tier is not None:
            tier = str(tier).strip()[:80] or None
        return MonitorAccess(
            connected=True,
            allowed=payload["allowed"],
            supporter=supporter,
            tier=tier,
            mode=str(payload.get("mode") or "")[:40] or None,
            reason=str(payload.get("reason") or "")[:70] or None,
            payping_uses=_int_nonnegative(payload.get("uses")),
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # Do not log response bodies or the API key.
        logging.warning(
            "PayPing monitor status unavailable: type=%s",
            type(exc).__name__,
        )
        return MonitorAccess(unavailable=True)


def monitor_access_lines(snapshot: MonitorAccess) -> list[str]:
    if snapshot.unavailable:
        return [
            "💜 Support Tier: Tidak dapat disahkan (PayPing offline)",
            "🔐 Status Akses: Tidak dapat disahkan (PayPing offline)",
        ]
    if not snapshot.connected:
        return [
            "💜 Support Tier: Belum disambung ke PayPing",
            "🔐 Status Akses: ✅ Access (audio berjaya dihantar; PayPing belum aktif)",
        ]

    if snapshot.supporter is True:
        tier = snapshot.tier or "Supporter (tier belum tersedia)"
    elif snapshot.supporter is False:
        tier = "Free"
    else:
        tier = snapshot.tier or "Belum dapat disahkan"
    access = "✅ Access" if snapshot.allowed else "🔒 Locked"
    output = [f"💜 Support Tier: {tier}", f"🔐 Status Akses: {access}"]
    if snapshot.payping_uses is not None:
        output.append(f"📈 Penggunaan PayPing: {snapshot.payping_uses} kali")
    return output
