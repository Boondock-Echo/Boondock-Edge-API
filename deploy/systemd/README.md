# Appliance API restart setup

These files provision the systemd appliance restart helper. Copy the entire
`deploy/systemd/` directory into the API repository; its release workflow must
include `deploy/` in the API archive.

After creating the API service and reloading systemd, the root-run appliance
installer invokes:

```sh
bash deploy/systemd/install-api-restart.sh boondock-edge-api.service
```

Use the actual existing API unit name. Its `User=` must be a persistent,
unprivileged account, with `DynamicUser=no`. The setup requires the `sudo`
package for `visudo`. It installs:

| File | Owner | Mode |
| --- | --- | --- |
| `/usr/local/sbin/boondock-edge-api-restart` | root:root | 0755 |
| `/etc/systemd/system/boondock-edge-api-restart.service` | root:root | 0644 |
| `/etc/sudoers.d/boondock-edge-api-restart` | root:root | 0440 |

The sudo rule allows the API account to invoke only the fixed helper, with no
arguments. The helper starts a separate root-owned oneshot unit without blocking
the HTTP response. That unit waits three seconds and restarts the configured API
service. Concurrent starts of the same oneshot unit coalesce. Setup does not
restart the API or enable the helper to run at boot.

The API restart integration invokes this fixed argument list:

```python
["sudo", "-n", "/usr/local/sbin/boondock-edge-api-restart"]
```

The API remains responsible for administrator authorization, operation locking,
restart status, cooldowns, and its restart HTTP endpoint. Installing this helper
does not add those application endpoints. The current published API routes need
the restart integration from the original API patch to use this helper.

This setup targets a host running systemd. A Docker container needs a deployment
restart mechanism coordinated with its host; the systemd helper does not manage
container lifecycle or mount the Docker socket.
