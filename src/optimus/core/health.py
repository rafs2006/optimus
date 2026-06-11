"""A tiny aiohttp health/metrics server exposing /healthz, /readyz, /metrics."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest

ReadinessCheck = Callable[[], Awaitable[bool]]


class HealthServer:
    """Serves liveness, readiness, and Prometheus metrics endpoints.

    Liveness (``/healthz``) reflects process health. Readiness (``/readyz``)
    runs registered async checks; any failure yields HTTP 503.
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",  # noqa: S104 - intended bind for containerized service
        port: int = 8080,
        registry: CollectorRegistry = REGISTRY,
    ) -> None:
        self._host = host
        self._port = port
        self._registry = registry
        self._readiness_checks: list[ReadinessCheck] = []
        self._live = True
        self._app = web.Application()
        self._app.add_routes(
            [
                web.get("/healthz", self._handle_healthz),
                web.get("/readyz", self._handle_readyz),
                web.get("/metrics", self._handle_metrics),
            ]
        )
        self._runner: web.AppRunner | None = None

    def add_readiness_check(self, check: ReadinessCheck) -> None:
        """Register an async readiness check returning ``True`` when ready."""
        self._readiness_checks.append(check)

    def set_live(self, live: bool) -> None:
        """Set process liveness (used to fail ``/healthz`` during shutdown)."""
        self._live = live

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        if self._live:
            return web.json_response({"status": "ok"})
        return web.json_response({"status": "shutting_down"}, status=503)

    async def _handle_readyz(self, _request: web.Request) -> web.Response:
        for check in self._readiness_checks:
            try:
                ok = await check()
            except Exception:
                ok = False
            if not ok:
                return web.json_response({"status": "not_ready"}, status=503)
        return web.json_response({"status": "ready"})

    async def _handle_metrics(self, _request: web.Request) -> web.Response:
        payload = generate_latest(self._registry)
        return web.Response(body=payload, content_type=CONTENT_TYPE_LATEST.split(";")[0])

    async def start(self) -> None:
        """Start serving in the current event loop."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()

    async def stop(self) -> None:
        """Stop serving and release resources."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
