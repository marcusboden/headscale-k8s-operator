# Contributing

To make contributions to this charm, you'll need a working
[development setup](https://documentation.ubuntu.com/juju/3.6/howto/manage-your-deployment/#set-up-your-deployment-local-testing-and-development).

You can create an environment for development with `tox`:

```shell
tox devenv -e integration
source venv/bin/activate
```

## Testing

This project uses `tox` for managing test environments. There are some pre-configured environments
that can be used for linting and formatting code when you're preparing contributions to the charm:

```shell
tox run -e format        # update your code according to linting rules
tox run -e lint          # code style
tox run -e static        # static type checking
tox run -e unit          # unit tests
tox run -e integration   # integration tests
tox                      # runs 'format', 'lint', 'static', and 'unit' environments
```

### Integration tests

The upgrade-path integration tests (`tests/integration/test_upgrade.py`) deploy the charm
against a real Juju model and exercise the charm's actual version-upgrade logic, rather than
mocking it. To run them you need:

- `rockcraft`, `charmcraft`, and `skopeo` available on `PATH`.
- A Juju controller bootstrapped against a Kubernetes cloud (not LXD).
- The `HEADSCALE_ROCK_PATH` environment variable set to a local checkout of the
  `headscale-rock` repository.
- Optionally, `TEST_IMAGE_REGISTRY` to point at a registry other than the default
  `localhost:32000` (e.g. a MicroK8s registry) to push built rocks to.

These tests are slow, since they compile headscale from source (via `rockcraft`) once per
tested version. They also require the real upstream `juanfont/headscale` release tags
`v0.26.1`, `v0.27.0`, and `v0.28.0` to exist, since the tests build the OCI image directly
from those tags.

## Build the charm

Build the charm in this git repository using:

```shell
charmcraft pack
```

<!-- You may want to include any contribution/style guidelines in this document>
