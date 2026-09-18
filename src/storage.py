"""Persists probe records as JSON Lines files, partitioned into
output_dir/YYYY/MM/DD/HH/ folders (UTC), one file per service+region.

One file per hour per target keeps the file count sane at a 30s interval
(120 lines/hour/target) while still giving you an easy drill-down path.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_write_lock = threading.Lock()


def _partition_dir(output_dir: Path, timestamp: datetime) -> Path:
    return (
        output_dir
        / f"{timestamp.year:04d}"
        / f"{timestamp.month:02d}"
        / f"{timestamp.day:02d}"
        / f"{timestamp.hour:02d}"
    )


def write_record(output_dir: Path, record: dict[str, Any]) -> Path:
    timestamp = datetime.fromisoformat(record["timestamp_utc"])
    partition = _partition_dir(output_dir, timestamp)
    partition.mkdir(parents=True, exist_ok=True)

    file_path = partition / f"{record['service']}_{record['region']}.jsonl"
    line = json.dumps(record, ensure_ascii=False)

    # Threads write concurrently (one per target); JSONL append is safe as
    # long as writes are serialized, so guard with a single process-wide lock.
    with _write_lock:
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    return file_path
