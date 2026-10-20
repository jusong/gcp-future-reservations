#!/usr/bin/env python3
"""
Batch-create GCP Compute Engine future reservations from YAML.

Features:
  - Load YAML and expand to (reservation, zone) items
  - Before create, list existing future reservations in the project; skip when
    the same zone + machine_type overlaps in time
  - Count running VMs as inventory
  - total_count = demand (incremental from config) + inventory
  - Build and run gcloud compute future-reservations create
  - Supports dry-run, --filter regex, per-item overrides, share_setting, etc.

Requires: Python 3.9+, pyyaml, gcloud CLI (authenticated)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "[FATAL] pyyaml not installed. Run: pip install -r requirements.txt\n"
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("fr")


def setup_logging(verbose: bool, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"run-{ts}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    return log_path


# ---------------------------------------------------------------------------
# Machine type parse / normalize
# ---------------------------------------------------------------------------

# Series metadata: prefix + standard cap (GB/vCPU) + extended-memory support + vCPU max
# See https://cloud.google.com/compute/docs/instances/creating-instance-with-custom-machine-type
SERIES_SPEC: dict[str, dict[str, Any]] = {
    "n1":  {"prefix": "custom-",     "std_max_gb_per_cpu": 6.5, "supports_ext": True,  "max_cpu": 96},
    "n2":  {"prefix": "n2-custom-",  "std_max_gb_per_cpu": 8.0, "supports_ext": True,  "max_cpu": 128},
    "n2d": {"prefix": "n2d-custom-", "std_max_gb_per_cpu": 8.0, "supports_ext": True,  "max_cpu": 96},
    "n4":  {"prefix": "n4-custom-",  "std_max_gb_per_cpu": 8.0, "supports_ext": True,  "max_cpu": 80},
    "n4a": {"prefix": "n4a-custom-", "std_max_gb_per_cpu": 8.0, "supports_ext": True,  "max_cpu": 64},
    "n4d": {"prefix": "n4d-custom-", "std_max_gb_per_cpu": 8.0, "supports_ext": True,  "max_cpu": 96},
    "e2":  {"prefix": "e2-custom-",  "std_max_gb_per_cpu": 8.0, "supports_ext": False, "max_cpu": 32},
}


_MEMORY_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(g|gb|gib|m|mb|mib)?\s*$", re.IGNORECASE
)


def parse_memory_to_mb(value: Any) -> int:
    """
    Normalize memory to integer MB. Accepted forms:
      - int/float: treated as MB
      - "131072" / "131072M" / "131072 MB" / "131072MiB"
      - "128G" / "128 GB" / "128GiB" / "0.5G"
    Raises ValueError if not an integer MB or unparseable.
    """
    if isinstance(value, (int, float)):
        num = float(value)
        unit = "m"
    elif isinstance(value, str):
        m = _MEMORY_RE.match(value)
        if not m:
            raise ValueError(f"invalid memory value: {value!r}")
        num = float(m.group(1))
        unit = (m.group(2) or "m").lower()
    else:
        raise ValueError(f"invalid memory type: {type(value).__name__}")

    if unit in ("g", "gb", "gib"):
        mb = num * 1024.0
    elif unit in ("m", "mb", "mib"):
        mb = num
    else:  # pragma: no cover
        raise ValueError(f"unknown memory unit: {unit}")

    if abs(mb - round(mb)) > 1e-9:
        raise ValueError(f"memory {value!r} does not resolve to an integer MB ({mb})")
    return int(round(mb))


def normalize_machine_type(item: dict[str, Any]) -> str:
    """Build a machine_type string usable by gcloud.

    Supports:
      - machine_type: return as-is
      - series + cpu + memory (or memory_mb): assemble via SERIES_SPEC, append -ext if needed
    """
    mt = item.get("machine_type")
    if mt:
        if any(k in item for k in ("series", "cpu", "memory", "memory_mb")):
            raise ValueError(
                f"item {item.get('name')!r}: machine_type and series/cpu/memory are mutually exclusive"
            )
        return str(mt).strip()

    series = item.get("series")
    cpu = item.get("cpu")
    mem = item.get("memory", item.get("memory_mb"))
    if not (series and cpu and mem is not None):
        raise ValueError(
            f"item {item.get('name')!r}: must provide either machine_type or (series + cpu + memory)"
        )

    series_key = str(series).lower()
    spec = SERIES_SPEC.get(series_key)
    if not spec:
        raise ValueError(
            f"item {item.get('name')!r}: series {series!r} does not support custom machine types "
            f"(supported: {sorted(SERIES_SPEC.keys())})"
        )

    cpu_int = int(cpu)
    if cpu_int < 1:
        raise ValueError(f"item {item.get('name')!r}: cpu must be >= 1")
    if cpu_int != 1 and cpu_int % 2 != 0:
        raise ValueError(
            f"item {item.get('name')!r}: cpu must be 1 or an even number (got {cpu_int})"
        )
    if cpu_int > spec["max_cpu"]:
        raise ValueError(
            f"item {item.get('name')!r}: cpu {cpu_int} exceeds {series_key.upper()} max {spec['max_cpu']}"
        )

    memory_mb = parse_memory_to_mb(mem)
    if memory_mb % 256 != 0:
        raise ValueError(
            f"item {item.get('name')!r}: memory must be a multiple of 256 MB "
            f"(got {memory_mb} MB)"
        )

    ratio = memory_mb / 1024.0 / cpu_int
    std_max = spec["std_max_gb_per_cpu"]
    needs_ext = ratio > std_max + 1e-9

    extended_flag = item.get("extended")
    if extended_flag is not None:
        if extended_flag and not spec["supports_ext"]:
            raise ValueError(
                f"item {item.get('name')!r}: series {series_key.upper()} does not support extended memory"
            )
        use_ext = bool(extended_flag)
    else:
        if needs_ext and not spec["supports_ext"]:
            raise ValueError(
                f"item {item.get('name')!r}: {ratio:.2f} GB/vCPU exceeds {series_key.upper()} "
                f"standard limit {std_max} and this series does not support extended memory"
            )
        use_ext = needs_ext

    suffix = "-ext" if use_ext else ""
    return f"{spec['prefix']}{cpu_int}-{memory_mb}{suffix}"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


@dataclass
class PlannedItem:
    """One expanded future reservation to create."""

    fr_name: str            # final gcloud name (prefix + zone short code)
    src_name: str           # original reservation item name
    zone: str
    machine_type: str
    demand: int
    project: str
    region: str
    start_time: str
    end_time: str
    planning_status: str
    share_setting: str
    shared_projects: list[str] = field(default_factory=list)
    vm_family: Optional[str] = None
    auto_delete: bool = True
    auto_created_reservations_delete_time: Optional[str] = None
    instance_label_filter: Optional[str] = None

    # Filled at runtime
    inventory: int = 0
    total: int = 0


ZONE_SHORTCODE_RE = re.compile(r"[a-z]$", re.IGNORECASE)


def zone_short(zone: str) -> str:
    m = ZONE_SHORTCODE_RE.search(zone)
    return m.group(0).lower() if m else zone


def load_config(
    path: Path,
    *,
    cli_project: Optional[str],
    cli_region: Optional[str],
    filter_regex: Optional[str],
    name_suffix: Optional[str] = None,
) -> list[PlannedItem]:
    """Load YAML, merge defaults/overrides, expand into PlannedItem list."""
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    defaults = cfg.get("defaults") or {}
    reservations = cfg.get("reservations") or []
    if not isinstance(reservations, list) or not reservations:
        raise ValueError("config.reservations must be a non-empty list")

    filt = re.compile(filter_regex) if filter_regex else None

    project = cli_project or defaults.get("project")
    region = cli_region or defaults.get("region")
    if not project:
        raise ValueError("project is required (in defaults.project or --project)")
    if not region:
        raise ValueError("region is required (in defaults.region or --region)")

    name_prefix = str(defaults.get("name_prefix", "")).strip()

    planned: list[PlannedItem] = []
    for idx, raw in enumerate(reservations):
        if not isinstance(raw, dict):
            raise ValueError(f"reservations[{idx}] must be a mapping")
        src_name = raw.get("name")
        if not src_name:
            raise ValueError(f"reservations[{idx}]: missing 'name'")

        if filt and not filt.search(src_name):
            continue

        overrides = raw.get("overrides") or {}

        def pick(key: str, default: Any = None) -> Any:
            if key in overrides:
                return overrides[key]
            if key in raw:
                return raw[key]
            return defaults.get(key, default)

        machine_type = normalize_machine_type(raw)

        start_time = pick("start_time")
        end_time = pick("end_time")
        if not start_time or not end_time:
            raise ValueError(
                f"reservation {src_name!r}: start_time and end_time are required"
            )

        planning_status = str(pick("planning_status", "DRAFT")).upper()
        if planning_status not in ("DRAFT", "SUBMITTED"):
            raise ValueError(
                f"reservation {src_name!r}: planning_status must be DRAFT or SUBMITTED"
            )

        share_setting = str(pick("share_setting", "local")).lower()
        if share_setting not in ("local", "specific_projects"):
            raise ValueError(
                f"reservation {src_name!r}: share_setting must be local or specific_projects"
            )
        shared_projects = pick("shared_projects") or []
        if share_setting == "specific_projects" and not shared_projects:
            raise ValueError(
                f"reservation {src_name!r}: share_setting=specific_projects requires non-empty shared_projects"
            )

        vm_family = pick("vm_family")
        auto_delete = bool(pick("auto_delete_auto_created_reservations", True))
        auto_del_time = pick("auto_created_reservations_delete_time")
        instance_label_filter = pick("instance_label_filter")

        zones = raw.get("zones") or {}
        if not isinstance(zones, dict) or not zones:
            raise ValueError(f"reservation {src_name!r}: zones must be a non-empty mapping")

        for zone, demand in zones.items():
            demand_int = int(demand)
            if demand_int < 0:
                raise ValueError(
                    f"reservation {src_name!r} zone {zone}: demand must be >= 0"
                )
            zone_str = str(zone)
            if not zone_str.startswith(region):
                logger.warning(
                    "reservation %s zone %s does not belong to region %s",
                    src_name, zone_str, region,
                )
            fr_name_parts = [
                p for p in (name_prefix, src_name, zone_short(zone_str), name_suffix) if p
            ]
            fr_name = "-".join(fr_name_parts)

            planned.append(
                PlannedItem(
                    fr_name=fr_name,
                    src_name=src_name,
                    zone=zone_str,
                    machine_type=machine_type,
                    demand=demand_int,
                    project=project,
                    region=region,
                    start_time=start_time,
                    end_time=end_time,
                    planning_status=planning_status,
                    share_setting=share_setting,
                    shared_projects=list(shared_projects),
                    vm_family=vm_family,
                    auto_delete=auto_delete,
                    auto_created_reservations_delete_time=auto_del_time,
                    instance_label_filter=instance_label_filter,
                )
            )

    if not planned:
        raise ValueError("no reservation items after filter")
    return planned


# ---------------------------------------------------------------------------
# gcloud subprocess wrapper
# ---------------------------------------------------------------------------


def check_deps() -> None:
    if shutil.which("gcloud") is None:
        raise RuntimeError("gcloud CLI not found in PATH; install it and run `gcloud auth login`")


# Retryable transient error signatures (network / proxy / gcloud crash)
_RETRYABLE_STDERR_PATTERNS = (
    "ProxyError",
    "RemoteDisconnected",
    "Connection reset",
    "Connection aborted",
    "Max retries exceeded",
    "EOF occurred in violation of protocol",
    "ConnectionResetError",
    "Read timed out",
    "TemporaryFailure",
    "Temporary failure",
    "503",
    "502",
    "500 Internal",
    "Backend Error",
    "gcloud crashed",
    "Please try again",
)


def _is_retryable(stderr: str) -> bool:
    if not stderr:
        return False
    return any(p in stderr for p in _RETRYABLE_STDERR_PATTERNS)


def run_gcloud(
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    retries: int = 3,
    initial_backoff: float = 2.0,
) -> subprocess.CompletedProcess:
    """Run gcloud with exponential backoff on transient proxy/network errors."""
    logger.debug("exec: %s", " ".join(shlex.quote(a) for a in args))
    last_exc: Optional[subprocess.CalledProcessError] = None
    backoff = initial_backoff
    for attempt in range(1, retries + 1):
        try:
            return subprocess.run(
                args,
                check=check,
                capture_output=capture,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            last_exc = e
            stderr = e.stderr or ""
            if attempt < retries and _is_retryable(stderr):
                logger.warning(
                    "gcloud transient error (attempt %d/%d): %s; retrying in %.1fs",
                    attempt, retries, stderr.splitlines()[0] if stderr else e, backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
    assert last_exc is not None  # pragma: no cover
    raise last_exc


# ---------------------------------------------------------------------------
# Conflict detection: existing future reservations
# ---------------------------------------------------------------------------

# procurementStatus values still "occupying" a window; others are inactive (CANCELLED/DECLINED/FAILED, etc.)
ACTIVE_PROCUREMENT_STATUSES = {
    "DRAFTING",
    "PENDING_APPROVAL",
    "PENDING_AMENDMENT_APPROVAL",
    "APPROVED",
    "COMMITTED",
    "PROCURING",
    "PROVISIONING",
    "FULFILLED",
    "",      # some API versions may leave this empty
    None,    # type: ignore[list-item]
}


@dataclass
class ExistingFR:
    name: str
    zone: str              # short zone, e.g. asia-northeast1-a
    machine_type: str      # short name, e.g. n2-custom-64-131072
    start: datetime
    end: datetime
    status: str


def _tail(url_or_name: Optional[str]) -> str:
    if not url_or_name:
        return ""
    return url_or_name.rsplit("/", 1)[-1]


def _parse_rfc3339(value: str) -> datetime:
    if not value:
        raise ValueError("empty time string")
    # gcloud usually returns "2026-05-01T00:00:00Z" or with +00:00
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def list_existing_future_reservations(project: str) -> list[ExistingFR]:
    """Fetch all future reservations in the project once."""
    cmd = [
        "gcloud", "compute", "future-reservations", "list",
        f"--project={project}",
        "--format=json",
    ]
    proc = run_gcloud(cmd)
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"failed to parse future-reservations list: {e}") from e

    out: list[ExistingFR] = []
    for it in items:
        name = it.get("name") or ""
        zone = _tail(it.get("zone"))
        sku = (it.get("specificSkuProperties") or {})
        inst_props = (sku.get("instanceProperties") or {})
        machine_type = _tail(inst_props.get("machineType"))
        time_window = it.get("timeWindow") or {}
        start_s = time_window.get("startTime") or ""
        end_s = time_window.get("endTime") or ""
        status = (((it.get("status") or {}).get("procurementStatus")) or "")
        if not (zone and machine_type and start_s and end_s):
            logger.debug("skipping FR %s (missing fields)", name)
            continue
        try:
            start_dt = _parse_rfc3339(start_s)
            end_dt = _parse_rfc3339(end_s)
        except ValueError as e:
            logger.warning("FR %s has unparseable time window: %s", name, e)
            continue
        out.append(
            ExistingFR(
                name=name,
                zone=zone,
                machine_type=machine_type,
                start=start_dt,
                end=end_dt,
                status=status,
            )
        )
    logger.info("fetched %d existing future reservation(s) from project %s", len(out), project)
    return out


def find_conflict(
    item: PlannedItem,
    existing: list[ExistingFR],
) -> Optional[ExistingFR]:
    """Return the first conflicting existing FR, or None."""
    try:
        new_start = _parse_rfc3339(item.start_time)
        new_end = _parse_rfc3339(item.end_time)
    except ValueError as e:
        raise ValueError(f"{item.fr_name}: invalid time window: {e}") from e

    for ex in existing:
        if ex.zone != item.zone:
            continue
        if ex.machine_type != item.machine_type:
            continue
        if ex.status not in ACTIVE_PROCUREMENT_STATUSES:
            continue
        if ex.start < new_end and ex.end > new_start:
            return ex
    return None


# ---------------------------------------------------------------------------
# Inventory: running VMs
# ---------------------------------------------------------------------------


def count_running_vms(
    project: str,
    zone: str,
    machine_type: str,
    label_filter: Optional[str] = None,
) -> int:
    """Count VMs matching (zone, machine_type, RUNNING).

    Uses `--zones=ZONE` for the single-zone endpoint to avoid the default
    aggregated/instances API (which often times out or drops under an unstable proxy).
    """
    filt = f"machineType:({machine_type}) AND status=RUNNING"
    if label_filter:
        filt += f" AND labels.{label_filter}"
    cmd = [
        "gcloud", "compute", "instances", "list",
        f"--project={project}",
        f"--zones={zone}",
        f"--filter={filt}",
        "--format=value(name)",
    ]
    proc = run_gcloud(cmd)
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return len(lines)


# ---------------------------------------------------------------------------
# gcloud command assembly
# ---------------------------------------------------------------------------


def build_gcloud_cmd(item: PlannedItem) -> list[str]:
    cmd = [
        "gcloud", "compute", "future-reservations", "create", item.fr_name,
        f"--project={item.project}",
        f"--zone={item.zone}",
        f"--machine-type={item.machine_type}",
        f"--total-count={item.total}",
        f"--start-time={item.start_time}",
        f"--end-time={item.end_time}",
        f"--planning-status={item.planning_status}",
    ]
    if item.vm_family:
        cmd.append(f"--vm-family={item.vm_family}")
    if item.share_setting == "local":
        cmd.append("--share-setting=local")
    else:
        cmd.append("--share-setting=projects")
        cmd.append("--share-with=" + ",".join(item.shared_projects))
    if item.auto_delete:
        cmd.append("--auto-delete-auto-created-reservations")
    else:
        cmd.append("--no-auto-delete-auto-created-reservations")
    if item.auto_created_reservations_delete_time:
        cmd.append(
            f"--auto-created-reservations-delete-time={item.auto_created_reservations_delete_time}"
        )
    return cmd


def pretty_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in cmd)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch-create GCP future reservations from a YAML config."
    )
    p.add_argument("--config", default="reservations.yaml", help="YAML config path")
    p.add_argument("--dry-run", action="store_true", help="print gcloud commands without executing")
    p.add_argument("--filter", dest="filter_regex", help="only process reservation names matching this regex")
    p.add_argument("--project", help="override defaults.project")
    p.add_argument("--region", help="override defaults.region")
    p.add_argument("--log-dir", default="logs", help="directory for run logs")
    p.add_argument(
        "--name-suffix",
        default=None,
        help=(
            "append this suffix to every reservation name; "
            "use 'auto' (default behavior) to attach the run timestamp YYYYMMDD-HHMMSS, "
            "or 'none' to disable. Example: --name-suffix=20260113-091320"
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def resolve_name_suffix(cli_value: Optional[str]) -> Optional[str]:
    """Return the suffix to append to reservation names, or None to omit."""
    if cli_value is None or cli_value.lower() == "auto":
        return datetime.now().strftime("%Y%m%d-%H%M%S")
    if cli_value.lower() in ("none", "off", "no", ""):
        return None
    return cli_value


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    log_dir = Path(args.log_dir)
    log_path = setup_logging(args.verbose, log_dir)
    logger.info("log file: %s", log_path)

    name_suffix = resolve_name_suffix(args.name_suffix)
    if name_suffix:
        logger.info("name suffix: %s", name_suffix)

    try:
        check_deps()
    except RuntimeError as e:
        logger.error("%s", e)
        return 2

    try:
        items = load_config(
            Path(args.config),
            cli_project=args.project,
            cli_region=args.region,
            filter_regex=args.filter_regex,
            name_suffix=name_suffix,
        )
    except (FileNotFoundError, ValueError) as e:
        logger.error("config error: %s", e)
        return 2

    project = items[0].project
    logger.info("project=%s region=%s filter=%s dry_run=%s",
                project, items[0].region, args.filter_regex, args.dry_run)
    logger.info("planned items: %d", len(items))

    try:
        existing = list_existing_future_reservations(project)
    except subprocess.CalledProcessError as e:
        logger.error(
            "failed to list existing future reservations: %s\nstderr: %s",
            e, (e.stderr or "").strip(),
        )
        return 2

    ok: list[PlannedItem] = []
    failed: list[tuple[PlannedItem, str]] = []
    skipped: list[tuple[PlannedItem, ExistingFR]] = []
    dryrun_cmds: list[str] = []

    for item in items:
        conflict = find_conflict(item, existing)
        if conflict:
            logger.warning(
                "[SKIP] %s: conflicts with existing FR %r\n"
                "       zone=%s machine_type=%s\n"
                "       existing window = [%s, %s] status=%s\n"
                "       new window      = [%s, %s]",
                item.fr_name, conflict.name,
                item.zone, item.machine_type,
                conflict.start.isoformat(), conflict.end.isoformat(), conflict.status,
                item.start_time, item.end_time,
            )
            skipped.append((item, conflict))
            continue

        # Inventory query
        try:
            inv = count_running_vms(
                item.project,
                item.zone,
                item.machine_type,
                item.instance_label_filter,
            )
        except subprocess.CalledProcessError as e:
            msg = f"inventory query failed: {e}; stderr={(e.stderr or '').strip()}"
            logger.error("[FAIL] %s: %s", item.fr_name, msg)
            failed.append((item, msg))
            continue

        item.inventory = inv
        item.total = item.demand + inv

        logger.info(
            "[PLAN] %s  zone=%s  machine=%s  demand=%d  inventory(running_vms)=%d  total=%d",
            item.fr_name, item.zone, item.machine_type,
            item.demand, item.inventory, item.total,
        )

        if item.total <= 0:
            msg = f"total_count={item.total} (<=0) after adding inventory; skipping"
            logger.warning("[SKIP] %s: %s", item.fr_name, msg)
            failed.append((item, msg))
            continue

        cmd = build_gcloud_cmd(item)
        cmd_str = pretty_cmd(cmd)
        logger.info("[CMD ] %s", cmd_str)

        if args.dry_run:
            dryrun_cmds.append(cmd_str)
            ok.append(item)
            continue

        try:
            run_gcloud(cmd, check=True)
            logger.info("[OK  ] %s created", item.fr_name)
            ok.append(item)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            logger.error("[FAIL] %s: %s", item.fr_name, stderr or e)
            failed.append((item, stderr or str(e)))

    # ---- summary ----
    logger.info("=== SUMMARY ===")
    logger.info(
        "planned: %d  ok: %d  skipped(conflict): %d  failed: %d",
        len(items), len(ok), len(skipped), len(failed),
    )
    if skipped:
        logger.info("skipped items:")
        for it, ex in skipped:
            logger.info("  - %s (conflicts with existing FR: %s)", it.fr_name, ex.name)
    if failed:
        logger.info("failed items:")
        for it, msg in failed:
            logger.info("  - %s: %s", it.fr_name, msg.splitlines()[0] if msg else "")
        failed_path = log_dir / f"failed-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        failed_path.write_text(
            "\n".join(it.fr_name for it, _ in failed) + "\n",
            encoding="utf-8",
        )
        logger.info("failed list written to %s (retry via --filter)", failed_path)

    if args.dry_run and dryrun_cmds:
        dryrun_path = log_dir / f"dryrun-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sh"
        dryrun_path.write_text("#!/usr/bin/env bash\nset -e\n\n" + "\n\n".join(dryrun_cmds) + "\n", encoding="utf-8")
        os.chmod(dryrun_path, 0o755)
        logger.info("dry-run commands written to %s", dryrun_path)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
