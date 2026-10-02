"""Testler gizli profilden (config/profile.yaml) bağımsız çalışır: sadece herkese açık ayarlar kullanılır."""
import os

import pytest

os.environ["BULTEN_NO_PROFILE"] = "1"


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch):
    from bulten.config import load_profile

    monkeypatch.setenv("BULTEN_NO_PROFILE", "1")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    load_profile.cache_clear()
    yield
    load_profile.cache_clear()
