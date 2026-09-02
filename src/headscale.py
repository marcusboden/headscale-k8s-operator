# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Functions for interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""
import datetime
import json
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

POLICY_PATH=Path("/etc/headscale/policy.hujson")
DERP_PATH=Path("/etc/headscale/derp.yaml")
SQLITE_PATH=Path("/var/lib/headscale/db.sqlite")
NOISE_KEY=Path("/var/lib/headscale/noise_private.key")

BACKUP_PATH=Path("/tmp/backup/")


@dataclasses.dataclass(frozen=True, kw_only=True)
class HeadscaleConfig:
    """Configuration for the Headscale server."""

    name: str
    log_level: str
    policy: Optional[str] = ""
    magic_dns: str
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_secret: Optional[ops.Secret] = None
    oidc_expiry: Optional[str] = None
    oidc_scope: Optional[List[str]] = None
    oidc_groups: Optional[List[str]] = None
    node_expiry: Optional[str] = None
    derp_map: Optional[str] = None
    derp_map_url: Optional[str] = None
    dns_extra_records: Optional[str] = ""
    port: Optional[int] = None

    @staticmethod
    def static_config() -> Dict[str, Any]:
        return {
            "metrics_listen_addr": "0.0.0.0:9090",
            "noise": {
                "private_key_path": str(NOISE_KEY)
            },
            "prefixes": {
                "v4": "100.64.0.0/10",
                "v6": "fd7a:115c:a1e0::/48",
                "allocation": "sequential",
            },
            "disable_check_updates": True,
            "database": {
                "type": "sqlite",
                "debug": "false",
                "sqlite": {
                    "path": str(SQLITE_PATH),
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
            "scope": self.oidc_scope or ["openid", "email", "profile"],
            "only_start_if_oidc_is_available": True
        }
        if self.oidc_groups:
            oidc["allowed_groups"] = self.oidc_groups
        return { "oidc": oidc }

    def node(self) -> Dict[str, Dict[str, Any]]:
        """Build the `node` config block (default node-key expiry, ephemeral GC timeout).

        Headscale 0.29.0 removed `oidc.expiry` in favor of a general
        top-level `node.expiry` (applies to all registration methods,
        though tagged preauth-key nodes are exempt regardless of what this
        is set to). `node-expiry` is the up-to-date charm config option;
        `oidc-expiry` is kept for backwards compatibility and is
        deprecated. If both are set, `node-expiry` wins.

        `ephemeral_node_inactivity_timeout` was also deprecated in favor
        of the nested `node.ephemeral.inactivity_timeout` in the same
        release.
        """
        node: Dict[str, Any] = {"ephemeral": {"inactivity_timeout": "30m"}}
        expiry = self.node_expiry or self.oidc_expiry
        if not expiry and self.oidc_issuer:
            # Preserve the previous implicit default: only force an expiry
            # when OIDC is configured and no explicit expiry was given.
            expiry = "1d"
        if expiry:
            node["expiry"] = expiry
        return {"node": node}

    def dns(self) -> Dict:
        dns_dict = {"magic_dns": False}
        if self.dns_extra_records:
            dns_dict |= {"extra_records": yaml.safe_load(self.dns_extra_records)}
        if self.magic_dns != "":
            dns_dict |= {"magic_dns": True, "base_domain": self.magic_dns, "override_local_dns": False}
        return dns_dict

    def tls(self, enabled: bool, name: str) -> Dict[str, str]:
        logger.info(f"generating TLS config. Enabled: {enabled}, Name: {name}")
        if enabled:
            port = self.port or 443
            return {
                "tls_cert_path": f"{CERTS_DIR_PATH}/{CERTIFICATE_NAME}",
                "tls_key_path": f"{CERTS_DIR_PATH}/{PRIVATE_KEY_NAME}",
                "server_url": f"https://{name}:{port}",
                "listen_addr": f"0.0.0.0:{port}",
            }
        port = self.port or 80
        return {
            "server_url": f"http://{name}:{port}",
            "listen_addr": f"0.0.0.0:{port}"
        }

    def log(self) -> Dict[str, Dict[str, str]]:
        return {"log": {"level": self.log_level}}

    def derp(self) -> Dict[str, Dict[str, Any]]:
        derp = {"server": {"enabled": False}}
        if self.derp_map_url:
            derp["urls"] = [self.derp_map_url]
            derp["auto_update_enabled"] = True
            derp["update_frequency"] = "24h"
        if self.derp_map:
            derp["paths"] = [str(DERP_PATH)]
        return { "derp": derp }

    def get_policy(self) -> Dict:
        if self.policy is not None and self.policy != "":
            return {"mode": "file", "path": str(POLICY_PATH)}
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
                raise ValueError(f"Minimum OIDC Settings: issuer, secret, client_id")
            if self.oidc_groups and not self.oidc_scope:
                logger.warning("OIDC groups are set, but no scope.")
        if self.derp_map:
            try:
                yaml.safe_load(self.derp_map)
            except yaml.YAMLError as exc:
                logger.error("derp map is not valid yaml")
                raise exc
        if not self.derp_map and not self.derp_map_url:
            raise ValueError(f"Either derp-map or derp-map-url must be set.")

        if self.dns_extra_records:
            try:
                yaml.safe_load(self.dns_extra_records)
            except yaml.YAMLError as exc:
                logger.error("dns extra conf is not valid yaml")
                raise exc

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

    def get_version(self) -> Optional[str]:
        """Return the headscale version string from the running binary, or None on failure."""
        ret = self._run_headscale_cmd(["version"])
        if ret.exit_code != 0:
            logger.error(f"Failed to get headscale version: {ret.stderr}")
            return None
        # `headscale --output yaml version` returns a mapping with "version" and
        # "commit" keys. Older/other invocations may instead yield a plain
        # scalar, which dictify() wraps as {"out": <string>}; handle both.
        if isinstance(ret.stdout, dict) and "version" in ret.stdout:
            raw = str(ret.stdout["version"]).strip()
        elif isinstance(ret.stdout, dict) and "out" in ret.stdout:
            raw = ret.stdout["out"].strip()
        else:
            logger.warning(f"Unexpected version output format: {ret.stdout}")
            return None
        # Take only the first token (resilient to build metadata like "0.26.1 (commit abc)"),
        # then strip a leading 'v' if present.
        return raw.split()[0].lstrip("v")

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
        config_dict |= self.config.oidc()
        config_dict |= self.config.node()
        config_dict |= self.config.tls(self.tls, self.name)
        config_dict |= self.config.log()
        config_dict |= self.config.derp()
        return config_dict

    def render_config(self, restart: bool = True) -> None:
        try:
            self._check_policy()
            if self.config.derp_map:
                self.container.push(DERP_PATH, self.config.derp_map, make_dirs=True)
            self.container.push(
                "/etc/headscale/config.yaml", yaml.dump(self._generate_config()), make_dirs=True
            )
            if restart:
                self.container.restart(self.pebble_service_name)
        except (ops.pebble.APIError, ops.pebble.ConnectionError, ops.pebble.ChangeError) as e:
            raise RuntimeError(f"Failed to push config or restart headscale: {e}") from e

    def _run_cmd(self, command: List[str]):
        try:
            exc = self.container.exec(command)
            out, err = exc.wait_output()
            return CmdResult(stderr=err, stdout=dictify(out), exit_code=0)
        except ops.pebble.ExecError as e:
            logger.error(f"Command '{e.command}' returned {e.exit_code}.\nStdout: {e.stdout}\nStderr: {e.stderr}")
            return CmdResult(stderr=e.stderr, stdout=dictify(e.stdout), exit_code=e.exit_code)
        except (ops.pebble.APIError, ops.pebble.ConnectionError) as e:
            logger.error(f"Pebble error running {command}: {e}")
            return CmdResult(stderr=str(e), stdout={"out": ""}, exit_code=1)

    def _run_headscale_cmd(self, command: List[str]) -> CmdResult:
        hs_bin = "/usr/bin/headscale"
        return self._run_cmd([hs_bin, "--output", "yaml"] + command)

    def _check_policy(self):
        """Checks validity of hujson file by running it through hujsonfmt on the container"""
        if self.config.policy:
            self.container.push(POLICY_PATH, self.config.policy, make_dirs=True)
            exc = self.container.exec(['hujsonfmt', str(POLICY_PATH)])
            try:
                exc.wait()
            except ops.pebble.ExecError as e:
                logger.error(f"Policy file check returned {e.exit_code}. Command: {e.command}, Output: {e.stderr}")
                raise RuntimeError("Policy file incorrect") from e

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

    def expire_authkey(self, authkey_id: int) -> CmdResult:
        """Expire a preauthkey by its numeric ID.

        Headscale >=0.28.0 replaced the key-string + `--user` based
        `preauthkey expire` invocation with ID-only lookup
        (`preauthkey expire --id <ID>`). Use `list_authkeys()` to find the ID.
        """
        return self._run_headscale_cmd(["preauthkey", "expire", "--id", str(authkey_id)])

    def list_authkeys(self) -> CmdResult:
        """List all preauthkeys.

        Headscale >=0.28.0 removed the `--user` filter from `preauthkey list`;
        it now always lists every key for every user, system-wide.
        """
        return self._run_headscale_cmd(["preauthkey", "list"])

    def check_ssh_wildcard_policy(self) -> Optional[str]:
        """Return a warning if the configured policy has a wildcard SSH destination.

        Headscale >=0.28.0 rejects ACL policies containing an SSH rule with a
        wildcard ("*") destination at load time (see the 0.28.0 release
        notes). Returns None if there's no policy configured, if none of its
        SSH rules use a wildcard destination, or if the policy couldn't be
        parsed (fail open -- this is a best-effort heads-up, not validation).
        """
        if not self.config.policy:
            return None
        try:
            self.container.push(POLICY_PATH, self.config.policy, make_dirs=True)
            exc = self.container.exec(["hujsonfmt", "-s", str(POLICY_PATH)])
            out, _ = exc.wait_output()
            parsed = json.loads(out)
        except Exception as e:
            logger.warning(f"Could not parse policy to check for SSH wildcard destinations: {e}")
            return None
        if not isinstance(parsed, dict):
            return None
        for rule in parsed.get("ssh") or []:
            if "*" in (rule.get("dst") or []):
                return (
                    "Configured policy has an SSH rule with a wildcard ('*') destination, "
                    "which headscale 0.28+ rejects at startup. Update the policy config to "
                    "use 'autogroup:member'/'autogroup:tagged'/specific tags instead, then "
                    "run the proceed-upgrade action."
                )
        return None

    def restore_backup(self, backup_path: str) -> Path:
        backup_tar_path = Path(backup_path)

        # Do backup
        backup = self.create_backup()

        # stop headscale
        self.container.stop(self.pebble_service_name)

        # restore backup
        try:
            with TemporaryDirectory() as d:
                with TarFile.open(backup_tar_path) as t:
                    t.extractall(path=d)
                self.container.push_path(source_path=Path(d) / "db.sqlite", dest_dir=Path(SQLITE_PATH).parent)
                self.container.push_path(source_path=Path(d) / "noise_private.key", dest_dir=Path(NOISE_KEY).parent)
        finally:
            self.container.start(self.pebble_service_name)

        # cleanup
        backup_tar_path.unlink()
        return backup

    def create_backup(self) -> Path:
        # Create sqlite backup
        remote_tmpdir = Path("/tmp")
        cmd = ['sqlite3_rsync', str(SQLITE_PATH), str(remote_tmpdir / SQLITE_PATH.name)]
        ret = self._run_cmd(cmd)
        if ret.exit_code != 0:
            raise Exception(f"Could not create backup. {ret}")

        # clean up old backups
        BACKUP_PATH.mkdir(parents=False, exist_ok=True)
        for f in BACKUP_PATH.iterdir():
            f.unlink()

        # Get Timestamp
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_file = BACKUP_PATH / f'headscale-backup-{ts}.tar.gz'

        # create tar
        with TemporaryDirectory() as d:
            self.container.pull_path(source_path=[remote_tmpdir / SQLITE_PATH.name, NOISE_KEY],dest_dir=d)
            with TarFile.open(backup_file, 'w:gz') as t:
                t.add(Path(d) / SQLITE_PATH.name, arcname=SQLITE_PATH.name)
                t.add(Path(d) / NOISE_KEY.name, arcname=NOISE_KEY.name)

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