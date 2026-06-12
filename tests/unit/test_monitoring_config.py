"""Validate the optional monitoring stack config (Prometheus + Grafana).

These are pure-config artifacts under ``monitoring/`` wired into the
``monitoring`` docker-compose profile. The tests guard them against drift: the
Prometheus scrape config must cover every service, the alert rules must define
the conditions we promise operators, and — most importantly — every Prometheus
metric the Grafana dashboard and alert rules reference must actually exist in
the source tree, so a renamed metric can never silently break a panel or alert.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING = REPO_ROOT / "monitoring"
SRC = REPO_ROOT / "src"

SERVICES = ("gateway", "ingest", "detection", "moderation", "interactions", "scheduler")


def _all_source_text() -> str:
    return "\n".join(p.read_text() for p in SRC.rglob("*.py"))


def _metric_names(text: str) -> set[str]:
    """Every ``optimus_*`` metric base name appearing in a blob of text."""
    return set(re.findall(r"optimus_[a-z0-9_]+", text))


def test_prometheus_config_parses_and_scrapes_all_services() -> None:
    cfg = yaml.safe_load((MONITORING / "prometheus.yml").read_text())
    targets = {
        t.split(":")[0]
        for job in cfg["scrape_configs"]
        for sc in job.get("static_configs", [])
        for t in sc["targets"]
    }
    for svc in SERVICES:
        assert svc in targets, f"prometheus scrape config missing {svc}"
    # All six services share port 8080 (the aiohttp health/metrics server).
    optimus_job = next(j for j in cfg["scrape_configs"] if j["job_name"] == "optimus")
    for sc in optimus_job["static_configs"]:
        for target in sc["targets"]:
            assert target.endswith(":8080"), target
    assert cfg["scrape_configs"]  # non-empty
    assert "alerts.yml" in cfg["rule_files"][0]


def test_alert_rules_parse_and_cover_expected_conditions() -> None:
    rules_doc = yaml.safe_load((MONITORING / "alerts.yml").read_text())
    alert_names = {
        rule["alert"] for group in rules_doc["groups"] for rule in group["rules"] if "alert" in rule
    }
    expected = {
        "OptimusServiceDown",
        "OptimusConsumerStalled",
        "OptimusModerationCircuitOpen",
        "OptimusModerationQueueDepthHigh",
        "OptimusRatelimitRedisFallback",
        "OptimusBusMessagesDropped",
    }
    missing = expected - alert_names
    assert not missing, f"alert rules missing: {missing}"
    # Service-down keys off up==0; sanity-check the expr survived edits.
    service_down = next(
        rule
        for group in rules_doc["groups"]
        for rule in group["rules"]
        if rule.get("alert") == "OptimusServiceDown"
    )
    assert "up" in service_down["expr"] and "== 0" in service_down["expr"]


def test_grafana_dashboard_is_valid_json_with_expected_panels() -> None:
    dash = json.loads((MONITORING / "grafana/dashboards/optimus-overview.json").read_text())
    assert dash["uid"] == "optimus-overview"
    panel_titles = {p["title"] for p in dash["panels"] if p["type"] != "row"}
    expected_panels = {
        "Pipeline throughput (msgs/s)",
        "Detection in-flight vs max",
        "p95 dispatch latency",
        "Per-priority queue depth",
        "Circuit breaker states",
        "Ratelimit Redis fallbacks",
        "Reject / drop counters",
        "Retention purges (rows affected)",
    }
    assert expected_panels <= panel_titles, expected_panels - panel_titles


def test_dashboard_and_alert_metrics_exist_in_source() -> None:
    """Every optimus_* metric referenced in dashboard/alerts is defined in src.

    Histogram queries reference ``_bucket``/``_sum``/``_count`` suffixes that
    prometheus_client derives from the base metric, so strip those before
    checking against the names defined in source.
    """
    source_metrics = _metric_names(_all_source_text())
    dash_text = (MONITORING / "grafana/dashboards/optimus-overview.json").read_text()
    alert_text = (MONITORING / "alerts.yml").read_text()

    referenced = _metric_names(dash_text) | _metric_names(alert_text)
    suffixes = ("_bucket", "_sum", "_count")
    missing = set()
    for name in referenced:
        base = name
        for suffix in suffixes:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base not in source_metrics:
            missing.add(name)
    assert not missing, f"dashboard/alerts reference unknown metrics: {missing}"


def test_grafana_datasource_provisioned() -> None:
    ds = yaml.safe_load(
        (MONITORING / "grafana/provisioning/datasources/prometheus.yml").read_text()
    )
    datasource = ds["datasources"][0]
    assert datasource["type"] == "prometheus"
    assert datasource["uid"] == "optimus-prometheus"
    # The dashboard pins this datasource uid; they must agree.
    dash_text = (MONITORING / "grafana/dashboards/optimus-overview.json").read_text()
    assert "optimus-prometheus" in dash_text


def test_compose_monitoring_is_opt_in_profile() -> None:
    """prometheus + grafana must sit behind the monitoring profile so a plain
    ``docker compose up`` does not start them and the default stack is unchanged.
    """
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    for svc in ("prometheus", "grafana"):
        assert svc in compose["services"], f"{svc} missing from compose"
        assert compose["services"][svc].get("profiles") == ["monitoring"], (
            f"{svc} must be gated behind the monitoring profile"
        )
    # The six app services must NOT carry a profile (always start by default).
    for svc in SERVICES:
        assert "profiles" not in compose["services"][svc], (
            f"{svc} should start by default, not behind a profile"
        )
