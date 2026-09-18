"""Network Probe Sidecar

Long-lived collector that periodically checks reachability (ping + traceroute)
to a configurable list of external services and persists the results as
JSONL, partitioned by UTC year/month/day/hour. Follows the same
long-lived-Python-sidecar style as proxmox-disk-sidecar.
"""

from __future__ import annotations

import concurrent.futures
import logging
import signal
import time
from datetime import datetime, timezone

from config import AppConfig, Target
from prober import probe_target
from storage import write_record

logger = logging.getLogger("network-probe-sidecar")

_shutdown_requested = False


def _handle_shutdown(signum, _frame) -> None:
    global _shutdown_requested
    logger.info("Shutdown signal received (%s), finishing current cycle...", signum)
    _shutdown_requested = True


def run_cycle(cfg: AppConfig, targets: list[Target]) -> None:
    cycle_start = datetime.now(timezone.utc)
    logger.info("Starting probe cycle for %d targets", len(targets))

    worker_count = min(len(targets), cfg.max_workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(probe_target, target, cfg): target for target in targets}

        try:
            for future in concurrent.futures.as_completed(futures, timeout=cfg.cycle_hard_timeout):
                target = futures[future]
                try:
                    record = future.result()
                except Exception:
                    logger.exception("Unhandled error probing %s/%s", target.service, target.region)
                    continue
                try:
                    write_record(cfg.output_dir, record)
                except Exception:
                    logger.exception(
                        "Failed to persist record for %s/%s", target.service, target.region
                    )
        except concurrent.futures.TimeoutError:
            logger.error(
                "Cycle exceeded hard timeout of %ds - some targets did not complete",
                cfg.cycle_hard_timeout,
            )

    elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
    logger.info("Cycle finished in %.1fs", elapsed)
    if elapsed > cfg.interval_seconds:
        logger.warning(
            "Cycle took %.1fs, longer than the %ds interval - next cycle starts immediately",
            elapsed, cfg.interval_seconds,
        )


def main() -> None:
    cfg = AppConfig.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Network probe sidecar starting up | output_dir=%s interval=%ds targets_file=%s",
        cfg.output_dir, cfg.interval_seconds, cfg.targets_file,
    )

    targets = AppConfig.load_targets(cfg.targets_file)
    logger.info(
        "Loaded %d targets: %s",
        len(targets),
        ", ".join(f"{t.service}/{t.region}" for t in targets),
    )

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    next_run = time.monotonic()
    while not _shutdown_requested:
        run_cycle(cfg, targets)

        next_run += cfg.interval_seconds
        sleep_for = next_run - time.monotonic()

        if sleep_for <= 0:
            # We're behind schedule (cycle ran longer than the interval) -
            # resync instead of drifting further and further behind.
            next_run = time.monotonic()
            continue

        # Sleep in small slices so a shutdown signal is picked up promptly
        # instead of waiting out the full interval.
        deadline = time.monotonic() + sleep_for
        while time.monotonic() < deadline and not _shutdown_requested:
            time.sleep(min(1.0, deadline - time.monotonic()))

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
