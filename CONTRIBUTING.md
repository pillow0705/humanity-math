# Contributing

Use a focused branch and explain the concrete problem, resulting behavior and validation in the pull request. Run the checks in README before submitting. CI status checks and maintainer review are required before merging.

Preserve public IDs, proof dependencies, mathematical statements and backward-compatible API behavior. Keep new agent-facing explanatory text in English. Both UI languages should remain usable. Include regression tests when changing task dispatch, scoring, file transactions, credential handling or formal verification.

Use synthetic records in tests. Never add production research data, account records, private keys, access tokens or server backups. Keep user data outside release directories. Schema and Lean/Mathlib upgrades require an explicit migration plan; the deployer intentionally rejects an unprovisioned Lean environment.

Frontend files are served directly; no bundler is required. The dependency graph uses a compact endpoint and fetches full proofs only on demand. File record caches must see other processes' writes and must not expose mutable shared objects.
