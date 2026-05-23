# Mlx90640Native

This project wraps the official Melexis MLX90640 C algorithm in a small x64 DLL for the C# GUI.

The files under `melexis/` are copied from the official Apache-2.0 licensed repository:

https://github.com/melexis/mlx90640-library

Only the calculation path is used here. I2C functions are stubbed because the C# side performs USB2UARTPSIIIC reads and passes EEPROM/frameData into the native algorithm.
