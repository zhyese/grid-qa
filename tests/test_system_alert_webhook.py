import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers import system


def test_alerts_webhook_fails_closed_when_token_unset(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_TOKEN", "")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(system.alerts_webhook(SimpleNamespace(), token="", db=object()))

    assert exc_info.value.status_code == 503
