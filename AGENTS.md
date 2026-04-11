## Bootstrap behavior
Maintain a single bootstrap entrypoint for the repository.

The bootstrap script must support:
- install
- uninstall

Uninstall must only remove RemnaShop-Pro resources and must never affect unrelated Docker Compose projects on the host.

In interactive mode, destructive uninstall actions must require confirmation.
In non-interactive mode, uninstall behavior must be explicit and documented.
