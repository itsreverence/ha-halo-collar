from __future__ import annotations

from importlib.util import find_spec

import pytest

if find_spec("pytest_homeassistant_custom_component") is not None:
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def _enable_custom_integrations(enable_custom_integrations):
        yield
