# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Shared helpers for headscale-k8s-operator unit tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

import ops.testing as testing

from charm import HeadscaleCharm
from headscale import Headscale

BASE_VERSION = "0.26.1"


def base_container() -> testing.Container:
    """Return a connected workload container with a minimal Pebble plan."""
    return testing.Container(
        "headscale",
        can_connect=True,
        _base_plan={
            "services": {
                "headscale-server": {
                    "override": "replace",
                    "summary": "Start the headscale server",
                    "command": "/usr/bin/headscale serve",
                    "startup": "enabled",
                },
                "exporter": {
                    "override": "replace",
                    "summary": "Start the exporter",
                    "command": "headscale_exporter",
                    "startup": "enabled",
                    "requires": ["headscale-server"],
                },
            }
        },
        service_statuses={
            "headscale-server": testing.pebble.ServiceStatus.ACTIVE,
            "exporter": testing.pebble.ServiceStatus.ACTIVE,
        },
    )


def base_state(stored_version: str = BASE_VERSION) -> testing.State:
    """Return an initial state for a freshly installed unit."""
    return testing.State(
        leader=True,
        config={"derp-map-url": "https://example.com/derp.yaml"},
        containers={base_container()},
        relations={
            testing.PeerRelation(
                endpoint="headscale-peers",
                local_app_data={
                    "headscale_version": stored_version,
                    "upgrade_acknowledged": "",
                },
            )
        },
    )


def deploy_and_activate(ctx: testing.Context[HeadscaleCharm]) -> testing.State:
    """Install the charm and bring the workload to ActiveStatus.

    Simulates a freshly deployed unit at the charm's bundled workload version.
    Patches ``upgrade.HEADSCALE_VERSION`` to ``BASE_VERSION`` for the duration
    of the install/pebble-ready sequence so this fixture stays a stable,
    decoupled "old version" baseline regardless of whatever the real
    production ``HEADSCALE_VERSION`` currently is -- tests build on top of
    this by patching ``upgrade.HEADSCALE_VERSION`` again to simulate an
    upgrade target.
    """
    state = base_state()
    with (
        mock.patch("upgrade.HEADSCALE_VERSION", BASE_VERSION),
        mock_version(BASE_VERSION),
        mock_backup_and_setup(),
        mock_wait_for_ready(),
    ):
        state = ctx.run(ctx.on.install(), state)
        state = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)
    assert state.unit_status == testing.ActiveStatus()
    return state


@contextmanager
def mock_version(version: str | None) -> Iterator[mock.MagicMock]:
    """Patch Headscale.get_version() for the duration of the context."""
    with mock.patch.object(Headscale, "get_version", return_value=version) as m:
        yield m


@contextmanager
def mock_backup_and_setup() -> Iterator[tuple[mock.MagicMock, mock.MagicMock]]:
    """Patch Headscale.create_backup and Headscale.setup to no-ops."""
    with (
        mock.patch.object(
            Headscale, "create_backup", return_value=Path("/tmp/fake")
        ) as create_backup,
        mock.patch.object(Headscale, "setup") as setup,
    ):
        yield create_backup, setup


@contextmanager
def mock_wait_for_ready() -> Iterator[mock.MagicMock]:
    """Patch HeadscaleCharm.wait_for_ready() so Pebble readiness passes."""
    with mock.patch.object(HeadscaleCharm, "wait_for_ready") as m:
        m.return_value = None
        yield m
