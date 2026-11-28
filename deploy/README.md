# Deployment contract

`CI` validates pull requests and pushes. Only successful `push` runs on `main` can enter the `production` environment and deploy. Fork pull requests do not receive production credentials or execute on production infrastructure.

## One-time server configuration

A trusted administrator installs `release.py` root-owned as `/usr/local/sbin/humanity-deploy` and `ssh-entry.py` as `/usr/local/bin/humanity-deploy-entry`. The dedicated deployment account's key uses the forced command and SSH `restrict` option; it cannot open a shell or transfer arbitrary files. Its sudo rule permits only the fixed deployment program.

`/etc/humanity-deploy.json` specifies the release directory, persistent data directory, pinned library ID, Lean/Mathlib paths and runtime environment file. Systemd API/worker units point to the `current` release. Persistent data, installed libraries and backups remain outside releases. The current installation uses one serial Lean worker and an external daily data backup timer.

The release program accepts only a full commit SHA and a bounded code archive on stdin. It rejects data directories, traversal paths and links. It builds a new virtual environment and checks the installed library identity before switching anything. It waits for the current proof job to finish, stops the worker while holding the data lock, switches an atomic symlink, restarts the API and checks the deployed SHA. Startup failure restores the previous symlink and environment. Data migrations are never run automatically. Keep the last five releases and the previous release for rollback.

Infrastructure changes to the privileged release program itself must be reviewed and installed by an administrator; application releases do not overwrite the installed handler.

## Lean 4.36.0 rollout

Keep the Lean 4.19 installation and deployment settings for rollback. Before merging this upgrade, provision Lean 4.36.0 and the locked Mathlib release in a separate directory. Use `deploy/ci/setup-lean.sh` in a trusted Linux release staging copy. It downloads the pinned sources and cache, seals the installation and builds the sandbox image. Run the real Lean integration suite on that installation. Configure production's `lean_home`, `mathlib_home` and `library_id` in `/etc/humanity-deploy.json`. Set `LEAN_HOME`, `MATHLIB_HOME` and `LEAN_IMAGE` for the runtime units. `LEAN_MEMORY_MB` configures the per-container memory limit (default 4096 MB). On the 8 GB production host, use 6144 MB with the existing single worker; the public environment descriptor reports the configured limit. Rehearse cold imports of the full Mathlib library before cutover. Obtain the library ID from `platform-install.json` in the sealed Mathlib directory. The release preflight checks both the mounted executable and Mathlib.

Use an operator maintenance window. Stop the API and worker and take a consistent private data backup, including the old caches and records. From the trusted new release, preview the migration on a copy of that data with the service account and its Python environment:

```sh
python -m deploy.migrate_lean_environment --data /path/to/private/data-copy
python -m deploy.migrate_lean_environment --data /path/to/private/data-copy --apply
```

Review the counts and run the worker against the copy before applying the same command to persistent data during the cutover. Keep maintenance active until GitHub deploys the new release. The migration archives old target versions and queues the same definitions and propositions in dependency order when they only import other eligible targets. Targets with mismatch flags or unavailable proof dependencies remain available for repair. Old proof sources, IDs, audit reports and cache files are retained. Old compiled proofs cannot earn current-environment coverage or serve as current dependencies. Contributors can reuse the original source when re-verifying against a new target. Existing final problem statements and their historical review receipts are preserved.

Verify `/health` identifies the merged release, `/api/v1/lean/environment` reports 4.36.0 and the sealed library ID, and the worker can check a target and an exact-target proof against isolated test data. For rollback, restore the prior release and runtime paths together. If the new data migration has already run, restore the pre-cutover backup before reopening submissions, or reconcile intervening writes with the operator before restoring any data.

## GitHub configuration

Configure the `production` environment to accept **only `main`**. Store `DEPLOY_SSH_KEY` as an environment secret, with `DEPLOY_HOST`, `DEPLOY_USER` and `DEPLOY_KNOWN_HOSTS` as environment variables. The known host key must be obtained from an authenticated administrator connection, not trusted from a CI-time key scan.

Protect `main` with required `CI passed` checks and maintainer/code-owner review. Never execute fork code with deployment secrets. Deployments use a concurrency lock and do not cancel a running release midway through its switch.

## Rollback

The automatic handler rolls back failed startup checks. For an operator rollback to a known-good commit, trigger a new main commit reverting the change; it passes through the same CI and deployment checks. An administrator may also restore the previous `current` symlink and runtime environment during an incident, restart the services and inspect `/health`.

A code rollback does not reverse research submissions or schema changes. Changes to persistent records must be backward compatible or follow a separately reviewed migration.
