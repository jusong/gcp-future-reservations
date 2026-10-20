# GCP Future Reservation Batch Creator

Describe **incremental demand** per machine type and zone in a YAML file. The script then:

1. Lists existing future reservations in the project
2. Skips items that conflict (same zone + machine type with overlapping time windows)
3. Counts currently running VMs as inventory for each `(zone, machine_type)`
4. Sets `total_count = demand + inventory` and calls `gcloud compute future-reservations create`

## Layout

```text
gcp-future-reservations/
├── reservations.yaml       # config (defaults + per-item demand)
├── create_reservations.py  # main script (Python 3.9+)
├── requirements.txt        # pyyaml
└── README.md
```

## Dependencies

- Python 3.9+
- `pyyaml` (`pip install -r requirements.txt`)
- `gcloud` CLI installed and authenticated:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

## Usage

### 1. Edit `reservations.yaml`

Set at least `defaults.project`, `defaults.region`, `start_time`, `end_time`,
plus machine type and **incremental demand** per zone for each reservation.

### 2. Dry-run first

```bash
python3 create_reservations.py --config reservations.yaml --dry-run
```

Prints each `gcloud` command and writes a reviewable/runnable script to `logs/dryrun-*.sh`.

### 3. Execute

```bash
python3 create_reservations.py --config reservations.yaml
```

Logs go to `logs/run-YYYYmmdd-HHMMSS.log`. Failed item names are saved to
`logs/failed-*.txt` for precise retry with `--filter`.

### 4. Filter / retry

```bash
# Only items whose name contains app-b
python3 create_reservations.py --filter 'app-b'

# Only items matching a regex
python3 create_reservations.py --filter '^app-a-64v128g$'
```

### CLI flags

| Flag | Description |
|------|-------------|
| `--config <file>` | YAML config path (default: `reservations.yaml`) |
| `--dry-run` | Print gcloud commands without executing |
| `--filter <regex>` | Only process reservation names matching this regex |
| `--project <id>` | Override `defaults.project` |
| `--region <id>` | Override `defaults.region` |
| `--log-dir <dir>` | Log directory (default: `./logs`) |
| `--name-suffix <val>` | Suffix appended to reservation names: default is the run timestamp (`YYYYMMDD-HHMMSS`); `none` disables; or pass a fixed string |
| `-v, --verbose` | Debug logging |

## Config fields

### `defaults`

| Field | Required | Description |
|-------|----------|-------------|
| `project` | yes | GCP project ID |
| `region`  | yes | Default region; used to validate that zones belong to it |
| `name_prefix` | no | Prefix for FR names: `{name_prefix}-{name}-{zone_short}` |
| `start_time` / `end_time` | yes | RFC3339 window, e.g. `"2026-05-01T00:00:00Z"` |
| `planning_status` | no | `DRAFT` (default) or `SUBMITTED` |
| `share_setting` | no | `local` (default) or `specific_projects` |
| `shared_projects` | conditional | Required list when `share_setting=specific_projects` |
| `auto_delete_auto_created_reservations` | no | Default `true` |
| `auto_created_reservations_delete_time` | no | Optional RFC3339 |
| `vm_family` | no | e.g. `GENERAL_PURPOSE_N2` |
| `instance_label_filter` | no | Label filter for inventory VMs, e.g. `app=myapp`; omit for no label filter |

### Each `reservation`

Specify the machine type in one of two ways:

- Option A: `machine_type: n2-standard-8` (predefined or a fully formed custom string such as `n2-custom-64-131072`, `custom-32-29440`)
- Option B: `series` + `cpu` + `memory`
  - `memory` accepts: `128G`, `128GB`, `128 GiB`, `131072`, `131072MB`, `0.5G`
  - Normalized to integer MB, then assembled as `{prefix}{cpu}-{mb}` (appends `-ext` when needed)
  - Optional `extended: true|false` to override

Generated reservation name format:

```text
{name_prefix}-{name}-{zone_short}-{YYYYMMDD-HHMMSS}
```

The timestamp suffix is generated once per run and shared by all items in that run.
Disable with `--name-suffix=none`, or set a fixed value with `--name-suffix=20260113-091320`.

`zones` maps AZ → **incremental demand**:

```yaml
zones:
  asia-northeast1-a: 5
  asia-northeast1-b: 5
  asia-northeast1-c: 4
```

Use `overrides:` on an item to locally override `start_time` / `end_time` / `share_setting` / etc.

## Custom machine type rules

Built-in series metadata (standard ratio cap + `-ext` support):

| series | prefix | standard cap (GB/vCPU) | `-ext` |
|--------|--------|------------------------|--------|
| N1  | `custom-`     | 6.5 | yes |
| N2  | `n2-custom-`  | 8   | yes |
| N2D | `n2d-custom-` | 8   | yes |
| N4  | `n4-custom-`  | 8   | yes |
| N4A | `n4a-custom-` | 8   | yes |
| N4D | `n4d-custom-` | 8   | yes |
| E2  | `e2-custom-`  | 8   | no (errors if over the cap) |

Other series (C3, T2D, …) do not support Custom Machine Types; the script exits at parse time.

Constraints:
- `memory_mb` must be a multiple of 256
- `cpu` must be 1 or even, and within the series max

## Conflict detection

On start, the script runs `gcloud compute future-reservations list --format=json` once,
then for each planned item checks:

1. Same `zone` (short name, e.g. `asia-northeast1-a`)
2. Same `machine_type` (normalized string equality; works for predefined and custom)
3. `status.procurementStatus` is in the “still occupying” set (excludes `CANCELLED` / `DECLINED` / `FAILED`, etc.)
4. Time-window overlap: `existing.start < new.end AND existing.end > new.start`

Any conflict skips the item and logs the reason plus the existing FR name.

## Inventory query

```bash
gcloud compute instances list \
  --project="$PROJECT" \
  --zones="$ZONE" \
  --filter="machineType:($MACHINE_TYPE) AND status=RUNNING" \
  --format=value(name)
```

- `--zones=ZONE` hits the single-zone endpoint and avoids the default `aggregated/instances`
  API (which often times out or drops under a proxy)
- `machineType` uses substring matching for both predefined and custom types
- With `instance_label_filter: app=myapp`, appends `AND labels.app=myapp`
- The count is `inventory_count`; `total_count = demand + inventory_count`

## Transient error retries

Each gcloud call (list FRs / list instances / create) retries up to 3 times with
exponential backoff when stderr matches:

- `ProxyError` / `RemoteDisconnected` / `Max retries exceeded`
- `Connection reset` / `Connection aborted` / `ConnectionResetError`
- `Read timed out` / `Temporary failure`
- HTTP 5xx / `Backend Error`
- `gcloud crashed` / `Please try again`

Initial backoff is 2s (2s → 4s → 8s). Persistent failures go to the failed list.

## Sample output

```text
10:00:01 INFO  log file: logs/run-20260501-100001.log
10:00:01 INFO  planned items: 21
10:00:02 INFO  fetched 3 existing future reservation(s) from project my-gcp-project
10:00:02 WARNING [SKIP] example-fr-app-a-64v128g-a: conflicts with existing FR 'old-fr-a'
       zone=asia-northeast1-a machine_type=n2-custom-64-131072
       existing window = [2026-04-20T00:00Z, 2026-05-10T00:00Z] status=APPROVED
       new window      = [2026-05-01T00:00:00Z, 2026-05-15T00:00:00Z]
10:00:03 INFO  [PLAN] example-fr-app-a-64v128g-b  zone=asia-northeast1-b  machine=n2-custom-64-131072
       demand=5  inventory(running_vms)=3  total=8
10:00:03 INFO  [CMD ] gcloud compute future-reservations create example-fr-app-a-64v128g-b ...
10:00:04 INFO  [OK  ] example-fr-app-a-64v128g-b created
...
10:00:30 INFO  === SUMMARY ===
10:00:30 INFO  planned: 21  ok: 18  skipped(conflict): 2  failed: 1
```

## Out of scope

- List / delete future reservations (use `gcloud compute future-reservations list|describe|cancel|delete`)
- Creating a GCP project / enabling APIs
- Quota requests (GCP still enforces quota; failures appear on gcloud stderr and are recorded as failed)
