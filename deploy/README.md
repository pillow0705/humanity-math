# Deployment contract

`CI` validates pull requests and pushes. Only successful `push` runs on `main` can enter the `production` environment and deploy. Fork pull requests do not receive production credentials or execute on production infrastructure.

## One-time server configuration

A trusted administrator installs `release.py` root-owned as `/usr/local/sbin/humanity-deploy` and `ssh-entry.py` as `/usr/local/bin/humanity-deploy-entry`. The dedicated deployment account's key uses the forced command and SSH `restrict` option; it cannot open a shell or transfer arbitrary files. Its sudo rule permits only the fixed deployment program.

`/etc/humanity-deploy.json` specifies the release directory, persistent data directory, pinned library ID, Lean/Mathlib paths and runtime environment file. Systemd API/worker units point to the `current` release. Persistent data, installed libraries and backups remain outside releases. The current installation uses one serial Lean worker and an external daily data backup timer.

The release program accepts only a full commit SHA and a bounded code archive on stdin. It rejects data directories, traversal paths and links. It builds a new virtual environment and checks the installed library identity before switching anything. It waits for the current proof job to finish, stops the worker while holding the data lock, switches an atomic symlink, restarts the API and checks the deployed SHA. Startup failure restores the previous symlink and environment. Data migrations are never run automatically. Keep the last five releases and the previous release for rollback.

Infrastructure changes to the privileged release program itself must be reviewed and installed by an administrator; application releases do not overwrite the installed handler.

## GitHub configuration

Configure the `production` environment to accept **only `main`**. Store `DEPLOY_SSH_KEY` as an environment secret, with `DEPLOY_HOST`, `DEPLOY_USER` and `DEPLOY_KNOWN_HOSTS` as environment variables. The known host key must be obtained from an authenticated administrator connection, not trusted from a CI-time key scan.

Protect `main` with required `CI passed` checks and maintainer/code-owner review. Never execute fork code with deployment secrets. Deployments use a concurrency lock and do not cancel a running release midway through its switch.

## Rollback

The automatic handler rolls back failed startup checks. For an operator rollback to a known-good commit, trigger a new main commit reverting the change; it passes through the same CI and deployment checks. An administrator may also restore the previous `current` symlink and runtime environment during an incident, restart the services and inspect `/health`.

A code rollback does not reverse research submissions or schema changes. Changes to persistent records must be backward compatible or follow a separately reviewed migration.
