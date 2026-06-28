"""Phase 0 smoke tests — proves imports + settings load."""

from __future__ import annotations

import os

import pytest

# Make sure required env vars exist for import-time settings load.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://woundiq:woundiq_dev_only@localhost:5432/woundiq"
)
os.environ.setdefault(
    "DATABASE_URL_SYNC", "postgresql://woundiq:woundiq_dev_only@localhost:5432/woundiq"
)


def test_config_loads() -> None:
    from api.config import get_settings

    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.org_id


def test_fastapi_app_constructs() -> None:
    from api.main import create_app

    app = create_app()
    assert app.title == "Wound IQ API"


def test_prefect_flow_imports() -> None:
    from pipeline.flows import hello_world_flow

    assert hello_world_flow.name == "hello-world"


@pytest.mark.parametrize("path", ["/healthz"])
def test_health_routes_registered(path: str) -> None:
    from api.main import create_app

    app = create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert path in paths
