# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Helpers for building the charm and rock at a pinned headscale version.

These helpers shell out to `charmcraft`, `rockcraft`, and `skopeo` to produce
a charm package and an OCI image pinned to a specific upstream headscale
release tag. They require the `HEADSCALE_ROCK_PATH` environment variable to
point at a local checkout of the `headscale-rock` repository.

Building a rock compiles headscale (and its Go dependencies) from source, so
these helpers are slow. Callers should memoize results per-version, e.g. via
the `built_charm`/`built_rock` fixtures in `conftest.py`.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import uuid

import pytest

HEADSCALE_VERSION_PATTERN = re.compile(r'HEADSCALE_VERSION = "[^"]+"')
ROCK_SOURCE_TAG_LINE = "source-tag: v0.26.1"

# Headscale fetches and validates the DERP map at startup (crash-looping on an
# invalid/empty one), so unlike the unit tests (which mock this away), the
# integration tests need a real, reachable, valid DERP map. This is the same
# public Tailscale DERP map the rock's own bundled default config uses.
DERP_MAP_URL_CONFIG = {"derp-map-url": "https://controlplane.tailscale.com/derpmap/default"}

DEFAULT_REGISTRY = "localhost:32000"

_CHARM_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_CHARM_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "tests", ".tox", "venv", "build", "__pycache__", "*.charm"
)


def _registry() -> str:
    """Return the OCI registry to push built rocks to."""
    return os.environ.get("TEST_IMAGE_REGISTRY", DEFAULT_REGISTRY)


def _rock_repo_path() -> pathlib.Path:
    """Return the local headscale-rock checkout path from `HEADSCALE_ROCK_PATH`."""
    raw_path = os.environ.get("HEADSCALE_ROCK_PATH")
    if not raw_path:
        pytest.fail("HEADSCALE_ROCK_PATH must be set to a local headscale-rock checkout.")
    rock_path = pathlib.Path(raw_path)
    if not rock_path.is_dir() or not (rock_path / "rockcraft.yaml").is_file():
        pytest.fail(
            f"HEADSCALE_ROCK_PATH ({rock_path}) is not a directory containing rockcraft.yaml."
        )
    return rock_path


def _patch_file(path: pathlib.Path, old: str, new: str) -> None:
    """Replace an exact, anchored substring in `path`, failing if it's not found."""
    content = path.read_text()
    if old not in content:
        raise RuntimeError(f"Expected to find {old!r} in {path}, but it was not present.")
    patched = content.replace(old, new)
    path.write_text(patched)
    if new not in patched:
        raise RuntimeError(f"Patch of {path} did not take effect: {new!r} not found after write.")


def _patch_regex(path: pathlib.Path, pattern: re.Pattern[str], replacement: str) -> None:
    """Replace the first match of `pattern` in `path` with `replacement`.

    Unlike `_patch_file`, this doesn't need to know the *current* value --
    just the shape of the line -- so it doesn't need updating every time the
    real value (e.g. HEADSCALE_VERSION) is bumped in the repo.
    """
    content = path.read_text()
    patched, count = pattern.subn(replacement, content, count=1)
    if count == 0:
        raise RuntimeError(f"Pattern {pattern.pattern!r} not found in {path}.")
    path.write_text(patched)


def _single_glob_match(directory: pathlib.Path, pattern: str) -> pathlib.Path:
    """Return the single file matching `pattern` in `directory`, or raise."""
    matches = list(directory.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one match for {pattern!r} in {directory}, got {matches}"
        )
    return matches[0]


def _run_subprocess(command: list[str], cwd: pathlib.Path) -> None:
    """Run `command` in `cwd`, raising an informative error on failure."""
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Command {command} failed in {cwd} with exit code {e.returncode}.\n"
            f"Stdout: {e.stdout}\nStderr: {e.stderr}"
        ) from e


def build_charm_at_version(version: str, tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Build the charm with `HEADSCALE_VERSION` pinned to `version`.

    Copies the charm repository into a fresh temp directory, patches
    `src/upgrade.py`'s `HEADSCALE_VERSION` constant, runs `charmcraft pack`,
    and returns the path to the resulting `.charm` file.
    """
    tmp_dir = tmp_path_factory.mktemp(f"charm-{version}")
    shutil.copytree(_CHARM_REPO_ROOT, tmp_dir, dirs_exist_ok=True, ignore=_CHARM_COPY_IGNORE)

    _patch_regex(
        tmp_dir / "src" / "upgrade.py",
        HEADSCALE_VERSION_PATTERN,
        f'HEADSCALE_VERSION = "{version}"',
    )

    _run_subprocess(["charmcraft", "pack"], cwd=tmp_dir)

    return _single_glob_match(tmp_dir, "*.charm").resolve()


def build_rock_at_version(version: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build and push the headscale rock at upstream release tag `v{version}`.

    Copies the headscale-rock repository into a fresh temp directory, patches
    `rockcraft.yaml`'s `source-tag`, runs `rockcraft pack`, pushes the result
    to the configured registry with `skopeo`, and returns the full image
    reference.
    """
    rock_repo = _rock_repo_path()
    tmp_dir = tmp_path_factory.mktemp(f"rock-{version}")
    shutil.copytree(
        rock_repo, tmp_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git")
    )

    _patch_file(
        tmp_dir / "rockcraft.yaml",
        ROCK_SOURCE_TAG_LINE,
        f"source-tag: v{version}",
    )

    _run_subprocess(["rockcraft", "pack"], cwd=tmp_dir)

    rock_file = _single_glob_match(tmp_dir, "*.rock")

    registry = _registry()
    # Append a random suffix so every build gets a unique tag. Kubernetes'
    # default imagePullPolicy (IfNotPresent) means a node that already has
    # *any* image cached under a given tag will reuse it without re-pulling,
    # even if the registry's content for that tag has since changed -- e.g.
    # across repeated local test runs against the same cluster/node. A
    # unique tag per build guarantees the freshly built image is always
    # actually pulled and used.
    image_ref = f"{registry}/headscale-rock:{version}-{uuid.uuid4().hex[:8]}"
    _run_subprocess(
        [
            "skopeo",
            "copy",
            f"oci-archive:{rock_file}",
            f"docker://{image_ref}",
            "--dest-tls-verify=false",
        ],
        cwd=tmp_dir,
    )

    return image_ref
