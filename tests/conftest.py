"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np
import numpy.typing as npt
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from optimus.db.engine import create_engine, create_session_factory
from optimus.db.models import Base


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """An aiosqlite-backed async session with the full schema created."""
    engine = create_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def gradient_image() -> npt.NDArray[np.uint8]:
    """A deterministic 200x200 RGB gradient image."""
    rng = np.random.default_rng(1234)
    base = np.linspace(0, 255, 200, dtype=np.float64)
    img = np.zeros((200, 200, 3), dtype=np.float64)
    img[:, :, 0] = base[None, :]
    img[:, :, 1] = base[:, None]
    img[:, :, 2] = (base[None, :] + base[:, None]) / 2
    img += rng.normal(0, 3, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)
