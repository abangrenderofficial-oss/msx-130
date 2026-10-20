"""Optional, pull-only home audio worker queue. Never used unless explicitly enabled.

The bot owns PostgreSQL and the existing Telegram cache. A home PC only
accesses three tightly scoped HTTPS endpoints, not the bot's database.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from services.storage.models import HomeAudioJob, HomeAudioHeartbeat


def enabled() -> bool:
    return os.getenv("HOME_WORKER_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def configured() -> bool:
    return enabled() and len(os.getenv("HOME_WORKER_API_TOKEN", "")) >= 32


def timeout_seconds() -> int:
    return max(20, min(300, int(os.getenv("HOME_WORKER_TIMEOUT_SECONDS", "90"))))


def now() -> datetime:
    return datetime.now(timezone.utc)

def _aware(value: datetime) -> datetime:
    """SQLite returns naive timestamps despite timezone=True; PostgreSQL does not."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _valid_worker_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value))


async def record_heartbeat(db, worker_id: str, ready: bool = True) -> None:
    if not _valid_worker_id(worker_id):
        raise ValueError("Invalid worker ID")
    async with db.SessionLocal() as session:
        async with session.begin():
            item = await session.get(HomeAudioHeartbeat, worker_id)
            if item:
                item.last_seen_at = now()
                item.ready = bool(ready)
            else:
                session.add(HomeAudioHeartbeat(worker_id=worker_id, last_seen_at=now(), ready=bool(ready)))


async def is_online(db) -> bool:
    cutoff = now() - timedelta(seconds=180)
    async with db.SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(HomeAudioHeartbeat).where(
                HomeAudioHeartbeat.last_seen_at >= cutoff,
                HomeAudioHeartbeat.ready.is_(True)
            )
        )
        return bool(count)


async def enqueue(db, *, url: str, video_id: str, chat_id: int, message_id: int) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video ID")
    if not url.startswith(("https://www.youtube.com/", "https://youtube.com/", "https://youtu.be/", "https://m.youtube.com/")):
        raise ValueError("Invalid YouTube URL")
    job_id = uuid4().hex
    async with db.SessionLocal() as session:
        async with session.begin():
            session.add(HomeAudioJob(
                id=job_id,
                video_id=video_id,
                source_url=url,
                chat_id=int(chat_id),
                request_message_id=int(message_id),
                status="pending",
                created_at=now(),
                expires_at=now() + timedelta(seconds=timeout_seconds()),
            ))
    return job_id


async def claim(db, worker_id: str) -> dict | None:
    if not _valid_worker_id(worker_id):
        raise ValueError("Invalid worker ID")
    current = now()
    async with db.SessionLocal() as session:
        async with session.begin():
            # Fence out late workers and recover abandoned leases only while a
            # Telegram request is still inside its original deadline.
            await session.execute(
                update(HomeAudioJob)
                .where(HomeAudioJob.status == "processing",
                       HomeAudioJob.lease_expires_at < current,
                       HomeAudioJob.expires_at > current)
                .values(status="pending", claim_token=None, worker_id=None)
            )
            await session.execute(
                update(HomeAudioJob)
                .where(HomeAudioJob.status.in_(("pending", "processing")),
                       HomeAudioJob.expires_at <= current)
                .values(status="timeout", finished_at=current, failure_reason="deadline")
            )
            result = await session.execute(
                select(HomeAudioJob)
                .where(HomeAudioJob.status == "pending", HomeAudioJob.expires_at > current)
                .order_by(HomeAudioJob.created_at, HomeAudioJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            item = result.scalar_one_or_none()
            if item is None:
                return None
            token = secrets.token_urlsafe(24)
            item.status = "processing"
            item.worker_id = worker_id
            item.claim_token = token
            item.started_at = current
            item.lease_expires_at = min(_aware(item.expires_at), current + timedelta(seconds=timeout_seconds()))
            return {
                "job_id": item.id,
                "video_id": item.video_id,
                "url": item.source_url,
                "claim_token": token,
            }


async def complete(
    db, *, worker_id: str, job_id: str, claim_token: str,
    success: bool, file_id: str = "", title: str = "",
    performer: str = "", duration: float | None = None,
    failure_reason: str = "",
) -> dict | None:
    if not _valid_worker_id(worker_id) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return None
    if not claim_token or (success and (not file_id or len(file_id) > 512)):
        return None
    current = now()
    async with db.SessionLocal() as session:
        async with session.begin():
            item = await session.get(HomeAudioJob, job_id, with_for_update=True)
            if (item is None or item.status != "processing"
                    or item.worker_id != worker_id
                    or not secrets.compare_digest(item.claim_token or "", claim_token)
                    or _aware(item.expires_at) <= current):
                return None
            item.status = "done" if success else "failed"
            item.file_id = file_id[:512] if success else None
            item.title = title[:200] if success else None
            item.performer = performer[:200] if success else None
            item.duration_seconds = min(86400.0, max(0.0, float(duration or 0))) if success else None
            item.failure_reason = failure_reason[:80] if not success else None
            item.finished_at = current
            return {
                "url": item.source_url,
                "video_id": item.video_id,
                "file_id": item.file_id,
                "title": item.title or "Audio",
                "performer": item.performer or "YouTube",
                "duration": item.duration_seconds,
                "status": item.status,
            }


async def result(db, job_id: str) -> dict:
    async with db.SessionLocal() as session:
        item = await session.get(HomeAudioJob, job_id)
        if item is None:
            return {"status": "missing"}
        return {
            "status": item.status,
            "file_id": item.file_id,
            "title": item.title or "Audio",
            "performer": item.performer or "YouTube",
            "duration": item.duration_seconds,
            "failure_reason": item.failure_reason,
        }


async def expire(db, job_id: str, reason: str = "timeout") -> None:
    async with db.SessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(HomeAudioJob)
                .where(HomeAudioJob.id == job_id, HomeAudioJob.status.in_(("pending", "processing")))
                .values(status="timeout", finished_at=now(), failure_reason=reason[:80])
            )


async def wait_for_result(db, job_id: str) -> dict | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds()
    while asyncio.get_running_loop().time() < deadline:
        state = await result(db, job_id)
        if state["status"] == "done" and state["file_id"]:
            return state
        if state["status"] in {"failed", "timeout", "missing"}:
            return None
        await asyncio.sleep(2)
    await expire(db, job_id)
    return None


async def stats(db) -> dict:
    current = now()
    async with db.SessionLocal() as session:
        rows = {}
        for label, hours in (("24h", 24), ("7d", 168)):
            records = (await session.execute(
                select(HomeAudioJob.status, func.count())
                .where(HomeAudioJob.created_at >= current - timedelta(hours=hours))
                .group_by(HomeAudioJob.status)
            )).all()
            counts = {status: int(count) for status, count in records}
            terminal = sum(counts.get(k, 0) for k in ("done", "failed", "timeout"))
            rows[label] = {
                "success": counts.get("done", 0), "total": terminal,
                "rate": round(100 * counts.get("done", 0) / terminal, 1) if terminal else 0.0,
            }
        last = await session.scalar(select(func.max(HomeAudioHeartbeat.last_seen_at)))
        ready = await session.scalar(select(func.count()).select_from(HomeAudioHeartbeat).where(
            HomeAudioHeartbeat.last_seen_at >= current - timedelta(seconds=180),
            HomeAudioHeartbeat.ready.is_(True),
        ))
    rows["last_heartbeat"] = last.isoformat() if last else None
    rows["online"] = bool(ready)
    return rows
