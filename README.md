# network-probe-sidecar

Network reachability and latency monitoring for external services, packaged as
a single Docker image with two entrypoints:

- **Collector** (`main.py`): a long-lived loop that, every `INTERVAL_SECONDS`
  (default 30s), resolves, pings and traceroutes a configurable list of external
  targets (Google, Microsoft, AWS across BR/US/EU) and writes the results as
  JSON Lines, partitioned by **UTC** year/month/day/hour.
- **Exporter** (`exporter.py`): reads the latest record per target and exposes
  it as Prometheus metrics on `:9200` (`/metrics`, `/healthz`).

```
collector ──> data/ (JSONL) ──> exporter ──> :9200/metrics ──> Prometheus
```

Both can run on the same host, or on different hosts sharing the data
directory read-only (e.g. over NFS).

## Output layout

```
data/
└── 2026/
    └── 09/
        └── 18/
            └── 14/
                ├── google_brazil.jsonl
                ├── google_us.jsonl
                ├── google_europe.jsonl
                ├── microsoft_brazil.jsonl
                ├── microsoft_us.jsonl
                ├── microsoft_europe.jsonl
                ├── aws_sa-east-1.jsonl
                └── aws_us-east-1.jsonl
```

One file per `service_region` per UTC hour; each line is one probe cycle:

```json
{
  "timestamp_utc": "2026-09-18T14:32:01.123456+00:00",
  "service": "aws",
  "region": "sa-east-1",
  "host": "ec2.sa-east-1.amazonaws.com",
  "resolved_ip": "52.67.10.20",
  "ping": {
    "success": true,
    "packets_sent": 4,
    "packets_received": 4,
    "packet_loss_pct": 0.0,
    "rtt_min_ms": 12.3,
    "rtt_avg_ms": 13.1,
    "rtt_max_ms": 14.0,
    "rtt_mdev_ms": 0.5
  },
  "traceroute": {
    "success": true,
    "hop_count": 14,
    "hops": [{ "hop": 1, "ip": "192.168.1.1", "rtt_ms": [0.4] }]
  }
}
```

DNS failures, timeouts, and packet loss are all recorded (`success: false` +
an `error` field) instead of crashing the collector - it's meant to run
unattended for weeks.

## Running the collector

Requires a host with Docker and outbound ICMP/UDP to the targets.

```bash
git clone https://github.com/nataannn/network-probe-sidecar.git
cd network-probe-sidecar
cp .env.example .env        # adjust intervals/timeouts if needed
docker compose pull         # use the published image
docker compose up -d        # or: docker compose up -d --build (build from source)
docker compose logs -f      # confirm cycles are running every 30s
```

Probe results land in `./data`. Edit `config/targets.yaml` to change which
hosts get probed - no code changes needed, just `docker compose restart` to
pick up edits.

## Running the exporter

Same image, different command. Point it at the collector's data directory
(read-only):

```yaml
services:
  network-probe-exporter:
    image: nataannn/network-probe-sidecar:latest
    command: ["python", "exporter.py"]
    restart: unless-stopped
    environment:
      METRICS_PORT: 9200
      COLLECT_INTERVAL: 30
      DATA_DIR: /data/probes
      LOG_LEVEL: INFO
    volumes:
      - /path/to/probe-data:/data/probes:ro
    ports:
      - "9200:9200"
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request; urllib.request.urlopen('http://localhost:9200/healthz')",
        ]
      interval: 30s
      timeout: 5s
      retries: 3
```

Targets are discovered automatically from the data directory - adding a target
to `targets.yaml` requires no exporter change.

If the data directory is a network mount, mount it on the host **before**
starting the container. A container started before the mount exists keeps a
stale view of an empty directory; recreate it (`docker rm -f` + `up -d`) to fix.

Prometheus scrape job:

```yaml
- job_name: network-probe-exporter
  scrape_interval: 30s
  scrape_timeout: 10s
  static_configs:
    - targets:
        - <exporter-host>:9200
```

## Metrics

All per-target metrics carry `service`, `region` and `host` labels.

| Metric                                                  | Description                                     |
| ------------------------------------------------------- | ----------------------------------------------- |
| `network_probe_ping_success`                            | 1 if the last ping succeeded                    |
| `network_probe_ping_packet_loss_pct`                    | Packet loss, %                                  |
| `network_probe_ping_rtt_avg_ms` / `_min_ms` / `_max_ms` | Round-trip time, ms                             |
| `network_probe_traceroute_success`                      | 1 if the last traceroute succeeded              |
| `network_probe_traceroute_hop_count`                    | Hops to the target                              |
| `network_probe_dns_resolution_success`                  | 1 if the hostname resolved                      |
| `network_probe_last_record_timestamp`                   | Timestamp of the latest record (data freshness) |
| `network_probe_exporter_last_collect_timestamp`         | Last exporter collection loop                   |
| `network_probe_exporter_targets_found`                  | Targets discovered in the data directory        |

## Known behaviors

- **AWS endpoints block ICMP.** `ping.success: false` and 100% packet loss are
  expected for them; DNS and traceroute still work. Exclude them from ICMP
  alerts with `service!="aws"`.
- **Some providers stop answering traceroute after a few hops** (`* * *`).
  This is expected and not a failure of the path.

## Alerting tips

- Alert on data freshness, not just exporter uptime:
  `time() - network_probe_last_record_timestamp > 300`. If the collector or the
  shared data directory stops, the exporter stays up and keeps serving the last
  values it read.
- Use a `for:` window (5-10 min) on loss and latency alerts; short spikes are
  common on the public internet.

## CI/CD

`.github/workflows/docker-publish.yml` builds and pushes the image to Docker Hub:

- push to `main` → `latest`
- tag `v*.*.*` → versioned tag

Requires the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.

## Notes

- `cap_add: NET_RAW, NET_ADMIN` is required for ping/traceroute inside the
  container without running it privileged.
- `MAX_WORKERS` controls how many targets are probed in parallel; keep it
  > = the number of targets so a full cycle comfortably fits inside
  > `INTERVAL_SECONDS`. With 8 targets and the default timeouts, a cycle
  > typically finishes well under 30s, but if you add many more targets or
  > tighten timeouts, watch the "cycle took Xs" warnings in the logs.
