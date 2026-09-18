"""Runs ping and traceroute against a target host and returns structured,
JSON-serializable results. Never raises for expected network failures
(timeouts, unreachable hosts, DNS failures) - those are recorded as
success=False so the collector keeps running.
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
from datetime import datetime, timezone
from typing import Any

from config import AppConfig, Target

logger = logging.getLogger("network-probe-sidecar.prober")

_PING_SUMMARY_RE = re.compile(
    r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?"
    r"(\d+(?:\.\d+)?)% packet loss",
    re.DOTALL,
)
_PING_RTT_RE = re.compile(
    r"(?:rtt|round-trip) min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)"
)
_TRACEROUTE_HOP_RE = re.compile(r"^\s*(\d+)\s+(.+)$")
_RTT_TOKEN_RE = re.compile(r"([\d.]+)\s*ms")


def resolve_host(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        return None


def run_ping(host: str, cfg: AppConfig) -> dict[str, Any]:
    cmd = ["ping", "-n", "-c", str(cfg.ping_count), "-W", str(cfg.ping_timeout_seconds), host]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=cfg.ping_count * cfg.ping_timeout_seconds + 5,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "ping timed out"}
    except OSError as exc:
        return {"success": False, "error": f"ping failed to start: {exc}"}

    output = proc.stdout + proc.stderr
    result: dict[str, Any] = {"success": False}
    if cfg.include_raw_output:
        result["raw_output"] = output.strip()

    summary = _PING_SUMMARY_RE.search(output)
    if summary:
        sent, received, loss_pct = summary.groups()
        result["packets_sent"] = int(sent)
        result["packets_received"] = int(received)
        result["packet_loss_pct"] = float(loss_pct)
        result["success"] = int(received) > 0

    rtt = _PING_RTT_RE.search(output)
    if rtt:
        mn, avg, mx, mdev = (float(v) for v in rtt.groups())
        result["rtt_min_ms"] = mn
        result["rtt_avg_ms"] = avg
        result["rtt_max_ms"] = mx
        result["rtt_mdev_ms"] = mdev

    if not summary and not rtt:
        result["error"] = "could not parse ping output"

    return result


def run_traceroute(host: str, cfg: AppConfig) -> dict[str, Any]:
    cmd = [
        "traceroute", "-n",
        "-m", str(cfg.traceroute_max_hops),
        "-q", str(cfg.traceroute_probes_per_hop),
        "-w", str(cfg.traceroute_timeout_seconds),
        host,
    ]
    hard_timeout = (
        cfg.traceroute_max_hops * cfg.traceroute_timeout_seconds * cfg.traceroute_probes_per_hop
        + 10
    )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=hard_timeout)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "traceroute timed out"}
    except OSError as exc:
        return {"success": False, "error": f"traceroute failed to start: {exc}"}

    output = proc.stdout + proc.stderr
    result: dict[str, Any] = {"success": proc.returncode == 0}
    if cfg.include_raw_output:
        result["raw_output"] = output.strip()

    hops = []
    for line in output.splitlines():
        match = _TRACEROUTE_HOP_RE.match(line)
        if not match:
            continue
        hop_num, rest = match.groups()
        rest = rest.strip()
        tokens = rest.split()
        first_token = tokens[0] if tokens else ""
        ip = None if first_token in ("*", "") else first_token
        rtts = [float(v) for v in _RTT_TOKEN_RE.findall(rest)]
        hops.append({"hop": int(hop_num), "ip": ip, "rtt_ms": rtts})

    result["hop_count"] = len(hops)
    result["hops"] = hops
    if not hops:
        result.setdefault("error", "could not parse traceroute output")

    return result


def probe_target(target: Target, cfg: AppConfig) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc)
    logger.debug("Probing %s/%s (%s)", target.service, target.region, target.host)

    resolved_ip = resolve_host(target.host)

    record: dict[str, Any] = {
        "timestamp_utc": timestamp.isoformat(),
        "service": target.service,
        "region": target.region,
        "host": target.host,
        "resolved_ip": resolved_ip,
    }

    if resolved_ip is None:
        record["ping"] = {"success": False, "error": "DNS resolution failed"}
        record["traceroute"] = {"success": False, "error": "DNS resolution failed"}
        logger.warning(
            "DNS resolution failed for %s (%s/%s)", target.host, target.service, target.region
        )
        return record

    record["ping"] = run_ping(target.host, cfg)
    record["traceroute"] = run_traceroute(target.host, cfg)

    if not record["ping"].get("success"):
        logger.warning(
            "Ping failed for %s/%s (%s)", target.service, target.region, target.host
        )

    return record
