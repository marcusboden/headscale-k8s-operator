# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Headscale version-upgrade tracking, gating, and orchestration.

This module owns everything related to deciding whether a headscale version
change is safe to apply, persisting the last-known-running version across
charm/pod restarts, and running the actual upgrade sequence (backup,
migration, restart, setup).
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Callable, Optional

import ops
from packaging.version import Version

from headscale import Headscale

if TYPE_CHECKING:
    from charm import HeadscaleCharm

logger = logging.getLogger(__name__)

HEADSCALE_VERSION = "0.29.3"


@dataclasses.dataclass
class UpgradeStep:
    """Describes what the charm must do when upgrading TO this minor version.

    manual_note: set this to unconditionally block pebble-ready until the
                 operator runs proceed-upgrade.
    check:       callable evaluated against the live Headscale wrapper (config
                 included) to *conditionally* decide whether a manual gate is
                 needed -- return a blocking message, or None if this
                 particular deployment isn't affected. Use this instead of
                 manual_note when the risk depends on the operator's own
                 configuration (e.g. their custom ACL policy content) rather
                 than being universally true for every deployment.
    migration:   callable run automatically (after backup) before starting the new binary.
    """

    manual_note: Optional[str] = None
    check: Optional[Callable[["Headscale"], Optional[str]]] = None
    migration: Optional[Callable[["Headscale"], None]] = None


def _check_0_28_ssh_policy(headscale: "Headscale") -> Optional[str]:
    """Only gate the 0.28 upgrade if this deployment's own policy is affected.

    Headscale 0.28.0 rejects ACL policies with a wildcard ('*') SSH
    destination at load time. The charm can see the configured policy (it's
    a plain charm config option), so check it directly instead of blanket-
    blocking every 0.28 upgrade regardless of whether the operator's policy
    actually uses that pattern.
    """
    return headscale.check_ssh_wildcard_policy()


# Keys are "<major>.<minor>" strings, e.g. "0.27".
# Each entry covers the step of upgrading TO that minor version.
UPGRADE_PATH: dict[str, UpgradeStep] = {
    # "0.27": UpgradeStep(migration=_migrate_0_27),
    "0.28": UpgradeStep(check=_check_0_28_ssh_policy),
}


class Upgrader(ops.Object):
    """Tracks the running headscale version and gates/executes upgrades.

    Persists state via the charm's peer relation application databag instead
    of ops.StoredState, because StoredState is backed by a local SQLite file
    inside the charm container's ephemeral filesystem. On Kubernetes, every
    `juju refresh` of this modern sidecar charm causes k8s to recreate the pod
    (and thus the charm container), wiping StoredState. Peer relation data is
    persisted by Juju in the controller's database and survives pod
    recreation.

    Subclasses `ops.Object` because `ops.Framework.observe()` requires the
    observer's bound-method owner to be an `Object`; `charm.py` still wires up
    the actual event observation explicitly (this class does not register its
    own observers).
    """

    def __init__(self, charm: "HeadscaleCharm") -> None:
        super().__init__(charm, "upgrade")
        self._charm = charm

    @property
    def _peer_relation(self) -> Optional[ops.Relation]:
        """Return the peer relation used to persist upgrade-tracking state."""
        return self._charm.model.get_relation("headscale-peers")

    def _get_stored_version(self) -> str:
        """Return the last-known-running headscale version, from peer relation data."""
        relation = self._peer_relation
        if relation is None:
            return HEADSCALE_VERSION
        return relation.data[self._charm.app].get("headscale_version", HEADSCALE_VERSION)

    def _set_stored_version(self, version: str) -> None:
        """Persist the running headscale version to peer relation app data."""
        if not self._charm.unit.is_leader():
            logger.warning("Not leader; skipping write of headscale_version to peer relation.")
            return
        relation = self._peer_relation
        if relation is None:
            logger.warning("Peer relation not available; cannot persist headscale_version.")
            return
        relation.data[self._charm.app]["headscale_version"] = version

    def _get_upgrade_acknowledged(self) -> str:
        """Return the version for which a manual upgrade gate was acknowledged."""
        relation = self._peer_relation
        if relation is None:
            return ""
        return relation.data[self._charm.app].get("upgrade_acknowledged", "")

    def _set_upgrade_acknowledged(self, value: str) -> None:
        """Persist the acknowledged-upgrade version to peer relation app data."""
        if not self._charm.unit.is_leader():
            logger.warning(
                "Not leader; skipping write of upgrade_acknowledged to peer relation."
            )
            return
        relation = self._peer_relation
        if relation is None:
            logger.warning("Peer relation not available; cannot persist upgrade_acknowledged.")
            return
        relation.data[self._charm.app]["upgrade_acknowledged"] = value

    def _blocked_version_jump(self, old_v: Version, new_v: Version) -> Optional[str]:
        """Return a BlockedStatus message if the version jump is not allowed, else None.

        Per headscale's own upgrade policy, major and minor releases must be
        applied one at a time, but patch releases don't need to be applied
        sequentially -- e.g. 0.28.0 -> 0.29.3 is fine, skipping 0.28.1,
        0.28.2, 0.29.0, 0.29.1, 0.29.2 entirely, since only the minor
        component (28 -> 29) advances by one step. Patch is intentionally
        never checked here; do not add patch-sequencing logic.
        """
        if new_v < old_v:
            return f"Downgrade {old_v} → {new_v} not supported. Restore previous charm."
        if new_v.major != old_v.major:
            return f"Major version bump {old_v} → {new_v} not supported automatically."
        if new_v.minor > old_v.minor + 1:
            return (
                f"Cannot upgrade {old_v} → {new_v} directly. "
                "Refresh to an intermediate version first."
            )
        return None

    def _manual_gate_reason(self, new_v: Version) -> Optional[str]:
        """Return a manual-gate message if the target version requires one, else None.

        Checks both an unconditional `manual_note` and a conditional `check`
        callable (evaluated against the live Headscale wrapper) on the
        matching UPGRADE_PATH entry, if any.
        """
        step = UPGRADE_PATH.get(f"{new_v.major}.{new_v.minor}")
        if not step:
            return None
        if step.manual_note:
            return step.manual_note
        if step.check:
            return step.check(self._charm.headscale)
        return None

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        old_v = Version(self._get_stored_version())
        new_v = Version(HEADSCALE_VERSION)

        if new_v == old_v:
            logger.info(f"Headscale version unchanged ({old_v}); no migration needed.")
            return

        reason = self._blocked_version_jump(old_v, new_v)
        if reason:
            logger.warning(f"Upgrade blocked: {reason}")
            self._charm.unit.status = ops.BlockedStatus(reason)
            return

        key = f"{new_v.major}.{new_v.minor}"
        step = UPGRADE_PATH.get(key)
        gate_reason = self._manual_gate_reason(new_v)
        if gate_reason:
            logger.warning(f"Manual steps required for v{key}: {gate_reason}")
            self._charm.unit.status = ops.BlockedStatus(
                f"Manual steps needed before v{key}. Check logs, then run proceed-upgrade."
            )
            return
        if step and step.migration:
            logger.info(f"Automated migration queued for v{key}; runs at pebble-ready.")

        logger.info(f"Headscale upgrade {old_v} → {new_v} will be applied at pebble-ready.")

    def _run_version_upgrade(self, old_v: Version, new_v: Version) -> bool:
        """Take a backup, run migrations, and start headscale for a version change.

        Returns True on success. On failure sets BlockedStatus and returns False.
        """
        charm = self._charm
        charm.unit.status = ops.MaintenanceStatus(f"Backing up before upgrade {old_v} → {new_v}")
        try:
            backup_path = charm.headscale.create_backup()
            logger.info(f"Pre-upgrade backup created at {backup_path}")
        except Exception as e:
            logger.error(f"Pre-upgrade backup failed: {e}")
            charm.unit.status = ops.BlockedStatus("Pre-upgrade backup failed. Check logs.")
            return False

        key = f"{new_v.major}.{new_v.minor}"
        step = UPGRADE_PATH.get(key)
        if step and step.migration:
            charm.unit.status = ops.MaintenanceStatus(f"Running migration for v{key}")
            try:
                step.migration(charm.headscale)
                logger.info(f"Migration for v{key} completed.")
            except Exception as e:
                logger.error(f"Migration for v{key} failed: {e}")
                charm.unit.status = ops.BlockedStatus(f"Migration for v{key} failed. Check logs.")
                return False

        if not charm._update_layer_and_restart(set_active=False):
            charm.unit.status = ops.BlockedStatus(
                "Could not start workload after upgrade. Check logs."
            )
            return False
        try:
            charm.wait_for_ready()
        except RuntimeError as e:
            logger.error(f"Workload did not become ready after upgrade: {e}")
            charm.unit.status = ops.BlockedStatus("Workload not ready after upgrade. Check logs.")
            return False
        try:
            charm.headscale.setup()
        except Exception as e:
            logger.error(f"Workload setup failed after upgrade: {e}")
            charm.unit.status = ops.BlockedStatus(
                "Workload setup failed after upgrade. Check logs."
            )
            return False
        self._set_stored_version(str(new_v))
        self._set_upgrade_acknowledged("")
        return True

    def handle_pebble_ready(self, running_v: Version) -> None:
        """Evaluate the running headscale version and act accordingly.

        Called by the charm's pebble-ready handler once it has confirmed the
        workload container reports a real version. Handles the "wrong image"
        check, deciding whether this is a no-op/fresh-install, blocking
        disallowed version jumps, gating on manual upgrade acknowledgement,
        and running the upgrade sequence.
        """
        charm = self._charm

        if running_v != Version(HEADSCALE_VERSION):
            logger.error(
                f"Version mismatch: charm expects {HEADSCALE_VERSION}, "
                f"container reports {running_v}. Attach the correct OCI resource."
            )
            charm.unit.status = ops.BlockedStatus(
                f"Wrong headscale image: expected {HEADSCALE_VERSION}, got {running_v}"
            )
            return

        old_v = Version(self._get_stored_version())

        if running_v == old_v:
            # Explicitly (re)persist the confirmed-running version. Without this,
            # a fresh install (or any no-op pebble-ready) would never actually
            # write to the peer relation databag -- _get_stored_version()'s
            # fallback only *returns* the current HEADSCALE_VERSION when no key
            # is set, it doesn't persist it. Since that fallback re-evaluates
            # HEADSCALE_VERSION from whichever charm revision is currently
            # running, an unwritten value would silently track every new
            # revision instead of the version that was actually last verified
            # running, defeating downgrade/skip-minor detection on the next
            # upgrade.
            self._set_stored_version(str(running_v))
            charm._start_and_activate()
            return

        reason = self._blocked_version_jump(old_v, running_v)
        if reason:
            logger.error(f"Upgrade blocked at pebble-ready: {reason}")
            charm.unit.status = ops.BlockedStatus(reason)
            return

        # Version has changed — check for unacknowledged manual gate
        gate_reason = self._manual_gate_reason(running_v)
        if gate_reason and self._get_upgrade_acknowledged() != str(running_v):
            logger.error(
                f"Manual steps required before this upgrade: {gate_reason} "
                "Check logs from upgrade-charm, complete them, then run proceed-upgrade."
            )
            charm.unit.status = ops.BlockedStatus(
                f"Manual steps required: {gate_reason} Then run proceed-upgrade action."
            )
            return

        if not self._run_version_upgrade(old_v, running_v):
            return
        charm.unit.status = ops.ActiveStatus()

    def _on_proceed_upgrade(self, event: ops.ActionEvent) -> None:
        charm = self._charm
        if not charm.container.can_connect():
            event.fail("Workload container is not ready.")
            return

        old_v = Version(self._get_stored_version())
        new_v = Version(HEADSCALE_VERSION)

        if old_v == new_v:
            event.fail("No upgrade in progress: stored version already matches charm version.")
            return

        if not self._manual_gate_reason(new_v):
            event.fail(
                "No manual steps required for this upgrade; pebble-ready handles it automatically."
            )
            return

        running_v_str = charm.headscale.get_version()
        if running_v_str is None:
            event.fail("Could not read headscale version from container. Is the workload running?")
            return
        running_v = Version(running_v_str)
        if running_v != new_v:
            event.fail(
                f"Container reports headscale {running_v}, charm expects {new_v}. "
                "Attach the correct OCI resource before running proceed-upgrade."
            )
            return

        self._set_upgrade_acknowledged(str(new_v))
        event.log(f"Upgrade to {new_v} acknowledged. Running upgrade sequence.")

        if charm.certs.configure_certs():
            charm.headscale.tls = True
        try:
            charm.headscale.render_config(restart=False)
        except RuntimeError as e:
            event.fail(f"Failed to write headscale config: {e}")
            return
        if not self._run_version_upgrade(old_v, running_v):
            event.fail("Upgrade sequence failed. Check unit logs.")
            return

        charm.unit.status = ops.ActiveStatus()
        event.set_results({"result": f"Headscale upgraded from {old_v} to {new_v} successfully."})
