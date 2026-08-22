"""Validate the distributed-mode docker-compose topology.

``docker-compose.yml`` is a pure-config artifact, so these tests guard it
against drift: the six app services must start by default, the NATS config the
bus depends on must actually exist at the path the compose file mounts, and no
bundled metrics collector may creep back in. Optimus exposes ``/metrics`` and
expects operators to bring their own scraper — a collector in this file would be
a service nobody runs (see ``docs/scaling.md`` §7).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"

SERVICES = ("gateway", "ingest", "detection", "moderation", "interactions", "scheduler")


def _compose() -> dict:
    parsed = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(parsed, dict)
    return parsed


def test_app_services_start_by_default() -> None:
    """None of the six services may hide behind a compose profile."""
    services = _compose()["services"]
    for svc in SERVICES:
        assert svc in services, f"{svc} missing from compose"
        assert "profiles" not in services[svc], (
            f"{svc} should start by default, not behind a profile"
        )


def test_no_bundled_metrics_collector() -> None:
    """No prometheus/grafana services: operators point their own scraper at /metrics."""
    compose = _compose()
    for name in ("prometheus", "grafana"):
        assert name not in compose["services"], (
            f"{name} is back in compose; optimus ships no collector — see docs/scaling.md"
        )
        assert f"{name}-data" not in (compose.get("volumes") or {}), (
            f"{name}-data volume left behind"
        )


def test_nats_config_mount_resolves_to_a_real_file() -> None:
    """The mounted nats.conf must exist, and must set the raised max_payload.

    nats-server rejects ``max_payload`` as a CLI flag, so it can only come from
    this file; a broken mount path would silently fall back to the 1 MiB default
    and drop inlined images.
    """
    nats = _compose()["services"]["nats"]
    mounts = [v for v in nats["volumes"] if isinstance(v, str) and "nats.conf" in v]
    assert len(mounts) == 1, f"expected exactly one nats.conf mount, got {mounts}"

    host_path = mounts[0].split(":")[0]
    resolved = (REPO_ROOT / host_path.lstrip("./")).resolve()
    assert resolved.is_file(), f"compose mounts {host_path}, which does not exist"
    assert "max_payload" in resolved.read_text()
