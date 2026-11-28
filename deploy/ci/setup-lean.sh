#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
mkdir -p tools
LEAN_VERSION=$(python -c 'from apps.api.lean_config import LEAN_VERSION; print(LEAN_VERSION)')
MATHLIB_REVISION=$(python -c 'from apps.api.lean_config import MATHLIB_REVISION; print(MATHLIB_REVISION)')
if [ ! -x "tools/lean-${LEAN_VERSION}-linux/bin/lean" ]; then
  curl --fail --location --retry 3 "https://github.com/leanprover/lean4/releases/download/v${LEAN_VERSION}/lean-${LEAN_VERSION}-linux.tar.zst" -o tools/lean.tar.zst
  tar --zstd -xf tools/lean.tar.zst -C tools
  rm tools/lean.tar.zst
fi
export PATH="$ROOT/tools/lean-${LEAN_VERSION}-linux/bin:$PATH"
MATHLIB="$ROOT/tools/mathlib-v${LEAN_VERSION}"
if [ ! -d "$MATHLIB/.git" ]; then
  git init "$MATHLIB"
  git -C "$MATHLIB" remote add origin https://github.com/leanprover-community/mathlib4.git
  git -C "$MATHLIB" fetch --depth 1 origin "$MATHLIB_REVISION"
  git -C "$MATHLIB" checkout --detach FETCH_HEAD
fi
if [ ! -f "$MATHLIB/.lake/build/lib/lean/Mathlib.olean" ]; then
  (cd "$MATHLIB" && lake exe cache get)
fi
python -m deploy.check_mathlib
chmod -R a+rX tools
# Pin the base image in production; CI builds a fresh isolated image for its own proofs.
docker build -f deploy/Dockerfile.lean -t "math-collab-lean:$LEAN_VERSION" .
