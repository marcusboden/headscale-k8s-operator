# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Functions for interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""
import datetime
import logging
import dataclasses
from pathlib import Path

from pydantic import BaseModel
import ops
import yaml
from tempfile import TemporaryDirectory
from tarfile import TarFile
from typing import Any, Dict, Optional, List#, cast

from certificates import (CERTIFICATE_NAME, CERTS_DIR_PATH, PRIVATE_KEY_NAME)

logger = logging.getLogger(__name__)

POLICY_PATH="/etc/headscale/policy.hujson"
SQLITE_PATH="/var/lib/headscale/db.sqlite"
NOISE_KEY="/var/lib/headscale/noise_private.key"

BACKUP_PATH=Path("/tmp/backup/")


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
            "metrics_listen_addr": "0.0.0.0:9090",
            "noise": {
                "private_key_path": NOISE_KEY
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
                    "path": SQLITE_PATH,
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

    def tls(self, enabled: bool, name: str) -> Dict[str, str]:
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

    def log(self) -> Dict[str, Dict[str, str]]:
        return {"log": {"level": self.log_level}}


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

class CmdResult(BaseModel):
    stderr: str
    stdout: Dict | List
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
        self._create_admin_user()

    def _create_admin_user(self) -> None:
        # check if user exists
        ret = self._run_headscale_cmd(["user", "list"])
        if ret.exit_code != 0:
            raise Exception("Couldn't list users, bailing out")
        logger.info(f"found users: {ret.stdout}")
        if "charm-admin" not in [u["name"] for u in ret.stdout]:
            logger.info(f"creating Admin user")
            # create admin user
            if self._run_headscale_cmd(["user", "create", "charm-admin"]).exit_code != 0:
                raise Exception("Couldn't create admin user, bailing out")

    def set_name(self, name):
        self.name = name

    def _generate_config(self) -> Dict[str, Any]:
        config_dict = self.config.static_config()
        config_dict["dns"] = self.config.dns()
        config_dict["policy"] = self.config.get_policy()
        # merge operator for dict: Didn't know that one!
        config_dict |= self.config.oidc()
        config_dict |= self.config.tls(self.tls, self.name)
        config_dict |= self.config.log()

        return config_dict

    def render_config(self):
        try:
            self._check_policy()
        except ValueError as e:
            raise e

        self.container.push("/etc/headscale/config.yaml", yaml.dump(self._generate_config()), make_dirs=True)
        self.container.restart(self.pebble_service_name)

    def _run_cmd(self, command: List[str]):
        exc = self.container.exec(command)
        try:
            out, err = exc.wait_output()
            return CmdResult(stderr=err, stdout=dictify(out), exit_code=0)
        except ops.pebble.ExecError as e:
            logger.error(f"Command '{e.command}' returned {e.exit_code}.\nStdout: {e.stdout}\nStderr: {e.stderr}")
            return CmdResult(stderr=e.stderr, stdout=dictify(e.stdout), exit_code=e.exit_code)

    def _run_headscale_cmd(self, command: List[str]) -> CmdResult:
        hs_bin = "/usr/bin/headscale"
        return self._run_cmd([hs_bin, "--output", "yaml"] + command)

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

    def create_authkey(self, tags: str, expiry: str, reusable: bool, ephemeral: bool) -> CmdResult:
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

    def expire_authkey(self, authkey: str) -> CmdResult:
        cmd = ["preauthkey", "expire"]
        cmd += [authkey]
        cmd += ["-u", "1"]
        return self._run_headscale_cmd(cmd)

    def list_authkeys(self) -> CmdResult:
        return self._run_headscale_cmd(["preauthkey", "list", "-u", "1"])

    @staticmethod
    def get_version() -> str | None:
        """Get the running version of the workload."""
        # You'll need to implement this function (or remove it if not needed).
        return "version one, mf"

    def restore_backup(self, backup_path: str) -> Path:
        backup_tar_path = Path(backup_path)

        # Do backup
        backup = self.create_backup()

        # stop headscale
        self.container.stop(self.pebble_service_name)

        # restore backup
        with TemporaryDirectory() as d:
            with TarFile.open(backup_tar_path) as t:
                t.extractall(path=d)
            self.container.push_path(source_path=Path(d) / "db.sqlite", dest_dir=Path(SQLITE_PATH).parent)
            self.container.push_path(source_path=Path(d) / "noise_private.key", dest_dir=Path(NOISE_KEY).parent)


        self.container.start(self.pebble_service_name)

        # cleanup
        backup_tar_path.unlink()
        return backup

    def create_backup(self) -> Path:
        # Create sqlite backup
        cmd = ['sqlite3_rsync', SQLITE_PATH, '/tmp/db.sqlite']
        ret = self._run_cmd(cmd)
        if ret.exit_code != 0:
            raise Exception(f"Could not create backup. {ret}")

        # Get Noise Key
        self._run_cmd(['cp', NOISE_KEY, '/tmp/'])


        # create tar from it
        cmd = ['tar', '-czf', '/tmp/backup.tar.gz', '-C', '/tmp/', 'db.sqlite', 'noise_private.key']
        ret = self._run_cmd(cmd)
        if ret.exit_code != 0:
            raise Exception(f"Could not tar backup. {ret}")

        # clean up old backups
        BACKUP_PATH.mkdir(parents=False, exist_ok=True)
        for f in BACKUP_PATH.iterdir():
            f.unlink()

        # pull backup to charm container
        self.container.pull_path(source_path='/tmp/backup.tar.gz', dest_dir=BACKUP_PATH)

        # Rename with Timestamp
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_file = BACKUP_PATH / f'headscale-backup-{ts}.tar.gz'
        Path(BACKUP_PATH / "backup.tar.gz").rename(backup_file)
        return backup_file


def dictify(out) -> Dict|List:
    """headscale doesn't always return proper yaml, so I can't trust it to be yamlable"""
    d = ""
    try:
        d = yaml.safe_load(out)
        logger.debug(f"loaded yaml output: {d}")
    except yaml.YAMLError as e:
        logger.error(f"Invalid YAML: {out}.\n{e}")
    # well, a simple string is valid yaml :/
    if not isinstance(d, dict) and not isinstance(d, list):
        d = {"out": out}
    return d