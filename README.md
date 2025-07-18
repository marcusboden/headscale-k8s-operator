<!--
Avoid using this README file for information that is maintained or published elsewhere, e.g.:

* charmcraft.yaml > published on Charmhub
* documentation > published on (or linked to from) Charmhub
* detailed contribution guide > documentation or CONTRIBUTING.md

Use links instead.
-->

# headscale-k8s

Charmhub package name: headscale-k8s
More information: https://charmhub.io/headscale-k8s

This charm deploys headscale in kubernetes. It uses [this rock](http://github.com/marcusboden/headscale-rock) as a container.

## TODO:
-[x] policy Files Done
-[x] volume for db
-[ ] headacale actions
    -[x] auth key handling
      - [x] creation
      - [x] expiration
      - [x] listing
    -[ ] user list, delete 
      - no creation b/c OIDC
      - deletion might be nice if we want to force people to re-auth
    -[ ] remove node
    -[ ] extend node
-[ ] backup
    - stop/start or rsync_sqlite?
-[ ] metrics
    - alerts for node expiration
-[ ] headscale config check
-[ ] headscale policy check
  - i.e.
-[ ] use internal storedstate to not render conf all the time

## Other resources

<!-- If your charm is documented somewhere else other than Charmhub, provide a link separately. -->

- [Read more](https://example.com)

- [Contributing](CONTRIBUTING.md) <!-- or link to other contribution documentation -->

- See the [Juju documentation](https://documentation.ubuntu.com/juju/3.6/howto/manage-charms/) for more information about developing and improving charms.
