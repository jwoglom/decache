#!/usr/bin/env bash
# Compile the native perceptual-hash helper (replaces build.bat).
#
# Produces bin/phash from phash/phash.cpp. Works with g++ or clang++ (c++).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/phash/phash.cpp"
OUT="$HERE/bin/phash"

if [[ ! -f "$SRC" ]]; then
  echo "error: $SRC not found." >&2
  exit 1
fi

if command -v g++ >/dev/null 2>&1; then
  CXX=g++
elif command -v c++ >/dev/null 2>&1; then
  CXX=c++
elif command -v clang++ >/dev/null 2>&1; then
  CXX=clang++
else
  echo "error: no C++ compiler (g++/clang++) found." >&2
  exit 1
fi

mkdir -p "$HERE/bin"
echo "Compiling phash with $CXX..."
"$CXX" -O2 "$SRC" -o "$OUT"
echo "Built $OUT"
