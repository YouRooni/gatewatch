# Install and update

Debian 12+ and Ubuntu 22.04+, Python 3.10+. Other systems can run the Python package with their own service manager.

## Install

Use a tagged checkout and `sudo bash scripts/install.sh hub`. For nodes, use `sudo bash scripts/install.sh agent /path/to/agent.secret.json`. See [setup](operations.md) for credentials and HTTPS.

Release bundles include a standalone installer, source, wheel, metadata and `SHA256SUMS.txt`. Verify the installer before running it. With those files in one directory:

```sh
sha256sum --check SHA256SUMS.txt
sudo python3 gatewatch-install.py install agent \
  --bundle gatewatch-1.2.0-bundle.tar.gz --config agent.secret.json
```

## Update

Update the hub first, then agents. Protocol 1 accepts legacy 0.1.0 agents; features depend on the installed adapters. Configs without `config_version` are treated as version 1. Unknown newer config/database versions are rejected.

Custom service events require hub 1.1+. Rollback to 1.0 is rejected if the database contains these cards; use a matching backup for that downgrade.

```sh
sudo /opt/gatewatch/bin/gatewatch-manage status
sudo /opt/gatewatch/bin/gatewatch-manage upgrade --version 1.2.0
sudo -u gatewatch /opt/gatewatch/bin/gatewatch doctor --online
```

Choose an exact release, or explicitly use `--version latest`. Private repository downloads require `GH_TOKEN` and `sudo --preserve-env=GH_TOKEN`. The token is not saved. Offline source delivery uses `upgrade --bundle FILE` with its checksum file, or `upgrade --source /path/to/tagged/checkout`.

Check the agent heartbeat in `/status` and deliver a synthetic event after upgrading. If an install created the config but failed before activation, fix the reported error and retry the same command. An identical config is reused. To keep an edited config, retry without `--config`.

Environments live in `/opt/gatewatch-releases`; `/opt/gatewatch` points to the active one. Updates retain configs, topics, node tokens and queued data. Configs, consistent database snapshots and integration files are backed up under `/var/backups/gatewatch` before activation. Keep an off-server backup too.

## Rollback

```sh
sudo /opt/gatewatch/bin/gatewatch-manage rollback
```

Rollback restores the previous environment and integration files while preserving database data. It rejects incompatible newer schemas; those require a matching backup and manual recovery. The 0.1.0 → 1.0.0 migration supports rollback, including journal checkpoints. Config edits are retained.

Pass additional pinned adapter packages with `--package 'package==version'`; subsequent updates retain that list. Remove a role with `gatewatch-manage uninstall agent` or `uninstall all`. Configs, data, releases and backups remain. No firewall rules are reset.

`--no-start` prepares container images without systemd; `--no-apt` uses preinstalled system dependencies. Neither flag changes protocol or schema checks.
