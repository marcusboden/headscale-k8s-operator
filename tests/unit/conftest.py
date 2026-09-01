# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Shared fixtures and helpers for headscale-k8s-operator unit tests."""

import ops.testing as testing
import pytest

from charm import HeadscaleCharm


@pytest.fixture
def ctx() -> testing.Context[HeadscaleCharm]:
    """Return a Scenario Context for HeadscaleCharm."""
    return testing.Context(HeadscaleCharm)
