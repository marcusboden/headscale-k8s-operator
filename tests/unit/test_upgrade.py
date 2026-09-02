# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Unit tests for charm upgrade logic using the ops.testing Scenario API."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest import mock

import ops.testing as testing
import pytest

from charm import HeadscaleCharm
from headscale import Headscale
from tests.unit.helpers import (
    BASE_VERSION,
    deploy_and_activate,
    mock_backup_and_setup,
    mock_version,
    mock_wait_for_ready,
)
from upgrade import UpgradeStep


def _state_with_version(state: testing.State, version: str) -> testing.State:
    """Return ``state`` with the charm's stored workload version set to ``version``."""
    kept = {rel for rel in state.relations if rel.endpoint != "headscale-peers"}
    kept.add(
        testing.PeerRelation(
            endpoint="headscale-peers",
            local_app_data={
                "headscale_version": version,
                "upgrade_acknowledged": "",
            },
        )
    )
    return dataclasses.replace(state, relations=frozenset(kept))


def _get_stored_version(state: testing.State) -> str:
    """Return the headscale_version stored on the state."""
    for rel in state.relations:
        if rel.endpoint == "headscale-peers":
            return rel.local_app_data["headscale_version"]
    raise KeyError("headscale-peers relation not found")


def _run_proceed_action(ctx, state):
    """Run proceed-upgrade and return the resulting state and action results.

    Shared by test classes that don't have their own ``_run_action`` helper
    (e.g. ``TestUpgradeScenarios``, unlike ``TestProceedUpgradeAction``).
    """
    out = ctx.run(ctx.on.action("proceed-upgrade"), state)
    return out, ctx.action_results


class TestOnUpgradeCharm:
    """Tests for _on_upgrade_charm preliminary version checks."""

    def test_version_unchanged_noop(self, ctx):
        """If HEADSCALE_VERSION hasn't changed, the handler is a no-op."""
        state = deploy_and_activate(ctx)

        with mock.patch("upgrade.HEADSCALE_VERSION", BASE_VERSION):
            out = ctx.run(ctx.on.upgrade_charm(), state)

        assert out.unit_status == testing.ActiveStatus()

    def test_downgrade_blocked(self, ctx):
        """A downgrade should set BlockedStatus immediately."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with mock.patch("upgrade.HEADSCALE_VERSION", "0.25.1"):
            out = ctx.run(ctx.on.upgrade_charm(), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Downgrade" in out.unit_status.message

    def test_major_bump_blocked(self, ctx):
        """A major version bump should set BlockedStatus."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with mock.patch("upgrade.HEADSCALE_VERSION", "1.0.0"):
            out = ctx.run(ctx.on.upgrade_charm(), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Major version bump" in out.unit_status.message

    def test_micro_upgrade_allowed(self, ctx):
        """A micro/patch upgrade is allowed and does not persist yet."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"):
            out = ctx.run(ctx.on.upgrade_charm(), state)

        assert not isinstance(out.unit_status, testing.BlockedStatus)
        assert _get_stored_version(out) == "0.26.1"

    def test_manual_gate_blocked(self, ctx):
        """If UPGRADE_PATH has a manual_note, upgrade-charm should block."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(manual_note="Do operator things first")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
        ):
            out = ctx.run(ctx.on.upgrade_charm(), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Manual steps needed" in out.unit_status.message

    def test_conditional_check_gate_blocked(self, ctx):
        """A `check` callable returning a message should also block upgrade-charm."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(check=lambda h: "wildcard SSH destination found")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
        ):
            out = ctx.run(ctx.on.upgrade_charm(), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Manual steps needed" in out.unit_status.message

    def test_conditional_check_gate_not_triggered(self, ctx):
        """A `check` callable returning None should not block upgrade-charm."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(check=lambda h: None)}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
        ):
            out = ctx.run(ctx.on.upgrade_charm(), state)

        assert not isinstance(out.unit_status, testing.BlockedStatus)


class TestOnPebbleReady:
    """Tests for _on_pebble_ready version handling branches."""

    def test_version_read_failure_starts_no_check(self, ctx):
        """When get_version returns None the charm starts without version checks."""
        state = testing.State(
            leader=True,
            config={"derp-map-url": "https://example.com/derp.yaml"},
            containers={testing.Container("headscale", can_connect=True)},
        )
        with (
            mock_version(None),
            mock_backup_and_setup(),
            mock_wait_for_ready(),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert out.unit_status == testing.ActiveStatus()

    def test_wrong_image_blocked(self, ctx):
        """A running version different from HEADSCALE_VERSION is a wrong image."""
        state = deploy_and_activate(ctx)

        with mock_version("0.25.1"):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Wrong headscale image" in out.unit_status.message
        assert "0.25.1" in out.unit_status.message

    def test_version_unchanged_starts(self, ctx):
        """When running and stored versions equal HEADSCALE_VERSION, just start."""
        state = deploy_and_activate(ctx)

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", BASE_VERSION),
            mock_version(BASE_VERSION),
            mock_backup_and_setup(),
            mock_wait_for_ready(),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert out.unit_status == testing.ActiveStatus()

    def test_micro_upgrade_runs(self, ctx):
        """A newer running micro version triggers backup, migration and version update."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"),
            mock_version("0.26.2"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, setup):
                out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert out.unit_status == testing.ActiveStatus()
        assert create_backup.called
        assert setup.called
        assert _get_stored_version(out) == "0.26.2"

    def test_minor_upgrade_no_gate_runs(self, ctx):
        """A newer minor version without a gate triggers the upgrade sequence."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock_version("0.27.0"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, setup):
                out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert out.unit_status == testing.ActiveStatus()
        assert create_backup.called
        assert setup.called
        assert _get_stored_version(out) == "0.27.0"

    def test_minor_upgrade_skips_intermediate_patches(self, ctx):
        """Patch releases don't need to be applied sequentially, only minor/major do.

        Per headscale's own upgrade policy, 0.28.0 -> 0.29.3 is a single
        minor-version step (28 -> 29) and must not be blocked, even though
        it skips 0.28.1, 0.28.2, 0.29.0, 0.29.1, and 0.29.2 entirely.
        """
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.28.0")

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.29.3"),
            mock_version("0.29.3"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, setup):
                out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert out.unit_status == testing.ActiveStatus()
        assert create_backup.called
        assert setup.called
        assert _get_stored_version(out) == "0.29.3"

    def test_newer_version_manual_gate_unacknowledged(self, ctx):
        """A manual gate without acknowledgement blocks pebble-ready."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(manual_note="Do operator things first")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.0"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, _):
                out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Manual steps required" in out.unit_status.message
        assert not create_backup.called

    def test_conditional_check_gate_blocked(self, ctx):
        """A `check` callable returning a message blocks pebble-ready like manual_note."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(check=lambda h: "wildcard SSH destination found")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.0"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, _):
                out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Manual steps required" in out.unit_status.message
        assert "wildcard SSH destination found" in out.unit_status.message
        assert not create_backup.called

    def test_conditional_check_gate_not_triggered_runs(self, ctx):
        """A `check` callable returning None lets the upgrade proceed automatically."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(check=lambda h: None)}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.0"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, setup):
                out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert out.unit_status == testing.ActiveStatus()
        assert create_backup.called
        assert setup.called
        assert _get_stored_version(out) == "0.27.0"

    def test_real_0_28_gate_blocks_on_wildcard_policy(self, ctx):
        """Exercise the real production 0.28 gate wiring, not just the mechanism.

        The real UPGRADE_PATH['0.28'] entry should only block if the policy
        has a wildcard SSH destination.
        """
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.27.1")

        with (
            mock_version("0.28.0"),
            mock.patch.object(Headscale, "check_ssh_wildcard_policy", return_value="found it"),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "found it" in out.unit_status.message

    def test_real_0_28_gate_allows_when_no_wildcard(self, ctx):
        """The real 0.28 gate doesn't block deployments without an affected policy."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.27.1")

        with (
            mock_version("0.28.0"),
            mock.patch.object(Headscale, "check_ssh_wildcard_policy", return_value=None),
            mock_backup_and_setup(),
            mock_wait_for_ready(),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert out.unit_status == testing.ActiveStatus()
        assert _get_stored_version(out) == "0.28.0"

    def test_blocked_version_jump_at_pebble_ready(self, ctx):
        """A version mismatch detected at pebble-ready blocks."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.26.1"),
            mock_version("0.25.1"),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Wrong headscale image" in out.unit_status.message

    def test_render_config_runtime_error(self, ctx):
        """A RuntimeError from render_config returns early with MaintenanceStatus."""
        state = testing.State(
            leader=True,
            config={"derp-map-url": "https://example.com/derp.yaml"},
            containers={testing.Container("headscale", can_connect=True)},
        )

        with mock.patch.object(Headscale, "render_config", side_effect=RuntimeError("boom")):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.MaintenanceStatus)
        assert "Failed to write config" in out.unit_status.message


class TestRunVersionUpgrade:
    """Tests for the _run_version_upgrade helper."""

    def test_backup_failure_blocked(self, ctx):
        """A failed backup should block and leave the stored version unchanged."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        create_backup = mock.MagicMock(side_effect=RuntimeError("backup failed"))

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"),
            mock_version("0.26.2"),
            mock.patch.object(Headscale, "create_backup", create_backup),
            mock.patch.object(Headscale, "setup"),
            mock_wait_for_ready(),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Pre-upgrade backup failed" in out.unit_status.message
        assert _get_stored_version(out) == "0.26.1"
        assert create_backup.called

    def test_migration_failure_blocked(self, ctx):
        """A failed migration should block and leave the stored version unchanged."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        migration = mock.MagicMock(side_effect=RuntimeError("migration failed"))
        path = {"0.26": UpgradeStep(migration=migration)}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.26.2"),
            mock_backup_and_setup(),
            mock_wait_for_ready(),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Migration" in out.unit_status.message
        assert _get_stored_version(out) == "0.26.1"

    def test_wait_for_ready_timeout_blocked(self, ctx):
        """A timeout waiting for the workload should block."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        wait_for_ready = mock.MagicMock(side_effect=RuntimeError("timeout"))

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"),
            mock_version("0.26.2"),
            mock_backup_and_setup(),
            mock.patch.object(HeadscaleCharm, "wait_for_ready", wait_for_ready),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Workload not ready" in out.unit_status.message
        assert _get_stored_version(out) == "0.26.1"
        assert wait_for_ready.called

    def test_setup_failure_blocked(self, ctx):
        """A failure in setup should block."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        setup = mock.MagicMock(side_effect=RuntimeError("setup failed"))

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"),
            mock_version("0.26.2"),
            mock.patch.object(Headscale, "create_backup", return_value=Path("/tmp/fake")),
            mock.patch.object(Headscale, "setup", setup),
            mock_wait_for_ready(),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Workload setup failed" in out.unit_status.message
        assert _get_stored_version(out) == "0.26.1"
        assert setup.called


class TestProceedUpgradeAction:
    """Tests for the proceed-upgrade action."""

    def _run_action(self, ctx, state):
        """Run proceed-upgrade and return the final state and results.

        Scenario returns action results via ``ctx.action_results``; a failed action
        raises ``testing.ActionFailed``.
        """
        out = ctx.run(ctx.on.action("proceed-upgrade"), state)
        return out, ctx.action_results

    def test_container_not_connectable(self, ctx):
        """Failure when the workload container cannot be connected."""
        state = testing.State(
            leader=True,
            config={"derp-map-url": "https://example.com/derp.yaml"},
            containers={testing.Container("headscale", can_connect=False)},
            relations={
                testing.PeerRelation(
                    endpoint="headscale-peers",
                    local_app_data={
                        "headscale_version": "0.26.1",
                        "upgrade_acknowledged": "",
                    },
                )
            },
        )

        with mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"):
            with pytest.raises(testing.ActionFailed) as exc:
                self._run_action(ctx, state)

        assert "container is not ready" in exc.value.message.lower()

    def test_no_upgrade_in_progress(self, ctx):
        """Failure when stored version already matches charm version."""
        state = deploy_and_activate(ctx)

        with mock.patch("upgrade.HEADSCALE_VERSION", BASE_VERSION):
            with pytest.raises(testing.ActionFailed) as exc:
                self._run_action(ctx, state)

        assert "No upgrade in progress" in exc.value.message

    def test_no_manual_gate(self, ctx):
        """Failure when the target version has no manual gate."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"):
            with pytest.raises(testing.ActionFailed) as exc:
                self._run_action(ctx, state)

        assert "No manual steps required" in exc.value.message

    def test_unreadable_version_fails(self, ctx):
        """Failure when the binary version cannot be read."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(manual_note="Do operator things first")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version(None),
        ):
            with pytest.raises(testing.ActionFailed) as exc:
                self._run_action(ctx, state)

        assert "Could not read headscale version" in exc.value.message

    def test_wrong_image_version(self, ctx):
        """Failure when the container reports a different version than expected."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(manual_note="Do operator things first")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.1"),
        ):
            with pytest.raises(testing.ActionFailed) as exc:
                self._run_action(ctx, state)

        assert "Attach the correct OCI resource" in exc.value.message

    def test_manual_gate_upgrade_fails(self, ctx):
        """Failure when the upgrade sequence fails after acknowledgement."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(manual_note="Do operator things first")}
        create_backup = mock.MagicMock(side_effect=RuntimeError("backup failed"))

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.0"),
            mock.patch.object(Headscale, "create_backup", create_backup),
        ):
            with pytest.raises(testing.ActionFailed) as exc:
                self._run_action(ctx, state)

        assert "Upgrade sequence failed" in exc.value.message
        assert _get_stored_version(exc.value.state) == "0.26.1"

    def test_manual_gate_upgrade_succeeds(self, ctx):
        """Successful manual-gate upgrade updates version and activates."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(manual_note="Do operator things first")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.0"),
            mock_backup_and_setup(),
            mock_wait_for_ready(),
        ):
            out_state, results = self._run_action(ctx, state)

        assert results["result"] == "Headscale upgraded from 0.26.1 to 0.27.0 successfully."
        assert out_state.unit_status == testing.ActiveStatus()
        assert _get_stored_version(out_state) == "0.27.0"

    def test_conditional_check_gate_upgrade_succeeds(self, ctx):
        """proceed-upgrade also works when the gate came from a `check` callable."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(check=lambda h: "wildcard SSH destination found")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.0"),
            mock_backup_and_setup(),
            mock_wait_for_ready(),
        ):
            out_state, results = self._run_action(ctx, state)

        assert results["result"] == "Headscale upgraded from 0.26.1 to 0.27.0 successfully."
        assert out_state.unit_status == testing.ActiveStatus()
        assert _get_stored_version(out_state) == "0.27.0"


class TestUpgradeScenarios:
    """End-to-end upgrade scenario tests."""

    def test_successful_minor_upgrade(self, ctx):
        """Full event sequence for a normal minor upgrade."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock_version("0.27.0"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, setup):
                state = ctx.run(ctx.on.upgrade_charm(), state)
                assert not isinstance(state.unit_status, testing.BlockedStatus)

                state = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert create_backup.called
        assert setup.called
        assert _get_stored_version(state) == "0.27.0"
        assert state.unit_status == testing.ActiveStatus()

    def test_bad_upgrade_wrong_image(self, ctx):
        """Attaching the wrong image should block without backup or setup."""
        state = deploy_and_activate(ctx)

        with mock_version("0.25.1"):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Wrong headscale image" in out.unit_status.message

    def test_skip_minor_blocked(self, ctx):
        """A skipped minor version keeps the unit blocked on upgrade_charm and pebble_ready."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.28.0"),
            mock_version("0.28.0"),
            mock_wait_for_ready(),
        ):
            with mock_backup_and_setup() as (create_backup, _):
                state = ctx.run(ctx.on.upgrade_charm(), state)
                assert isinstance(state.unit_status, testing.BlockedStatus)
                assert "intermediate version" in state.unit_status.message

                state = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)
                assert isinstance(state.unit_status, testing.BlockedStatus)
                assert "intermediate version" in state.unit_status.message

        assert not create_backup.called
        assert _get_stored_version(state) == "0.26.1"

    def test_manual_gate_upgrade_proceed(self, ctx):
        """A manual-gate upgrade sequence ending with proceed-upgrade."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        path = {"0.27": UpgradeStep(manual_note="Do operator things first")}

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.27.0"),
            mock.patch("upgrade.UPGRADE_PATH", path),
            mock_version("0.27.0"),
            mock_backup_and_setup(),
            mock_wait_for_ready(),
        ):
            state = ctx.run(ctx.on.upgrade_charm(), state)
            assert isinstance(state.unit_status, testing.BlockedStatus)
            assert "Manual steps needed" in state.unit_status.message

            state = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)
            assert isinstance(state.unit_status, testing.BlockedStatus)
            assert "Manual steps required" in state.unit_status.message

            out_state, results = _run_proceed_action(ctx, state)

        assert results["result"] == "Headscale upgraded from 0.26.1 to 0.27.0 successfully."
        assert _get_stored_version(out_state) == "0.27.0"
        assert out_state.unit_status == testing.ActiveStatus()

    def test_backup_fails_during_upgrade(self, ctx):
        """A backup failure during upgrade blocks and keeps the old version."""
        state = deploy_and_activate(ctx)
        state = _state_with_version(state, "0.26.1")
        create_backup = mock.MagicMock(side_effect=RuntimeError("backup failed"))

        with (
            mock.patch("upgrade.HEADSCALE_VERSION", "0.26.2"),
            mock_version("0.26.2"),
            mock.patch.object(Headscale, "create_backup", create_backup),
            mock.patch.object(Headscale, "setup"),
            mock_wait_for_ready(),
        ):
            out = ctx.run(ctx.on.pebble_ready(state.get_container("headscale")), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Pre-upgrade backup failed" in out.unit_status.message
        assert _get_stored_version(out) == "0.26.1"
        assert create_backup.called
