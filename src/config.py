"""Configuration loading.

Operational settings (interval, timeouts, output dir, log level) come from
environment variables / .env, following the project's convention of keeping
.env for operational settings only. Target definitions (which hosts to probe)
live in a separate YAML file, since they're structural data, not secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Target:
    service: str
    region: str
    host: str


@dataclass(frozen=True)
class AppConfig:
    output_dir: Path
    targets_file: Path
    interval_seconds: int
    ping_count: int
    ping_timeout_seconds: int
    traceroute_max_hops: int
    traceroute_timeout_seconds: int
    traceroute_probes_per_hop: int
    max_workers: int
    include_raw_output: bool
    log_level: str

    @property
    def cycle_hard_timeout(self) -> int:
        """Upper bound (seconds) for a whole probe cycle before we stop
        waiting on stragglers and move on, so one stuck target can't stall
        the collector forever."""
        return self.interval_seconds * 3

    @staticmethod
    def from_env() -> "AppConfig":
        return AppConfig(
            output_dir=Path(os.environ.get("OUTPUT_DIR", "/data/probes")),
            targets_file=Path(os.environ.get("TARGETS_FILE", "/app/config/targets.yaml")),
            interval_seconds=int(os.environ.get("INTERVAL_SECONDS", "30")),
            ping_count=int(os.environ.get("PING_COUNT", "4")),
            ping_timeout_seconds=int(os.environ.get("PING_TIMEOUT_SECONDS", "5")),
            traceroute_max_hops=int(os.environ.get("TRACEROUTE_MAX_HOPS", "20")),
            traceroute_timeout_seconds=int(os.environ.get("TRACEROUTE_TIMEOUT_SECONDS", "2")),
            traceroute_probes_per_hop=int(os.environ.get("TRACEROUTE_PROBES_PER_HOP", "1")),
            max_workers=int(os.environ.get("MAX_WORKERS", "8")),
            include_raw_output=os.environ.get("INCLUDE_RAW_OUTPUT", "true").lower() == "true",
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )

    @staticmethod
    def load_targets(targets_file: Path) -> list[Target]:
        with open(targets_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        targets = [
            Target(service=entry["service"], region=entry["region"], host=entry["host"])
            for entry in data.get("targets", [])
        ]
        if not targets:
            raise ValueError(f"No targets found in {targets_file}")
        return targets
