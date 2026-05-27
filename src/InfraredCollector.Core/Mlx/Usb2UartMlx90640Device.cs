using InfraredCollector.Core.Configuration;
using InfraredCollector.Core.Devices;
using InfraredCollector.Core.Util;
using System.Text;

namespace InfraredCollector.Core.Mlx;

public sealed class Usb2UartMlx90640Device : IDisposable
{
    private readonly uint _usbIndex;
    private readonly byte _i2cAddress;
    private readonly int _chunkWords;
    private readonly double _refreshRateHz;
    private readonly byte _refreshRateBits;
    private bool _opened;

    public Usb2UartMlx90640Device(uint usbIndex, AppConfig config)
    {
        _usbIndex = usbIndex;
        _i2cAddress = config.MlxI2cAddress;
        _chunkWords = Math.Clamp(config.MlxReadChunkWords, 1, 128);
        _refreshRateHz = config.MlxRefreshRateHz;
        _refreshRateBits = Mlx90640Constants.RefreshRateBitsFromHz(_refreshRateHz);
        ClockStretchLevel = (uint)config.I2cClockStretchLevel;
    }

    public uint UsbIndex => _usbIndex;
    public uint ClockStretchLevel { get; }
    public double RefreshRateHz => _refreshRateHz;
    public string BoardUid { get; private set; } = "";

    public void OpenAndConfigure()
    {
        ThrowIfError(Usb2UartNative.OpenUsb(_usbIndex), "OpenUsb");
        _opened = true;

        var uid = new byte[64];
        if (Usb2UartNative.GetBoardInformation(uid, out _, out _, _usbIndex) >= 0) {
            BoardUid = DecodeUid(uid);
        }

        ThrowIfError(Usb2UartNative.ConfigIICParam(Mlx90640Constants.IicRate1M, ClockStretchLevel, _usbIndex), "ConfigIICParam");
        ThrowIfError(Usb2UartNative.IICCheckSlaveAddr(0, _i2cAddress, _usbIndex), $"IICCheckSlaveAddr 0x{_i2cAddress:X2}");
    }

    public ushort[] ReadEeprom()
    {
        return ReadWords(Mlx90640Constants.EepromStartAddress, Mlx90640Constants.EepromWords);
    }

    public ushort ReadWord(int register)
    {
        return ReadWords(register, 1)[0];
    }

    public void WriteWord(int register, ushort value)
    {
        WriteWordViaRawI2c(register, value);
    }

    public MlxStatusClearResult ClearStatusRegister()
    {
        var before = ReadWord(Mlx90640Constants.StatusRegister);
        WriteWordViaRawI2c(Mlx90640Constants.StatusRegister, Mlx90640Constants.InitStatusValue);
        Thread.Sleep(2);
        var afterRawWrite = ReadWord(Mlx90640Constants.StatusRegister);
        if (!Mlx90640Registers.IsDataReady(afterRawWrite)) {
            return new MlxStatusClearResult(before, afterRawWrite, "raw-i2c");
        }

        WriteWordViaRegisterApi(Mlx90640Constants.StatusRegister, Mlx90640Constants.InitStatusValue);
        Thread.Sleep(2);
        var afterRegisterWrite = ReadWord(Mlx90640Constants.StatusRegister);
        if (!Mlx90640Registers.IsDataReady(afterRegisterWrite)) {
            return new MlxStatusClearResult(before, afterRegisterWrite, "register-api");
        }

        throw new InvalidOperationException(
            $"Failed to clear MLX90640 status register on USB index {_usbIndex}. " +
            $"before=0x{before:X4}, after raw write=0x{afterRawWrite:X4}, after register-api write=0x{afterRegisterWrite:X4}.");
    }

    private void WriteWordViaRawI2c(int register, ushort value)
    {
        var buf = new[]
        {
            (byte)(_i2cAddress << 1),
            (byte)(register >> 8),
            (byte)register,
            (byte)(value >> 8),
            (byte)value
        };
        ThrowIfError(Usb2UartNative.IICSendData(1, 1, buf, (uint)buf.Length, _usbIndex), $"I2C write 0x{register:X4}");
    }

    private void WriteWordViaRegisterApi(int register, ushort value)
    {
        var reg = RegisterBytes(register);
        var payload = new[]
        {
            (byte)(value >> 8),
            (byte)value
        };
        ThrowIfError(Usb2UartNative.IICRegisterSend(0, _i2cAddress, reg, payload, (byte)reg.Length, (uint)payload.Length, _usbIndex), $"IICRegisterSend 0x{register:X4}");
    }

    public ushort[] ReadWords(int startRegister, int wordCount)
    {
        var words = new ushort[wordCount];
        var offset = 0;
        while (offset < wordCount) {
            var count = Math.Min(_chunkWords, wordCount - offset);
            ReadWordsChunk(startRegister + offset, words, offset, count);
            offset += count;
        }

        return words;
    }

    public MlxOperatingModeResult ConfigureOperatingMode()
    {
        var chess = ConfigureChessMode();
        var resolution = ConfigureResolution18Bit();
        var refresh = ConfigureRefreshRate();
        var lowBits = ConfigureMacVerifiedLowBits();
        return new MlxOperatingModeResult(chess, resolution, refresh, lowBits, _refreshRateHz, _refreshRateBits);
    }

    private MlxControlRegisterResult ConfigureChessMode()
    {
        var current = ReadWord(Mlx90640Constants.ControlRegister);
        var updated = Mlx90640Registers.WithChessMode(current);
        if (updated != current) {
            WriteWord(Mlx90640Constants.ControlRegister, updated);
            Thread.Sleep(2);
        }

        var verify = ReadWord(Mlx90640Constants.ControlRegister);
        if (!Mlx90640Registers.IsChessMode(verify)) {
            throw new InvalidOperationException(
                $"Failed to set MLX90640 chess mode on USB index {_usbIndex}. " +
                $"control before=0x{current:X4}, target=0x{updated:X4}, after=0x{verify:X4}.");
        }

        return new MlxControlRegisterResult(current, updated, verify);
    }

    private MlxControlRegisterResult ConfigureResolution18Bit()
    {
        var current = ReadWord(Mlx90640Constants.ControlRegister);
        var updated = Mlx90640Registers.WithResolution(current, Mlx90640Constants.Resolution18Bit);
        if (updated != current) {
            WriteWord(Mlx90640Constants.ControlRegister, updated);
            Thread.Sleep(2);
        }

        var verify = ReadWord(Mlx90640Constants.ControlRegister);
        if (Mlx90640Registers.Resolution(verify) != Mlx90640Constants.Resolution18Bit) {
            throw new InvalidOperationException(
                $"Failed to set MLX90640 ADC resolution to 18-bit on USB index {_usbIndex}. " +
                $"control before=0x{current:X4}, target=0x{updated:X4}, after=0x{verify:X4}, " +
                $"after resolution bits={Mlx90640Registers.Resolution(verify)}.");
        }

        return new MlxControlRegisterResult(current, updated, verify);
    }

    private MlxControlRegisterResult ConfigureRefreshRate()
    {
        var current = ReadWord(Mlx90640Constants.ControlRegister);
        var updated = Mlx90640Registers.WithRefreshRate(current, _refreshRateBits);
        if (updated != current) {
            WriteWord(Mlx90640Constants.ControlRegister, updated);
            Thread.Sleep(2);
        }

        var verify = ReadWord(Mlx90640Constants.ControlRegister);
        if (Mlx90640Registers.RefreshRate(verify) != _refreshRateBits) {
            throw new InvalidOperationException(
                $"Failed to set MLX90640 refresh rate to {_refreshRateHz:g} Hz on USB index {_usbIndex}. " +
                $"control before=0x{current:X4}, target=0x{updated:X4}, after=0x{verify:X4}, " +
                $"after refresh bits={Mlx90640Registers.RefreshRate(verify)}.");
        }

        return new MlxControlRegisterResult(current, updated, verify);
    }

    private MlxControlRegisterResult ConfigureMacVerifiedLowBits()
    {
        var current = ReadWord(Mlx90640Constants.ControlRegister);
        var updated = Mlx90640Registers.WithMacVerifiedLowBits(current);
        if (updated != current) {
            WriteWord(Mlx90640Constants.ControlRegister, updated);
            Thread.Sleep(2);
        }

        var verify = ReadWord(Mlx90640Constants.ControlRegister);
        if ((verify & Mlx90640Constants.ControlLowBitsMask) != Mlx90640Constants.ControlLowBitsMacVerified) {
            throw new InvalidOperationException(
                $"Failed to set MLX90640 control low bits on USB index {_usbIndex}. " +
                $"control before=0x{current:X4}, target=0x{updated:X4}, after=0x{verify:X4}.");
        }

        return new MlxControlRegisterResult(current, updated, verify);
    }

    public MlxRawSubpage ReadSubpage(CancellationToken cancellationToken)
    {
        ushort status;
        var polls = 0;
        do {
            cancellationToken.ThrowIfCancellationRequested();
            status = ReadWord(Mlx90640Constants.StatusRegister);
            polls++;
            if (!Mlx90640Registers.IsDataReady(status)) {
                Thread.Sleep(2);
            }
        } while (!Mlx90640Registers.IsDataReady(status));

        var clear = ClearStatusRegister();
        var pixels = ReadWords(Mlx90640Constants.PixelDataStartAddress, Mlx90640Constants.PixelWords);
        var aux = ReadWords(Mlx90640Constants.AuxDataStartAddress, Mlx90640Constants.AuxWords);
        var control = ReadWord(Mlx90640Constants.ControlRegister);
        var frameData = Mlx90640FrameData.Compose(pixels, aux, control, status);

        return new MlxRawSubpage(East8Clock.Now(), status, control, polls, clear.After, clear.Method, frameData);
    }

    public void Dispose()
    {
        if (_opened) {
            Usb2UartNative.CloseUsb(_usbIndex);
            _opened = false;
        }
    }

    private void ReadWordsChunk(int startRegister, ushort[] target, int wordOffset, int wordCount)
    {
        var reg = RegisterBytes(startRegister);
        var bytes = new byte[wordCount * 2];
        ThrowIfError(Usb2UartNative.IICRegisterRead(0, _i2cAddress, reg, bytes, (byte)reg.Length, (uint)bytes.Length, _usbIndex), $"IICRegisterRead 0x{startRegister:X4}");

        for (var i = 0; i < wordCount; i++) {
            target[wordOffset + i] = (ushort)((bytes[i * 2] << 8) | bytes[i * 2 + 1]);
        }
    }

    private static byte[] RegisterBytes(int register)
    {
        return [(byte)(register >> 8), (byte)register];
    }

    private static void ThrowIfError(int rc, string operation)
    {
        if (rc < 0) {
            throw new InvalidOperationException($"{operation} failed with USB2UART return code {rc}.");
        }
    }

    private static string DecodeUid(byte[] uid)
    {
        var nul = Array.IndexOf(uid, (byte)0);
        var len = nul >= 0 ? nul : uid.Length;
        var useful = uid.Take(len).ToArray();
        if (useful.Length >= 4 && useful.All(b => b is >= 0x20 and <= 0x7E)) {
            var text = Encoding.ASCII.GetString(useful).Trim();
            if (!string.IsNullOrWhiteSpace(text)) {
                return text;
            }
        }

        var nonZero = useful.Where(b => b != 0).DefaultIfEmpty().ToArray();
        return Convert.ToHexString(nonZero.Take(16).ToArray());
    }
}

public sealed record MlxRawSubpage(DateTimeOffset TimestampUtc, ushort StatusRegister, ushort ControlRegister, int DataReadyPolls, ushort StatusAfterClear, string StatusClearMethod, ushort[] FrameData);
public sealed record MlxControlRegisterResult(ushort Before, ushort Target, ushort After);
public sealed record MlxOperatingModeResult(
    MlxControlRegisterResult Chess,
    MlxControlRegisterResult Resolution,
    MlxControlRegisterResult Refresh,
    MlxControlRegisterResult LowBits,
    double RefreshRateHz,
    byte RefreshRateBits);
public sealed record MlxStatusClearResult(ushort Before, ushort After, string Method);
