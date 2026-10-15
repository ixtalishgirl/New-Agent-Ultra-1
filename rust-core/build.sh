#!/usr/bin/env bash
# Build and install the Haley Rust core as a Python extension via maturin.
# Usage: ./build.sh [--release]
set -euo pipefail

cd "$(dirname "$0")"

export PATH="$HOME/.cargo/bin:$PATH"

if ! command -v cargo >/dev/null 2>&1; then
  echo "Installing Rust toolchain..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
  export PATH="$HOME/.cargo/bin:$PATH"
fi

if ! command -v maturin >/dev/null 2>&1; then
  echo "Installing maturin..."
  pip install --quiet maturin
fi

MODE="--release"
if [ "${1:-}" = "--debug" ]; then
  MODE=""
fi

echo "Building haley_rust_core $MODE ..."
maturin develop $MODE

echo "Running Rust unit tests..."
cargo test --release --lib

echo "OK: haley_rust_core installed and tested."
python3 -c "import haley_rust_core; print('import OK, version', haley_rust_core.__version__)"
