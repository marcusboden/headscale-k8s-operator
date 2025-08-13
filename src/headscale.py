# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Functions for interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging
import dataclasses
from pydantic import BaseModel
import ops
import yaml
from typing import Any, Dict, Optional, List#, cast

from certificates import (CERTIFICATE_NAME, CERTS_DIR_PATH, PRIVATE_KEY_NAME)

logger = logging.getLogger(__name__)

POLICY_PATH="/etc/headscale/policy.hujson"

@dataclasses.dataclass(frozen=True, kw_only=True)
class HeadscaleConfig:
    """Configuration for the Headscale server."""

    name: str
    log_level: str
    policy: Optional[str] = None
    magic_dns: str
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_secret: Optional[ops.Secret] = None
    oidc_expiry: Optional[str] = None
    oidc_scope: Optional[List[str]] = None
    oidc_groups: Optional[List[str]] = None

    @staticmethod
    def static_config() -> Dict[str, Any]:
        return {
            "listen_addr": f"0.0.0.0:80",
            "metrics_listen_addr": "0.0.0.0:9090",
            "noise": {
                "private_key_path": "/var/lib/headscale/noise_private.key"
            },
            "prefixes": {
                "v4": "100.64.0.0/10",
                "v6": "fd7a:115c:a1e0::/48",
                "allocation": "sequential",
            },
            "derp": {
                "server": {
                    "enabled": False
                },
                "urls": [
                    "https://controlplane.tailscale.com/derpmap/default"
                ],
                "auto_update_enabled": True,
                "update_frequency": "24h"
            },
            "disable_check_updates": False,
            "ephemeral_node_inactivity_timeout": "30m",
            "database": {
                "type": "sqlite",
                "debug": "false",
                "sqlite": {
                    "path": "/var/lib/headscale/db.sqlite",
                    "write_ahead_log": True,
                    "wal_autocheckpoint": 1000
                },
            },
            "unix_socket": "/var/run/headscale/headscale.sock",
            "unix_socket_permission": "0770"
        }

    def oidc(self) -> Dict:
        if not self.oidc_issuer:
            return {}
        secret = self.oidc_secret.get_content()['oidc-secret']
        oidc = {
            "issuer": self.oidc_issuer,
            "client_id": self.oidc_client_id,
            "client_secret": secret,
            "expiry": self.oidc_expiry or "1d",
            "scope": self.oidc_scope or ["openid", "email", "profile"],
            "only_start_if_oidc_is_available": True
        }
        if self.oidc_groups:
            oidc["allowed_groups"] = self.oidc_groups
        return { "oidc": oidc }

    def dns(self) -> Dict:
        if self.magic_dns == "":
            return { "magic_dns": False }
        else:
            return { "magic_dns": True, "base_domain": self.magic_dns, "override_local_dns": False }

    def tls(self, enabled: bool, name: str):
        logger.info(f"generating TLS config. Enabled: {enabled}, Name: {name}")
        if enabled:
            return {
                "tls_cert_path": f"{CERTS_DIR_PATH}/{CERTIFICATE_NAME}" if enabled else "",
                "tls_key_path": f"{CERTS_DIR_PATH}/{PRIVATE_KEY_NAME}" if enabled else "",
                "server_url": f"https://{name}:443",
                "listen_addr": f"0.0.0.0:443",
            }
        return {
            "server_url": f"http://{name}:80",
            "listen_addr": f"0.0.0.0:80"
        }

    def get_policy(self) -> Dict:
        if self.policy is not None:
            return {"mode": "file", "path": POLICY_PATH}
        else:
            return {"mode": "database"}

    def __post_init__(self):
        """Validate the configuration."""
        
        levels = ["info", "debug", "critical", "warning"]
        if self.log_level not in levels:
            raise ValueError(f"Invalid log-level: '{self.log_level}' not in {", ".join(levels)}.")

        oidc_configs = [
            self.oidc_issuer, self.oidc_client_id, self.oidc_secret,
            self.oidc_expiry, self.oidc_scope, self.oidc_groups
        ]
        if any(oidc_configs):
            if not all([self.oidc_issuer, self.oidc_secret, self.oidc_client_id]):
                logging.error(f"{self.oidc_issuer}, {self.oidc_secret}, {self.oidc_client_id}")
                raise ValueError(f"Minimum OIDC Settings: issuer, secret, client_id")
            if self.oidc_groups and not self.oidc_scope:
                logger.warning("OIDC groups are set, but no scope.")

class HeadscaleCmdResult(BaseModel):
    stderr: str
    stdout: Dict
    exit_code: int


class Headscale:
    """Interact with the container"""

    def __init__(self, container: ops.Container, config: HeadscaleConfig):
        self.container = container
        self.config: HeadscaleConfig = config
        self.pebble_service_name = 'headscale-server'
        self.name = config.name
        self.tls = False

    def setup(self):
        ret = self._run_headscale_cmd(["user", "create", "admin"])
        return ret["exit_code"] == 0

    def set_name(self, name):
        self.name = name

    def _generate_config(self) -> Dict[str, Any]:
        config = self.config.static_config()
        config["dns"] = self.config.dns()
        config["policy"] = self.config.get_policy()
        config["server_url"] = f"http://{self.name}:80"
        return config

    def render_config(self):
        try:
            self._check_policy()
        except ValueError as e:
            raise e

        self.container.push("/etc/headscale/config.yaml", yaml.dump(self._generate_config()), make_dirs=True)
        self.container.restart(self.pebble_service_name)

    def _run_headscale_cmd(self, command: List[str]) -> HeadscaleCmdResult:
        hs_bin = "/usr/bin/headscale"
        exc = self.container.exec([hs_bin, "--output", "yaml"] + command)
        try:
            out, err = exc.wait_output()
            return HeadscaleCmdResult(stderr=err, stdout=dictify(out), exit_code=0)
        except ops.pebble.ExecError as e:
            logger.error(f"Command '{e.command}' returned {e.exit_code}.\nStdout: {e.stdout}\nStderr: {e.stderr}")
            return HeadscaleCmdResult(stderr=e.stderr, stdout=dictify(e.stdout),exit_code=e.exit_code )

    def _check_policy(self):
        """Checks validity of hujson file by running it through hujsonfmt on the container"""
        if self.config.policy:
            self.container.push(POLICY_PATH, self.config.policy, make_dirs=True)
            exc = self.container.exec(['hujsonfmt', POLICY_PATH])
            try:
                exc.wait()
            except ops.pebble.ExecError as e:
                logger.error(f"Policy file check returned {e.exit_code}. Command: {e.command}, Output: {e.stderr}")
                raise ValueError("Policy file incorrect")

    def create_authkey(self, tags: str, expiry: str, reusable: bool, ephemeral: bool) -> HeadscaleCmdResult:
        cmd = ["preauthkey", "create"]
        # Headscale wants the tags prepended with "tag:"
        cmd += ["--tags", "tag:"+",tag:".join(tags.split(","))]
        cmd += ["--expiration", expiry]
        if reusable:
            cmd += ["--reusable"]
        if ephemeral:
            cmd += ["--ephemeral"]
        cmd += ["-u", "1"]

        return self._run_headscale_cmd(cmd)

    def expire_authkey(self, authkey: str) -> HeadscaleCmdResult:
        cmd = ["preauthkey", "expire"]
        cmd += [authkey]
        cmd += ["-u", "1"]
        return self._run_headscale_cmd(cmd)

    def list_authkeys(self) -> HeadscaleCmdResult:
        return self._run_headscale_cmd(["preauthkey", "list", "-u", "1"])

    @staticmethod
    def get_version() -> str | None:
        """Get the running version of the workload."""
        # You'll need to implement this function (or remove it if not needed).
        return "version one, mf"

def dictify(out):
    """headscale doesn't always return proper yaml, so I can't trust it to be yamlable"""
    d = ""
    try:
        d = yaml.safe_load(out)
    except yaml.YAMLError as e:
        logger.error(f"Invalid YAML: {out}.\n{e}")
    # well, a simple string is valid yaml :/
    if not isinstance(d, dict):
        d = {"out": out}
    return d