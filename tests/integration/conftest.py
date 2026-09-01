# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Shared fixtures for headscale-k8s-operator integration tests."""

from __future__ import annotations

import pathlib
from typing import Callable

import pytest

from tests.integration.helpers import build_charm_at_version, build_rock_at_version


@pytest.fixture(scope="session")
def built_charm(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], pathlib.Path]:
    """Return a callable that builds (and memoizes) the charm at a given version."""
    cache: dict[str, pathlib.Path] = {}

    def _get(version: str) -> pathlib.Path:
        if version not in cache:
            cache[version] = build_charm_at_version(version, tmp_path_factory)
        return cache[version]

    return _get


@pytest.fixture(scope="session")
def built_rock(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], str]:
    """Return a callable that builds and pushes (and memoizes) the rock at a given version."""
    cache: dict[str, str] = {}

    def _get(version: str) -> str:
        if version not in cache:
            cache[version] = build_rock_at_version(version, tmp_path_factory)
        return cache[version]

    return _get
