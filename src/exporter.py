#!/usr/bin/env python3
"""
Network Probe Exporter

Reads the most recent JSONL record per target from the probe output directory
and exposes them as Prometheus metrics at /metrics. Same pattern as
proxmox-disk-sidecar: collector thread in background, HTTPServer in foreground.

Endpoints:
  /metrics  — Prometheus text format
  /healthz  — returns "ok" (used by Docker healthcheck)
"""

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    generate_latest,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("network-probe-exporter")

METRICS_PORT = int(os.environ.get("METRICS_PORT", "9200"))
COLLECT_INTERVAL = int(os.environ.get("COLLECT_INTERVAL", "30"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/probes"))

# Custom registry — avoids exposing default Python process metrics
registry = CollectorRegistry()

# --- Metric definitions (mirror the proxmox-disk-sidecar label style) -------

probe_ping_success = Gauge(
    "network_probe_ping_success",
    "1 if the last ping cycle had at least one packet received, 0 otherwise",
    ["service", "region", "host"],
    registry=registry,
)
probe_ping_packet_loss_pct = Gauge(
    "network_probe_ping_packet_loss_pct",
    "Packet loss percentage from the last ping cycle",
    ["service", "region", "host"],
    registry=registry,
)
probe_ping_rtt_avg_ms = Gauge(
    "network_probe_ping_rtt_avg_ms",
    "Average RTT in milliseconds from the last ping cycle",
    ["service", "region", "host"],
    registry=registry,
)
probe_ping_rtt_min_ms = Gauge(
    "network_probe_ping_rtt_min_ms",
    "Minimum RTT in milliseconds from the last ping cycle",
    ["service", "region", "host"],
    registry=registry,
)
probe_ping_rtt_max_ms = Gauge(
    "network_probe_ping_rtt_max_ms",
    "Maximum RTT in milliseconds from the last ping cycle",
    ["service", "region", "host"],
    registry=registry,
)
probe_traceroute_hop_count = Gauge(
    "network_probe_traceroute_hop_count",
    "Number of hops to reach the target in the last traceroute",
    ["service", "region", "host"],
    registry=registry,
)
probe_traceroute_success = Gauge(
    "network_probe_traceroute_success",
    "1 if traceroute exited successfully, 0 otherwise",
    ["service", "region", "host"],
    registry=registry,
)
probe_dns_resolution_success = Gauge(
    "network_probe_dns_resolution_success",
    "1 if DNS resolution succeeded in the last cycle, 0 otherwise",
    ["service", "region", "host"],
    registry=registry,
)

probe_last_record_timestamp = Gauge(
    "network_probe_last_record_timestamp",
    "Unix timestamp from the probe record itself (data freshness, per target)",
    ["service", "region", "host"],
    registry=registry,
)

# Exporter self-health
exporter_last_collect_timestamp = Gauge(
    "network_probe_exporter_last_collect_timestamp",
    "Unix timestamp of the last successful collection pass",
    registry=registry,
)
exporter_targets_found = Gauge(
    "network_probe_exporter_targets_found",
    "Number of distinct service/region targets found in the data directory",
    registry=registry,
)


# --- Data reading -----------------------------------------------------------

def find_latest_record(data_dir: Path, service: str, region: str) -> dict | None:
    """
    Walks backwards through the UTC partitioned tree (year/month/day/hour) to
    find the most recent JSONL file for a given service+region and returns its
    last line (most recent probe record).
    """
    filename = f"{service}_{region}.jsonl"

    # Collect all matching files and sort descending so we try newest first
    candidates = sorted(data_dir.rglob(filename), reverse=True)
    if not candidates:
        return None

    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            if not lines:
                continue
            return json.loads(lines[-1])
        except Exception as exc:
            log.warning("Failed to read %s: %s", path, exc)
            continue

    return None


def discover_targets(data_dir: Path) -> list[tuple[str, str]]:
    """
    Returns a list of (service, region) tuples by scanning filenames in the
    data directory. This means the exporter automatically picks up new targets
    added to targets.yaml without any config change on its side.
    """
    seen = set()
    for path in data_dir.rglob("*.jsonl"):
        stem = path.stem  # e.g. "aws_sa-east-1"
        parts = stem.split("_", 1)
        if len(parts) == 2:
            seen.add((parts[0], parts[1]))
    return sorted(seen)


# --- Collection loop --------------------------------------------------------

def collect_metrics() -> None:
    while True:
        log.info("Starting exporter collection pass...")

        if not DATA_DIR.exists():
            log.warning("DATA_DIR %s does not exist yet — waiting", DATA_DIR)
            time.sleep(COLLECT_INTERVAL)
            continue

        targets = discover_targets(DATA_DIR)
        log.info("Discovered %d targets: %s", len(targets), targets)
        exporter_targets_found.set(len(targets))

        for service, region in targets:
            record = find_latest_record(DATA_DIR, service, region)
            if record is None:
                log.warning("No records found for %s/%s", service, region)
                continue

            host = record.get("host", "unknown")
            labels = {"service": service, "region": region, "host": host}

            # Record timestamp (data freshness)
            ts = record.get("timestamp_utc")
            if ts:
                try:
                    # Normalize trailing 'Z' (UTC) which fromisoformat rejects on older CPython
                    normalized = ts.replace("Z", "+00:00")
                    probe_last_record_timestamp.labels(**labels).set(
                        datetime.fromisoformat(normalized).timestamp()
                    )
                except ValueError:
                    log.warning("Could not parse timestamp_utc %r for %s/%s", ts, service, region)

            # DNS
            resolved = record.get("resolved_ip")
            probe_dns_resolution_success.labels(**labels).set(1 if resolved else 0)

            # Ping
            ping = record.get("ping", {})
            probe_ping_success.labels(**labels).set(1 if ping.get("success") else 0)
            if "packet_loss_pct" in ping:
                probe_ping_packet_loss_pct.labels(**labels).set(ping["packet_loss_pct"])
            if "rtt_avg_ms" in ping:
                probe_ping_rtt_avg_ms.labels(**labels).set(ping["rtt_avg_ms"])
            if "rtt_min_ms" in ping:
                probe_ping_rtt_min_ms.labels(**labels).set(ping["rtt_min_ms"])
            if "rtt_max_ms" in ping:
                probe_ping_rtt_max_ms.labels(**labels).set(ping["rtt_max_ms"])

            # Traceroute
            tr = record.get("traceroute", {})
            probe_traceroute_success.labels(**labels).set(1 if tr.get("success") else 0)
            if "hop_count" in tr:
                probe_traceroute_hop_count.labels(**labels).set(tr["hop_count"])

            log.debug(
                "%s/%s — ping_success=%s loss=%.1f%% rtt_avg=%sms hops=%s",
                service, region,
                ping.get("success"),
                ping.get("packet_loss_pct", -1),
                ping.get("rtt_avg_ms", "n/a"),
                tr.get("hop_count", "n/a"),
            )

        exporter_last_collect_timestamp.set(time.time())
        log.info("Collection pass complete. Next in %ds.", COLLECT_INTERVAL)
        time.sleep(COLLECT_INTERVAL)

# --- HTTP server ------------------------------------------------------------

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            output = generate_latest(registry)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)
        elif self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # silence default HTTP access log
        pass


# --- Entry point ------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Network Probe Exporter")
    log.info("  Data dir     : %s", DATA_DIR)
    log.info("  Metrics port : %d", METRICS_PORT)
    log.info("  Collect interval: %ds", COLLECT_INTERVAL)

    collector_thread = threading.Thread(target=collect_metrics, daemon=True)
    collector_thread.start()

    server = HTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    log.info("Metrics available at http://0.0.0.0:%d/metrics", METRICS_PORT)
    server.serve_forever()
