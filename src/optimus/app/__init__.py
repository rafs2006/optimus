"""Process-composition layer.

This package wires Optimus into a running process. The detection core and the
individual service logic live elsewhere and are unchanged; everything here is
about *composition*: which transport, which datastores, and how the services are
assembled. :mod:`optimus.app.simple` composes all six services into one asyncio
process over the in-process bus (``OPTIMUS_MODE=simple``); distributed mode keeps
using the per-service entrypoints under ``optimus.services.*``.
"""

from __future__ import annotations
