"""Tests for structured logging and correlation-id propagation."""

from __future__ import annotations

from optimus.core import logging as log


def test_correlation_context_sets_and_resets() -> None:
    assert log.get_correlation_id() is None
    with log.correlation_context("abc123") as cid:
        assert cid == "abc123"
        assert log.get_correlation_id() == "abc123"
    assert log.get_correlation_id() is None


def test_correlation_context_generates_id() -> None:
    with log.correlation_context() as cid:
        assert cid
        assert log.get_correlation_id() == cid


def test_set_correlation_id_returns_value() -> None:
    value = log.set_correlation_id("fixed")
    assert value == "fixed"
    assert log.get_correlation_id() == "fixed"
    log.set_correlation_id(None)  # generates a fresh id
    assert log.get_correlation_id() is not None


def test_inject_correlation_id_processor() -> None:
    with log.correlation_context("xyz"):
        out = log._inject_correlation_id(None, "info", {"message": "hi"})
    assert out["correlation_id"] == "xyz"


def test_configure_logging_and_get_logger() -> None:
    log.configure_logging(level="INFO", service_name="optimus-test")
    logger = log.get_logger("test")
    logger.info("structured_event", key="value")
