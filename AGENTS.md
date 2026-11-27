# Working in this repository

Read README.md and CONTRIBUTING.md before changing code.

- Never put production data or credentials in this repository or CI logs.
- Preserve published IDs, statements, dependency chains and external persistent data.
- Keep agent collaboration text in English and maintain both frontend languages.
- The production Lean/Mathlib version is pinned. Do not upgrade it as part of an unrelated change.
- Run API, store and release tests; run real Lean integration tests for verifier or formal-contract changes.
- PR checks run on disposable GitHub-hosted runners. Never run untrusted PR code on production or grant it deployment secrets.
- Production deployment happens only after a passing main-branch CI run. Deployment infrastructure is installed separately and is not automatically replaced by repository code.
