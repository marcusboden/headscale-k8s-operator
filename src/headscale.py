# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Functions for interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging
import dataclasses
from typing import Any, Dict, List, cast

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, kw_only=True)
class HeadscaleConfig:
    """Configuration for the Headscale server."""

    name: str
    log_level: str
    policy: str
    magic_dns: str

    def generate_config(self, external_url="") -> Dict[str, Any]:
        """Generates config file str.

        Returns:
            A dict of the config to be rendered as yaml.
        """
        config = {
                "server_url": f"http://{external_url}",
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
                "log": {
                    "format": "text",
                    "level": self.log_level
                },
                "policy": self._policy(),
                "dns": self._dns(),
                "unix_socket": "/var/run/headscale/headscale.sock",
                "unix_socket_permission": "0770"
        }
        return config
    
    def _dns(self) -> Dict:
        if self.magic_dns == "":
            return { "magic_dns": False }
        else:
            return { "magic_dns": True, "base_domain": self.magic_dns, "override_local_dns": False }

    def _policy(self) -> Dict:
        if self.policy:
            return { "mode": "file", "path": "/etc/headscale/policy.hujson" }
        else:
            return { "mode": "database" }

    def __post_init__(self):
        """Validate the configuration."""
        
        levels = ["info", "debug", "critical", "warning"]
        if self.log_level not in levels:
            raise ValueError(f"Invalid log-level: '{log-level}' not in {", ".join(levels)}.")


def get_version() -> str | None:
    """Get the running version of the workload."""
    # You'll need to implement this function (or remove it if not needed).
    return "version one, mf"
