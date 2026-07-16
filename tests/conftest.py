from __future__ import annotations

import sys

import pytest

if sys.version_info >= (3, 14, 2):
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def _enable_custom_integrations(enable_custom_integrations):
        yield
