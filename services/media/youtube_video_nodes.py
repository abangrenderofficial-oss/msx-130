"""Authenticated multi-node downloader router for opt-in Musix Video-First V2.

Nodes are independent *deployments* with their own YouTube access context.
Rotating among profiles on the same blocked IP is not a replacement for
independent nodes. Remote services exchange MP3 bytes, never cookie files.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from services.media.youtube_video_first import VideoFirstError, _probe, _streams

_MAX_AUDIO_BYTES = 49 * 1024 * 1024
_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


@dataclass(frozen=True)
class WorkerNode:
    name: str
    base_url: str
    token: str


@dataclass(frozen=True)
class WorkerHealth:
    node: WorkerNode
    cookie_capable: bool
    active: int
    capacity: int


class WorkerRouterError(VideoFirstError):
    pass


def configured_nodes() -> list[WorkerNode]:
    """No credentials in JSON; reference secret environment variables only."""
    raw = (os.getenv("MUSIX_VIDEO_FIRST_NODES_JSON") or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise WorkerRouterError("Invalid worker nodes JSON") from exc
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise WorkerRouterError("Worker nodes must be a non-empty list of at most 12 nodes")

    result: list[WorkerNode] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise WorkerRouterError("Invalid worker entry")
        name = item.get("name")
        url = item.get("url")
        token_env = item.get("token_env")
        if not all(isinstance(v, str) for v in (name, url, token_env)):
            raise WorkerRouterError("Worker name, URL and token_env must be strings")
        if not _NAME.fullmatch(name) or name in seen:
            raise WorkerRouterError("Worker names must be valid and unique")
        seen.add(name)
        if not _ENV_NAME.fullmatch(token_env):
            raise WorkerRouterError("Invalid worker token environment variable name")
        secret = os.getenv(token_env, "")
        if len(secret) < 16:
            raise WorkerRouterError("Worker authentication secret must have at least 16 characters")

        parts = urlsplit(url)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise WorkerRouterError("Worker URL must not contain credentials or query parameters")
        if parts.path not in {"", "/"} or not parts.hostname:
            raise WorkerRouterError("Worker URL must be a host base URL")
        local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise WorkerRouterError("Worker URL requires HTTPS except local loopback")
        try:
            _ = parts.port  # Accessing malformed ports raises ValueError.
        except ValueError as exc:
            raise WorkerRouterError("Invalid worker URL port") from exc
        result.append(WorkerNode(name=name, base_url=url.rstrip("/"), token=secret))
    return result


def _headers(node: WorkerNode) -> dict[str, str]:
    return {"X-Musix-Worker-Token": node.token}


async def _status(client: httpx.AsyncClient, node: WorkerNode) -> WorkerHealth | None:
    try:
        res = await client.get(
            f"{node.base_url}/v1/health", headers=_headers(node),
            timeout=8.0, follow_redirects=False,
        )
        if res.status_code != 200:
            return None
        data = res.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        capacity = int(data.get("capacity", 1))
        active = int(data.get("active", 0))
        if not 1 <= capacity <= 32 or not 0 <= active <= capacity:
            return None
        domains = data.get("cookie_domains") or []
        if not isinstance(domains, list):
            return None
        return WorkerHealth(
            node=node, cookie_capable=("youtube.com" in domains),
            active=active, capacity=capacity,
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None


def _classify_http(status_code: int, body: str = "") -> str:
    if status_code == 409 or "context_unavailable" in body:
        return "context_unavailable"
    if status_code == 429:
        return "rate_limit"
    if status_code == 503:
        return "busy"
    if status_code in {401, 403}:
        return "worker_auth"
    return "worker_error"


async def _attempt(
    client: httpx.AsyncClient,
    node: WorkerNode,
    url: str,
    out_file: Path,
    bitrate_kbps: int,
) -> None:
    size = 0
    try:
        async with client.stream(
            "POST",
            f"{node.base_url}/v1/audio",
            headers=_headers(node),
            json={"url": url, "bitrate_kbps": int(bitrate_kbps)},
            follow_redirects=False,
            timeout=httpx.Timeout(210, connect=10, read=200, write=10, pool=10),
        ) as response:
            if response.status_code != 200:
                # Never log any body potentially containing server secrets.
                raise WorkerRouterError(_classify_http(response.status_code))
            if response.headers.get("Content-Type", "").split(";")[0].strip() != "audio/mpeg":
                raise WorkerRouterError("invalid_worker_content_type")
            content_length = response.headers.get("Content-Length")
            if content_length and (not content_length.isdigit() or int(content_length) > _MAX_AUDIO_BYTES):
                raise WorkerRouterError("worker_output_too_large")
            with out_file.open("xb") as handle:
                async for block in response.aiter_bytes(chunk_size=256 * 1024):
                    size += len(block)
                    if size > _MAX_AUDIO_BYTES:
                        raise WorkerRouterError("worker_output_too_large")
                    handle.write(block)
        if size < 1024:
            raise WorkerRouterError("worker_output_empty")
        data = await asyncio.to_thread(_probe, out_file)
        audio = _streams(data, "audio")
        if not audio or audio[0].get("codec_name") != "mp3":
            raise WorkerRouterError("worker_output_not_mp3")
        try:
            duration = float(data.get("format", {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration < 0.2:
            raise WorkerRouterError("worker_output_invalid_duration")
    except VideoFirstError as exc:
        raise WorkerRouterError("worker_output_invalid_media") from exc
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise WorkerRouterError("worker_network_or_io_error") from exc


async def download_from_worker_nodes(
    url: str,
    out_template: str,
    bitrate_kbps: int,
    *,
    nodes: list[WorkerNode] | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Try suitable independent workers; never leak their tokens into errors."""
    candidates = configured_nodes() if nodes is None else nodes
    if not candidates:
        raise WorkerRouterError("no_worker_nodes_configured")
    if not 64 <= int(bitrate_kbps) <= 320:
        raise WorkerRouterError("invalid_bitrate")

    target = Path(out_template.replace(".%(ext)s", ".mp3"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise WorkerRouterError("worker_target_already_exists")

    owned = client is None
    if owned:
        client = httpx.AsyncClient(trust_env=False)
    assert client is not None
    try:
        health = await asyncio.gather(*(_status(client, node) for node in candidates))
        healthy = [h for h in health if h is not None]
        # Just like Desiders: prefer cookie-capable node by domain and then
        # prefer less-loaded healthy nodes. A cookie does not imply it is valid.
        healthy.sort(key=lambda h: (not h.cookie_capable, h.active / h.capacity, h.node.name))
        if not healthy:
            raise WorkerRouterError("no_healthy_worker_nodes")

        failed: list[str] = []
        for h in healthy:
            if h.active >= h.capacity:
                failed.append("busy")
                continue
            try:
                await _attempt(client, h.node, url, target, bitrate_kbps)
                return str(target)
            except WorkerRouterError as exc:
                target.unlink(missing_ok=True)
                failed.append(str(exc))
                # Credential mismatch is misconfiguration, not a YouTube
                # failover reason; do not silently hide it.
                if str(exc) == "worker_auth":
                    raise
        raise WorkerRouterError("all_worker_nodes_failed:" + ",".join(failed))
    finally:
        if owned:
            await client.aclose()
