# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Unit tests for the Headscale wrapper's authkey CLI construction and policy checks.

These specifically cover the headscale 0.28.0 breaking changes to the
`preauthkey` CLI (ID-based expire, no more --user filter on list) and the
new SSH-wildcard-policy pre-upgrade check.
"""

from __future__ import annotations

import json
from unittest import mock

from headscale import Headscale, HeadscaleConfig


def _make_headscale(policy: str | None = "") -> tuple[Headscale, mock.MagicMock]:
    """Return a Headscale instance wired to a MagicMock container."""
    container = mock.MagicMock()
    config = HeadscaleConfig(
        name="headscale",
        log_level="info",
        policy=policy,
        magic_dns="",
        derp_map_url="https://example.com/derp.yaml",
    )
    return Headscale(container, config), container


def _exec_returns(container: mock.MagicMock, stdout: str, stderr: str = "") -> None:
    """Configure container.exec(...).wait_output() to return the given output."""
    exc = mock.MagicMock()
    exc.wait_output.return_value = (stdout, stderr)
    container.exec.return_value = exc


class TestCreateAuthkey:
    """create_authkey is unaffected by 0.28: --user remains valid on `create`."""

    def test_command_construction(self):
        hs, container = _make_headscale()
        _exec_returns(container, "key: abc123\n")

        hs.create_authkey(tags="server,ci", expiry="1h", reusable=True, ephemeral=False)

        container.exec.assert_called_once_with(
            [
                "/usr/bin/headscale", "--output", "yaml",
                "preauthkey", "create",
                "--tags", "tag:server,tag:ci",
                "--expiration", "1h",
                "--reusable",
                "-u", "1",
            ]
        )


class TestExpireAuthkey:
    """Headscale >=0.28 requires --id, not a positional key string + --user."""

    def test_command_construction_uses_id(self):
        hs, container = _make_headscale()
        _exec_returns(container, "")

        hs.expire_authkey(authkey_id=42)

        container.exec.assert_called_once_with(
            ["/usr/bin/headscale", "--output", "yaml", "preauthkey", "expire", "--id", "42"]
        )


class TestListAuthkeys:
    """Headscale >=0.28 removed --user from `preauthkey list` entirely."""

    def test_command_construction_no_user_filter(self):
        hs, container = _make_headscale()
        _exec_returns(container, "[]")

        hs.list_authkeys()

        container.exec.assert_called_once_with(
            ["/usr/bin/headscale", "--output", "yaml", "preauthkey", "list"]
        )


class TestCheckSshWildcardPolicy:
    """Only headscale 0.28+ rejects wildcard SSH destinations; check for it."""

    def test_no_policy_configured(self):
        hs, container = _make_headscale(policy="")
        assert hs.check_ssh_wildcard_policy() is None
        container.exec.assert_not_called()

    def test_policy_without_wildcard(self):
        hs, container = _make_headscale(policy='{"ssh": [{"dst": ["autogroup:tagged"]}]}')
        _exec_returns(container, json.dumps({"ssh": [{"dst": ["autogroup:tagged"]}]}))

        assert hs.check_ssh_wildcard_policy() is None

    def test_policy_with_wildcard_destination(self):
        hs, container = _make_headscale(policy='{"ssh": [{"dst": ["*"]}]}')
        _exec_returns(container, json.dumps({"ssh": [{"dst": ["*"]}]}))

        result = hs.check_ssh_wildcard_policy()

        assert result is not None
        assert "wildcard" in result.lower()

    def test_policy_without_ssh_section(self):
        hs, container = _make_headscale(policy='{"acls": []}')
        _exec_returns(container, json.dumps({"acls": []}))

        assert hs.check_ssh_wildcard_policy() is None

    def test_unparseable_policy_fails_open(self):
        """A policy that can't be parsed shouldn't block the upgrade indefinitely."""
        hs, container = _make_headscale(policy="not valid json or hujson {")
        _exec_returns(container, "this is not json")

        assert hs.check_ssh_wildcard_policy() is None
