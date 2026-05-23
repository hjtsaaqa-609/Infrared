#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/native/Mlx90640Native"
OUT="$SRC/build/macos"

mkdir -p "$OUT"

clang -c "$SRC/melexis/MLX90640_API.c" \
  -I "$SRC/melexis" \
  -o "$OUT/MLX90640_API.o"

clang -c "$SRC/MLX90640_I2C_Driver_Stubs.c" \
  -I "$SRC/melexis" \
  -o "$OUT/MLX90640_I2C_Driver_Stubs.o"

clang++ -std=c++17 -dynamiclib \
  -I "$SRC/melexis" \
  "$SRC/Mlx90640Native.cpp" \
  "$OUT/MLX90640_API.o" \
  "$OUT/MLX90640_I2C_Driver_Stubs.o" \
  -install_name "@rpath/libMlx90640Native.dylib" \
  -o "$OUT/libMlx90640Native.dylib"

echo "$OUT/libMlx90640Native.dylib"
