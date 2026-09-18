# network-probe-sidecar

Long-lived Python collector, same style as `proxmox-disk-sidecar`. Every
`INTERVAL_SECONDS` (default 30s) it pings and traceroutes a configurable list
of external targets (Google, Microsoft, AWS across BR/US/EU) and writes the
results as JSON Lines, partitioned by **UTC** year/month/day/hour.

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
    "hops": [{"hop": 1, "ip": "10.100.1.1", "rtt_ms": [0.4]}]
  }
}
```

DNS failures, timeouts, and packet loss are all recorded (`success: false` +
an `error` field) instead of crashing the collector - it's meant to run
unattended for weeks.

## Deploy on SRV04

This assumes SRV04 already has Docker (same setup as the Semaphore UI /
proxmox-disk-sidecar deployments). Provisioning the VM itself, if you want a
dedicated one rather than reusing an existing Docker host, is still a manual
step through the PVE web UI, same as always - this repo just gives you what
to put on it afterwards.

```bash
git clone <this-repo> network-probe-sidecar
cd network-probe-sidecar
cp .env.example .env        # adjust intervals/timeouts if needed
docker compose up -d --build
docker compose logs -f      # confirm cycles are running every 30s
```

Adjust `config/targets.yaml` to change which hosts get probed - no code
changes needed, just restart the container to pick up edits (or add a
`docker compose watch` / bind-mount reload if you want hot-reload later).

## Notes / follow-ups

- `cap_add: NET_RAW, NET_ADMIN` is required for ping/traceroute inside the
  container without running it privileged.
- `MAX_WORKERS` controls how many targets are probed in parallel; keep it
  >= the number of targets so a full cycle comfortably fits inside
  `INTERVAL_SECONDS`. With 8 targets and the default timeouts, a cycle
  typically finishes well under 30s, but if you add many more targets or
  tighten timeouts, watch the "cycle took Xs" warnings in the logs.
- Same CI/CD path as `proxmox-disk-sidecar` would work here too (GitHub
  Actions -> Docker Hub -> Komodo pulls the new tag) if you want this
  building automatically instead of `docker compose up --build` on the box.
- Natural next step if you want this in Grafana: add a small exporter that
  tails the latest JSONL per target and exposes packet loss / avg RTT as
  Prometheus gauges - happy to help with that once this is collecting data.
