# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Integration tests for the charm's real headscale version upgrade path.

These tests exercise the charm's upgrade logic end-to-end against a real Juju
model deployed on a Kubernetes cloud. They require:

- A Juju controller bootstrapped against a Kubernetes cloud.
- `rockcraft`, `charmcraft`, and `skopeo` available on PATH.
- The `HEADSCALE_ROCK_PATH` environment variable set to a local checkout of
  the `headscale-rock` repository.

They are slow, since each scenario compiles headscale from source (via
rockcraft) for one or more pinned upstream release tags.

Each test deploys its own, distinctly named application. This is required
because `pytest-jubilant`'s `juju` fixture is module-scoped (one Juju model
shared by every test function in this file); reusing a single app name across
tests would make the second and third `juju.deploy(...)` calls fail against
an application that already exists from a previous test.

Note on workload-version assertions after a blocked refresh: the charm calls
`self.unit.set_workload_version(...)` with the *new* container's reported
version unconditionally, before it decides whether the version jump is
blocked (see `HeadscaleCharm._on_pebble_ready` in src/charm.py, which
delegates the actual blocking decision to `Upgrader.handle_pebble_ready` in
src/upgrade.py). This is
because `juju refresh` swaps the OCI image immediately at the Kubernetes
level, so the container genuinely is running the new binary; the charm only
refuses to configure/activate it. So a blocked refresh does NOT leave Juju's
displayed workload version on the old value. To prove the block actually
protects the charm's persisted state (and isn't just a one-off status
message), each blocked-refresh test additionally performs the *correct* next
upgrade step afterwards and confirms it succeeds -- which is only possible if
the charm's internal stored version was never advanced to the blocked
target.
"""

from __future__ import annotations

import pathlib
from typing import Callable

import jubilant
import pytest

from tests.integration.helpers import DERP_MAP_URL_CONFIG

WAIT_TIMEOUT = 600

BuiltCharm = Callable[[str], pathlib.Path]
BuiltRock = Callable[[str], str]


def _wait_active(juju: jubilant.Juju, app: str) -> None:
    """Wait until `app` (and only `app`) is active.

    `jubilant.all_active`/`all_blocked` check *every* app in the model by
    default, and this test module's three tests share a single Juju model
    (see module docstring), so an unscoped predicate would also require
    unrelated apps from other tests to match.
    """
    juju.wait(lambda status: jubilant.all_active(status, app), timeout=WAIT_TIMEOUT)


def _wait_blocked(juju: jubilant.Juju, app: str) -> None:
    """Wait until `app` (and only `app`) is blocked. See `_wait_active`."""
    juju.wait(lambda status: jubilant.all_blocked(status, app), timeout=WAIT_TIMEOUT)


def _deploy(
    juju: jubilant.Juju,
    app: str,
    built_charm: BuiltCharm,
    built_rock: BuiltRock,
    version: str,
) -> None:
    """Deploy `app` at `version` and wait for it to become active."""
    juju.deploy(
        built_charm(version),
        app=app,
        resources={"headscale-image": built_rock(version)},
        config=DERP_MAP_URL_CONFIG,
    )
    _wait_active(juju, app)


def _refresh(
    juju: jubilant.Juju,
    app: str,
    built_charm: BuiltCharm,
    built_rock: BuiltRock,
    version: str,
) -> None:
    """Refresh `app` to `version`'s charm and OCI resource together."""
    juju.refresh(
        app,
        path=built_charm(version),
        resources={"headscale-image": built_rock(version)},
    )


def _workload_version(juju: jubilant.Juju, app: str) -> str:
    """Return the reported workload version for `app`."""
    return juju.status().apps[app].version


def _unit_status_message(juju: jubilant.Juju, app: str) -> str:
    """Return the workload status message for `app`/0."""
    status = juju.status()
    return status.apps[app].units[f"{app}/0"].workload_status.message


@pytest.mark.juju_setup
def test_sequential_upgrade_succeeds(
    juju: jubilant.Juju,
    built_charm: BuiltCharm,
    built_rock: BuiltRock,
) -> None:
    """Upgrading one minor version at a time (0.26.1 -> 0.27.0 -> 0.28.0) succeeds.

    This mirrors headscale's documented requirement to upgrade from one
    stable version to the next without skipping minor versions in between.
    """
    app = "headscale-sequential"
    _deploy(juju, app, built_charm, built_rock, "0.26.1")
    assert _workload_version(juju, app) == "0.26.1"

    _refresh(juju, app, built_charm, built_rock, "0.27.0")
    _wait_active(juju, app)
    assert _workload_version(juju, app) == "0.27.0"

    _refresh(juju, app, built_charm, built_rock, "0.28.0")
    _wait_active(juju, app)
    assert _workload_version(juju, app) == "0.28.0"


@pytest.mark.juju_setup
def test_downgrade_blocked(
    juju: jubilant.Juju,
    built_charm: BuiltCharm,
    built_rock: BuiltRock,
) -> None:
    """Refreshing to an older headscale version is blocked, and stored state is untouched.

    Headscale does not support downgrades; the charm's `_blocked_version_jump`
    check must refuse the refresh. To confirm the charm's internally stored
    version was not advanced to the rejected target (0.26.1), a subsequent
    *valid* forward refresh (0.27.0 -> 0.28.0) must still succeed -- if the
    stored version had incorrectly been left at 0.26.1, that refresh would
    itself be misclassified as a two-minor skip and blocked.
    """
    app = "headscale-downgrade"
    _deploy(juju, app, built_charm, built_rock, "0.27.0")
    assert _workload_version(juju, app) == "0.27.0"

    _refresh(juju, app, built_charm, built_rock, "0.26.1")
    _wait_blocked(juju, app)
    assert "downgrade" in _unit_status_message(juju, app).lower()

    _refresh(juju, app, built_charm, built_rock, "0.28.0")
    _wait_active(juju, app)
    assert _workload_version(juju, app) == "0.28.0"


@pytest.mark.juju_setup
def test_skip_minor_blocked(
    juju: jubilant.Juju,
    built_charm: BuiltCharm,
    built_rock: BuiltRock,
) -> None:
    """Refreshing directly across a minor version is blocked, and stored state is untouched.

    Headscale's upgrade docs require going through each intermediate minor
    version (e.g. 0.26.0 -> 0.27.1 -> 0.28.0) without skipping any. To confirm
    the charm's internally stored version was not advanced to the rejected
    target (0.28.0), a subsequent *valid* forward refresh (0.26.1 -> 0.27.0)
    must still succeed -- if the stored version had incorrectly been left at
    0.28.0, that refresh would itself be misclassified as a downgrade and
    blocked.
    """
    app = "headscale-skip-minor"
    _deploy(juju, app, built_charm, built_rock, "0.26.1")
    assert _workload_version(juju, app) == "0.26.1"

    _refresh(juju, app, built_charm, built_rock, "0.28.0")
    _wait_blocked(juju, app)
    assert "intermediate version" in _unit_status_message(juju, app).lower()

    _refresh(juju, app, built_charm, built_rock, "0.27.0")
    _wait_active(juju, app)
    assert _workload_version(juju, app) == "0.27.0"
