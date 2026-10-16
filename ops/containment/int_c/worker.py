#!/usr/bin/python3
"""Credential-free, no-subprocess worker for INT-2026-10-16-C.

The worker is intentionally dedicated to cron job f10749396cea.  It accepts only
predeclared GET endpoints and emits bounded JSON metadata; response bodies never
appear on stdout.  OS-level containment is supplied by the packaged systemd unit.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

JOB_ID = "f10749396cea"
SCHEMA_VERSION = 1
MAX_BATCH_BYTES = 64 * 1024
MAX_REQUESTS = 32
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RUNTIME_SECONDS = 840.0


class ContractError(ValueError):
    """A closed-schema or containment contract was violated."""


@dataclass(frozen=True)
class Endpoint:
    endpoint_id: str
    host: str
    port: int
    path: str
    connect_ips: tuple[str, ...]
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


@dataclass(frozen=True)
class Policy:
    endpoints: dict[str, Endpoint]


Transport = Callable[[Endpoint, str, float, int], tuple[int, bytes]]


def _exact_object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _parse_networks(v4: object, v6: object) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not isinstance(v4, list) or not isinstance(v6, list) or not v4 or not v6:
        raise ContractError("each endpoint requires non-empty IPv4 and IPv6 CIDR allowlists")
    try:
        networks = tuple(ipaddress.ip_network(item, strict=True) for item in [*v4, *v6])
    except (TypeError, ValueError) as exc:
        raise ContractError("allowlist contains an invalid CIDR") from exc
    if not any(n.version == 4 for n in networks) or not any(n.version == 6 for n in networks):
        raise ContractError("allowlist must cover IPv4 and IPv6")
    return networks


def load_policy(path: Path) -> Policy:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw = _exact_object(raw, {"schema_version", "job_id", "endpoints"}, "policy")
    if raw["schema_version"] != SCHEMA_VERSION or raw["job_id"] != JOB_ID:
        raise ContractError("policy identity mismatch")
    if not isinstance(raw["endpoints"], list) or not raw["endpoints"]:
        raise ContractError("policy endpoints must be a non-empty list")
    endpoints: dict[str, Endpoint] = {}
    endpoint_keys = {
        "id", "host", "port", "path", "connect_ips", "allowed_ipv4_cidrs", "allowed_ipv6_cidrs"
    }
    for item in raw["endpoints"]:
        item = _exact_object(item, endpoint_keys, "endpoint")
        endpoint_id_value, host_value, path_value = item["id"], item["host"], item["path"]
        port, connect_ips = item["port"], item["connect_ips"]
        if not all(isinstance(v, str) and v for v in (endpoint_id_value, host_value, path_value)):
            raise ContractError("endpoint id, host, and path must be non-empty strings")
        endpoint_id = str(endpoint_id_value)
        host = str(host_value)
        request_path = str(path_value)
        if endpoint_id in endpoints or port != 443 or not request_path.startswith("/") or "?" in request_path:
            raise ContractError("endpoint identity, port, or path is not permitted")
        if not isinstance(connect_ips, list) or not connect_ips:
            raise ContractError("endpoint connect_ips must be non-empty")
        networks = _parse_networks(item["allowed_ipv4_cidrs"], item["allowed_ipv6_cidrs"])
        checked_ips: list[str] = []
        for text in connect_ips:
            try:
                address = ipaddress.ip_address(text)
            except (TypeError, ValueError) as exc:
                raise ContractError("connect_ips must contain literal IP addresses") from exc
            if not any(address.version == network.version and address in network for network in networks):
                raise ContractError("connect IP is outside its explicit allowlist")
            checked_ips.append(str(address))
        endpoints[endpoint_id] = Endpoint(
            endpoint_id, host, port, request_path, tuple(checked_ips), networks
        )
    return Policy(endpoints)


def _open_beneath(root: Path, relative: str) -> bytes:
    """Read one regular single-link file beneath a pinned root, without symlinks."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {"", ".", ".."} for p in parts):
        raise ContractError("batch path must be a normalized relative path")
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current = os.open(root, flags_dir)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags_dir, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=current)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BATCH_BYTES:
                raise ContractError("batch must be a bounded, single-link regular file")
            data = bytearray()
            while len(data) <= MAX_BATCH_BYTES:
                chunk = os.read(fd, min(65536, MAX_BATCH_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_BATCH_BYTES:
                raise ContractError("batch exceeds byte limit")
            return bytes(data)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ContractError("batch path is unavailable or not safely traversable") from exc
    finally:
        os.close(current)


def load_batch(root: Path, relative: str) -> tuple[str, ...]:
    try:
        raw = json.loads(_open_beneath(root, relative))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError("batch is not canonical JSON") from exc
    raw = _exact_object(raw, {"schema_version", "job_id", "requests"}, "batch")
    if raw["schema_version"] != SCHEMA_VERSION or raw["job_id"] != JOB_ID:
        raise ContractError("batch identity mismatch")
    requests = raw["requests"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS:
        raise ContractError("batch request count is outside the fixed limit")
    endpoint_ids: list[str] = []
    for item in requests:
        item = _exact_object(item, {"endpoint"}, "request")
        if not isinstance(item["endpoint"], str) or not item["endpoint"]:
            raise ContractError("request endpoint must be a non-empty string")
        endpoint_ids.append(item["endpoint"])
    return tuple(endpoint_ids)


def _https_get(endpoint: Endpoint, address: str, timeout: float, max_bytes: int) -> tuple[int, bytes]:
    raw = socket.create_connection((address, endpoint.port), timeout=timeout)
    try:
        tls = ssl.create_default_context().wrap_socket(raw, server_hostname=endpoint.host)
        try:
            tls.settimeout(timeout)
            request = (
                f"GET {endpoint.path} HTTP/1.1\r\nHost: {endpoint.host}\r\n"
                "User-Agent: hermes-int-c/1\r\nAccept: application/json,text/plain\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            tls.sendall(request)
            response = http.client.HTTPResponse(tls)
            response.begin()
            if 300 <= response.status < 400:
                raise ContractError("redirects are denied")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ContractError("response exceeds byte limit")
            return response.status, body
        finally:
            tls.close()
    finally:
        raw.close()


def execute(policy: Policy, requests: tuple[str, ...], transport: Transport = _https_get,
            *, deadline: float | None = None) -> list[dict[str, object]]:
    stop_at = deadline if deadline is not None else time.monotonic() + MAX_RUNTIME_SECONDS
    results: list[dict[str, object]] = []
    for endpoint_id in requests:
        endpoint = policy.endpoints.get(endpoint_id)
        if endpoint is None:
            raise ContractError("batch names an endpoint outside the allowlist")
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            raise ContractError("batch runtime budget exhausted")
        failures: list[str] = []
        for address in endpoint.connect_ips:
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                raise ContractError("batch runtime budget exhausted")
            try:
                status, body = transport(endpoint, address, min(remaining, 20.0), MAX_RESPONSE_BYTES)
            except (OSError, ssl.SSLError) as exc:
                failures.append(type(exc).__name__)
                continue
            if not 200 <= status < 300:
                raise ContractError(f"allowed endpoint returned HTTP {status}")
            if len(body) > MAX_RESPONSE_BYTES:
                raise ContractError("response exceeds byte limit")
            results.append({
                "endpoint": endpoint.endpoint_id,
                "status": status,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            })
            break
        else:
            raise ContractError(f"all fixed addresses failed: {','.join(failures)}")
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-int-c-worker")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        policy = load_policy(args.policy)
        requests = load_batch(args.batch_root, args.batch)
        unknown = sorted(set(requests) - set(policy.endpoints))
        if unknown:
            raise ContractError("batch names an endpoint outside the allowlist")
        if args.validate_only:
            result: object = {"job_id": JOB_ID, "requests": len(requests), "validated": True}
        else:
            result = execute(policy, requests)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "job_id": JOB_ID}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
