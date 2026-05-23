#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>

extern "C" {
#include "MLX90640_API.h"
}

#if defined(_WIN32)
#define MLX_EXPORT extern "C" __declspec(dllexport)
#else
#define MLX_EXPORT extern "C"
#endif

struct MlxContext {
    paramsMLX90640 params;
};

MLX_EXPORT MlxContext* MlxCreateContext()
{
    auto* ctx = static_cast<MlxContext*>(std::calloc(1, sizeof(MlxContext)));
    return ctx;
}

MLX_EXPORT void MlxDestroyContext(MlxContext* ctx)
{
    std::free(ctx);
}

MLX_EXPORT int MlxExtractParameters(MlxContext* ctx, const uint16_t* eepromData, int wordCount)
{
    if (ctx == nullptr || eepromData == nullptr || wordCount < MLX90640_EEPROM_DUMP_NUM) {
        return -1001;
    }

    uint16_t local[MLX90640_EEPROM_DUMP_NUM];
    std::memcpy(local, eepromData, sizeof(local));
    return MLX90640_ExtractParameters(local, &ctx->params);
}

MLX_EXPORT float MlxGetTa(MlxContext* ctx, const uint16_t* frameData, int wordCount)
{
    if (ctx == nullptr || frameData == nullptr || wordCount < 834) {
        return NAN;
    }

    uint16_t local[834];
    std::memcpy(local, frameData, sizeof(local));
    return MLX90640_GetTa(local, &ctx->params);
}

MLX_EXPORT int MlxCalculateTo(MlxContext* ctx, const uint16_t* frameData, int wordCount, float emissivity, float tr, float* to768)
{
    if (ctx == nullptr || frameData == nullptr || to768 == nullptr || wordCount < 834) {
        return -1002;
    }

    uint16_t local[834];
    std::memcpy(local, frameData, sizeof(local));
    MLX90640_CalculateTo(local, &ctx->params, emissivity, tr, to768);
    return MLX90640_GetSubPageNumber(local);
}

MLX_EXPORT void MlxBadPixelsCorrection(MlxContext* ctx, const uint16_t* frameData, int wordCount, float* to768)
{
    if (ctx == nullptr || frameData == nullptr || to768 == nullptr || wordCount < 834) {
        return;
    }

    uint16_t local[834];
    std::memcpy(local, frameData, sizeof(local));
    const int mode = (local[832] & MLX90640_CTRL_MEAS_MODE_MASK) >> MLX90640_CTRL_MEAS_MODE_SHIFT;
    MLX90640_BadPixelsCorrection(ctx->params.brokenPixels, to768, mode, &ctx->params);
    MLX90640_BadPixelsCorrection(ctx->params.outlierPixels, to768, mode, &ctx->params);
}
