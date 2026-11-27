#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
mkdir -p tools
if [ ! -x tools/lean-4.19.0-linux/bin/lean ]; then
  curl --fail --location --retry 3 https://github.com/leanprover/lean4/releases/download/v4.19.0/lean-4.19.0-linux.tar.zst -o tools/lean.tar.zst
  tar --zstd -xf tools/lean.tar.zst -C tools
  rm tools/lean.tar.zst
fi
export PATH="$ROOT/tools/lean-4.19.0-linux/bin:$PATH"
MATHLIB="$ROOT/tools/mathlib-v4.19.0"
if [ ! -d "$MATHLIB/.git" ]; then
  git init "$MATHLIB"
  git -C "$MATHLIB" remote add origin https://github.com/leanprover-community/mathlib4.git
  git -C "$MATHLIB" fetch --depth 1 origin c44e0c8ee63ca166450922a373c7409c5d26b00b
  git -C "$MATHLIB" checkout --detach FETCH_HEAD
fi
if [ ! -f "$MATHLIB/.lake/build/lib/lean/Mathlib.olean" ]; then
  (cd "$MATHLIB" && lake exe cache get)
fi
python -m deploy.check_mathlib
chmod -R a+rX tools
# Pin the base image in production; CI builds a fresh isolated image for its own proofs.
docker build -f deploy/Dockerfile.lean -t math-collab-lean:4.19.0 .
