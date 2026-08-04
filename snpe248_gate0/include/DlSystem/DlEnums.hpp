//==============================================================================
//
//  Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
//  All rights reserved.
//  Confidential and Proprietary - Qualcomm Technologies, Inc.
//
//==============================================================================
#pragma once
#include "Wrapper.hpp"

#include <cstdint>

namespace DlSystem {
/** @addtogroup c_plus_plus_apis C++
@{ */

/**
 * Enumeration of supported target runtimes.
 */
enum class Runtime_t
{
  /// Special value indicating the property is unset.
  UNSET = -1,

  /// Run the processing on Snapdragon CPU.
  /// Data: float 32bit
  /// Math: float 32bit
  CPU_FLOAT32  = 0,

  /// Default legacy enum to retain backward compatibility.
  /// CPU = CPU_FLOAT32
  CPU = CPU_FLOAT32,

  /// Run the processing on the Adreno GPU.
  /// Data: float 16bit
  /// Math: float 32bit
  GPU_FLOAT32_16_HYBRID = 1,

  /// Default legacy enum to retain backward compatibility.
  /// GPU = GPU_FLOAT32_16_HYBRID
  GPU = GPU_FLOAT32_16_HYBRID,

  /// Run the processing on the Hexagon DSP.
  /// Data: 8bit fixed point Tensorflow style format
  /// Math: 8bit fixed point Tensorflow style format
  DSP_FIXED8_TF = 2,

  /// Default legacy enum to retain backward compatibility.
  /// DSP = DSP_FIXED8_TF
  DSP = DSP_FIXED8_TF,

  /// Run the processing on the Adreno GPU.
  /// Data: float 16bit
  /// Math: float 16bit
  GPU_FLOAT16 = 3,

  /// Run the processing on Snapdragon AIX+HVX.
  /// Data: 8bit fixed point Tensorflow style format
  /// Math: 8bit fixed point Tensorflow style format
  AIP_FIXED8_TF = 5,

  AIP_FIXED_TF = AIP_FIXED8_TF,

  /// Any new enums should be added above this line
  NUM_RUNTIME_TARGETS
};

/**
 * Enumeration of runtime available check options.
 */
enum class RuntimeCheckOption_t
{
  /// Perform standard runtime available check
  NORMAL_CHECK = 0,

  /// Perform basic runtime available check, may be runtime specific
  BASIC_CHECK = 1,

  /// Perform unsignedPD runtime available check
  UNSIGNEDPD_CHECK = 2,

  /// Perform standard runtime available check
  DEFAULT = 2,

  /// Any new enums should be added above this line
  NUM_RUNTIMECHECK_OPTIONS
};

/**
 * Enumeration of various performance profiles that can be requested.
 */
enum class PerformanceProfile_t
{
  /// Run in a standard mode.
  /// This mode will be deprecated in the future and replaced with BALANCED.
  DEFAULT = 0,
  /// Run in a balanced mode.
  BALANCED = 0,

  /// Run in high performance mode
  HIGH_PERFORMANCE = 1,

  /// Run in a power sensitive mode, at the expense of performance.
  POWER_SAVER = 2,

  /// Use system settings.  SNPE makes no calls to any performance related APIs.
  SYSTEM_SETTINGS = 3,

  /// Run in sustained high performance mode
  SUSTAINED_HIGH_PERFORMANCE = 4,

  /// Run in burst mode
  BURST = 5,

  /// Run in lower clock than POWER_SAVER, at the expense of performance.
  LOW_POWER_SAVER = 6,

  /// Run in higher clock and provides better performance than POWER_SAVER.
  HIGH_POWER_SAVER = 7,

  /// Run in lower balanced mode
  LOW_BALANCED = 8,

  /// Run in lowest clock at the expense of performance
  EXTREME_POWER_SAVER = 9,

  /// Any new enums should be added above this line
  NUM_PERF_PROFILES
};


enum class  DspPerfVoltageCorner_t
{
  /// Maps to HAP_DCVS_VCORNER_DISABLE.
  /// Disable setting up voltage corner
  DCVS_VOLTAGE_CORNER_DISABLE = 0x10,

  /// Maps to HAP_DCVS_VCORNER_SVS2.
  /// Set voltage corner to minimum value supported on platform
  DCVS_VOLTAGE_VCORNER_MIN_VOLTAGE_CORNER = 0x20,

  /// Maps to HAP_DCVS_VCORNER_SVS2.
  /// Set voltage corner to SVS2 value for the platform
  DCVS_VOLTAGE_VCORNER_SVS2 = 0x30,

  /// Maps to HAP_DCVS_VCORNER_SVS.
  /// Set voltage corner to SVS value for the platform
  DCVS_VOLTAGE_VCORNER_SVS = 0x40,

  /// Maps to HAP_DCVS_VCORNER_SVS_PLUS.
  /// Set voltage corner to SVS_PLUS value for the platform
  DCVS_VOLTAGE_VCORNER_SVS_PLUS = 0x50,

  /// Maps to HAP_DCVS_VCORNER_NOM.
  /// Set voltage corner to NOMINAL value for the platform
  DCVS_VOLTAGE_VCORNER_NOM = 0x60,

  /// Maps to HAP_DCVS_VCORNER_NOM_PLUS.
  /// Set voltage corner to NOMINAL_PLUS value for the platform
  DCVS_VOLTAGE_VCORNER_NOM_PLUS = 0x70,

  /// Maps to HAP_DCVS_VCORNER_TURBO.
  /// Set voltage corner to TURBO value for the platform
  DCVS_VOLTAGE_VCORNER_TURBO = 0x80,

  /// Maps to HAP_DCVS_VCORNER_TURBO_PLUS.
  /// Set voltage corner to TURBO_PLUS value for the platform
  DCVS_VOLTAGE_VCORNER_TURBO_PLUS = 0x90,

  //currently the Turbol1 is mapped to Turbo l2 in soc definition
  DCVS_VOLTAGE_VCORNER_TURBO_L1 = 0x92,
  /// Set voltage corner to TURBO_L2 value for the platform
  DCVS_VOLTAGE_VCORNER_TURBO_L2 = DCVS_VOLTAGE_VCORNER_TURBO_L1,
  /// Set voltage corner to TURBO_L3 value for the platform
  DCVS_VOLTAGE_VCORNER_TURBO_L3 = 0x93,
  /// Set voltage corner to TURBO_L4 value for the platform
  DCVS_VOLTAGE_VCORNER_TURBO_L4 = 0x94,
  /// Set voltage corner to TURBO_L5 value for the platform
  DCVS_VOLTAGE_VCORNER_TURBO_L5 = 0x95,

  /// Maps to HAP_DCVS_VCORNER_MAX.
  /// Set voltage corner to maximum value supported on the platform
  DCVS_VOLTAGE_VCORNER_MAX_VOLTAGE_CORNER = 0xA0,

  /// UNKNOWN value that must not be used by client
  DCVS_VOLTAGE_VCORNER_UNKNOWN = 0x7fffffff
};

/**
 * @brief Expanded voltage corners (2-byte range, 0x100+) for fine-grained
 * power/performance control. Used by DCVS_V3_EXP and CENG_EXP options.
 * Values below 0x100 (except DISABLE=0) are invalid in expanded structs.
 * Requires V85+ with CDSP expanded-corner support; unsupported targets
 * return QNN_HTP_PERF_INFRASTRUCTURE_ERROR_UNSUPPORTED.
 */
enum class DspPerfExpVoltageCorner_t {
  /// Maps to HAP_DCVS_EXP_VCORNER_DISABLE.
  /// Disable setting up voltage corner
  DCVS_EXP_VCORNER_DISABLE = 0,
  /// Maps to HAP_DCVS_EXP_VCORNER_MIN.
  /// Set voltage corner to minimum value supported on platform
  DCVS_EXP_VCORNER_MIN = 0x100,
  /// Maps to HAP_DCVS_EXP_VCORNER_MIN_SVS.
  /// Set voltage corner to MIN_SVS value for the platform
  DCVS_EXP_VCORNER_MIN_SVS = 0x130,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_D3.
  /// Set voltage corner to LOWSVS_D3 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_D3 = 0x132,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_D2_1.
  /// Set voltage corner to LOWSVS_D2_1 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_D2_1 = 0x133,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_D2.
  /// Set voltage corner to LOWSVS_D2 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_D2 = 0x134,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_D1_1.
  /// Set voltage corner to LOWSVS_D1_1 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_D1_1 = 0x136,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_D1.
  /// Set voltage corner to LOWSVS_D1 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_D1 = 0x138,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_D0.
  /// Set voltage corner to LOWSVS_D0 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_D0 = 0x13C,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS.
  /// Set voltage corner to LOWSVS value for the platform
  DCVS_EXP_VCORNER_LOW_SVS = 0x140,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_P1.
  /// Set voltage corner to LOWSVS_P1 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_P1 = 0x148,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_L0.
  /// Set voltage corner to LOWSVS_L0 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_L0 = 0x14C,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_L1.
  /// Set voltage corner to LOWSVS_L1 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_L1 = 0x150,
  /// Maps to HAP_DCVS_EXP_VCORNER_LOW_SVS_L2.
  /// Set voltage corner to LOWSVS_L2 value for the platform
  DCVS_EXP_VCORNER_LOW_SVS_L2 = 0x160,
  /// Maps to HAP_DCVS_EXP_VCORNER_SVS.
  /// Set voltage corner to SVS value for the platform
  DCVS_EXP_VCORNER_SVS = 0x180,
  /// Maps to HAP_DCVS_EXP_VCORNER_SVS_L0.
  /// Set voltage corner to SVS_L0 value for the platform
  DCVS_EXP_VCORNER_SVS_L0 = 0x190,
  /// Maps to HAP_DCVS_EXP_VCORNER_SVS_L1.
  /// Set voltage corner to SVS_L1 value for the platform
  DCVS_EXP_VCORNER_SVS_L1 = 0x1C0,
  /// Maps to HAP_DCVS_EXP_VCORNER_SVS_L2.
  /// Set voltage corner to SVS_L2 value for the platform
  DCVS_EXP_VCORNER_SVS_L2 = 0x1E0,
  /// Maps to HAP_DCVS_EXP_VCORNER_NOM.
  /// Set voltage corner to NOM value for the platform
  DCVS_EXP_VCORNER_NOM = 0x200,
  /// Maps to HAP_DCVS_EXP_VCORNER_NOM_L0.
  /// Set voltage corner to NOM_L0 value for the platform
  DCVS_EXP_VCORNER_NOM_L0 = 0x220,
  /// Maps to HAP_DCVS_EXP_VCORNER_NOM_L1.
  /// Set voltage corner to NOM_L1 value for the platform
  DCVS_EXP_VCORNER_NOM_L1 = 0x240,
  /// Maps to HAP_DCVS_EXP_VCORNER_NOM_L2.
  /// Set voltage corner to NOM_L2 value for the platform
  DCVS_EXP_VCORNER_NOM_L2 = 0x250,
  /// Maps to HAP_DCVS_EXP_VCORNER_TUR.
  /// Set voltage corner to TURBO value for the platform
  DCVS_EXP_VCORNER_TUR = 0x280,
  /// Maps to HAP_DCVS_EXP_VCORNER_TUR_L0.
  /// Set voltage corner to TURBO_L0 value for the platform
  DCVS_EXP_VCORNER_TUR_L0 = 0x290,
  /// Maps to HAP_DCVS_EXP_VCORNER_TUR_L1.
  /// Set voltage corner to TURBO_L1 value for the platform
  DCVS_EXP_VCORNER_TUR_L1 = 0x2A0,
  /// Maps to HAP_DCVS_EXP_VCORNER_TUR_L2.
  /// Set voltage corner to TURBO_L2 value for the platform
  DCVS_EXP_VCORNER_TUR_L2 = 0x2B0,
  /// Maps to HAP_DCVS_EXP_VCORNER_TUR_L3.
  /// Set voltage corner to TURBO_L3 value for the platform
  DCVS_EXP_VCORNER_TUR_L3 = 0x2C0,
  /// Maps to HAP_DCVS_EXP_VCORNER_TUR_L4.
  /// Set voltage corner to TURBO_L4 value for the platform
  DCVS_EXP_VCORNER_TUR_L4 = 0x2C4,
  /// Maps to HAP_DCVS_EXP_VCORNER_TUR_L5.
  /// Set voltage corner to TURBO_L5 value for the platform
  DCVS_EXP_VCORNER_TUR_L5 = 0x2C8,
  /// Maps to HAP_DCVS_EXP_VCORNER_SUPER_TURBO.
  /// Set voltage corner to SUPER_TURBO value for the platform
  DCVS_EXP_VCORNER_SUPER_TURBO = 0x2D0,
  /// Maps to HAP_DCVS_EXP_VCORNER_SUPER_TURBO_NO_CPR.
  /// Set voltage corner to SUPER_TURBO_NO_CPR value for the platform
  DCVS_EXP_VCORNER_SUPER_TURBO_NO_CPR = 0x2E0,
  /// Maps to HAP_DCVS_EXP_VCORNER_MAX.
  /// Selects the maximum voltage corner defined for the chipset
  DCVS_EXP_VCORNER_MAX = 0xFFFF,
  /// UNKNOWN value that must not be used by client
  DCVS_EXP_VCORNER_UNKNOWN = 0x7fffffff
};

enum class DspPerfPowerMode_t
{
  /// Maps to HAP_DCVS_V2_ADJUST_UP_DOWN.
  /// Allows for DCVS to adjust up and down
  DSP_PERF_INFRASTRUCTURE_POWERMODE_ADJUST_UP_DOWN = 0x1,

  /// Maps to HAP_DCVS_V2_ADJUST_ONLY_UP.
  /// Allows for DCVS to adjust up only
  DSP_PERF_INFRASTRUCTURE_POWERMODE_ADJUST_ONLY_UP = 0x2,

  /// Maps to HAP_DCVS_V2_POWER_SAVER_MODE.
  /// Higher thresholds for power efficiency
  DSP_PERF_INFRASTRUCTURE_POWERMODE_POWER_SAVER_MODE = 0x4,

  /// Maps to HAP_DCVS_V2_POWER_SAVER_AGGRESSIVE_MODE.
  /// Higher thresholds for power efficiency with faster ramp down
  DSP_PERF_INFRASTRUCTURE_POWERMODE_POWER_SAVER_AGGRESSIVE_MODE = 0x8,

  /// Maps to HAP_DCVS_V2_PERFORMANCE_MODE.
  /// Lower thresholds for maximum performance
  DSP_PERF_INFRASTRUCTURE_POWERMODE_PERFORMANCE_MODE = 0x10,

  /// Maps to HAP_DCVS_V2_DUTY_CYCLE_MODE.
  /// The below value applies only for HVX clients:
  ///  - For streaming class clients:
  ///    - detects periodicity based on HVX usage
  ///    - lowers clocks in the no HVX activity region of each period.
  ///  - For compute class clients:
  ///    - Lowers clocks on no HVX activity detects and brings clocks up on detecting
  ///      HVX activity again.
  ///    - Latency involved in bringing up the clock will be at max 1 to 2 ms.
  DSP_PERF_INFRASTRUCTURE_POWERMODE_DUTY_CYCLE_MODE = 0x20,

  /// UNKNOWN value that must not be used by client
  DSP_PERF_INFRASTRUCTURE_POWERMODE_UNKNOWN = 0x7fffffff
};

/**
 *  @brief Perf modes to specify clock frequency level within a target
 *  voltage corner. Applies to HMX_V2, CENG_EXP (cengPerfMode), and
 *  DCVS_V3_EXP (corePerfMode, busPerfMode). D1-D15 levels require
 *  expanded corner support (V85+ with HAP_dcvs_config); on legacy
 *  paths only HIGH/LOW are honoured.
 */
enum class DspClkPerfMode_t {
  // To select max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH = 0,
  // To select min frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_LOW,
  // To select one level down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D1,
  // To select two levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D2,
  // To select three levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D3,
  // To select four levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D4,
  // To select five levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D5,
  // To select six levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D6,
  // To select seven levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D7,
  // To select eight levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D8,
  // To select nine levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D9,
  // To select ten levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D10,
  // To select eleven levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D11,
  // To select twelve levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D12,
  // To select thirteen levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D13,
  // To select fourteen levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D14,
  // To select fifteen levels down from max frequency at target voltage corner.
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_HIGH_D15,
  // Range boundary — not a voteable perf mode value. Used for input validation
  // only.
  // Valid perf modes are HIGH (0), LOW (1), and D1-D15 (2-16).
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_MAX = 0xFF,
  /// UNKNOWN value that must not be used by client
  DSP_PERF_INFRASTRUCTURE_CLK_PERF_UNKNOWN = 0x7fffffff
};

/**
 * Enumeration of various profilngLevels that can be requested.
 */
enum class ProfilingLevel_t
{
  /// No profiling.
  /// Collects no runtime stats in the DiagLog
  OFF = 0,

  /// Basic profiling
  /// Collects some runtime stats in the DiagLog
  BASIC = 1,

  /// Detailed profiling
  /// Collects more runtime stats in the DiagLog, including per-layer statistics
  /// Performance may be impacted
  DETAILED = 2,

  /// Moderate profiling
  /// Collects more runtime stats in the DiagLog, no per-layer statistics
  MODERATE = 3,

  /// Linting profiling
  /// HTP exclusive profiling level that collects in-depth performance metrics
  /// for each op in the graph including main thread execution time and time spent
  /// on parallel background ops
  LINTING = 4,

  /// QHAS profiling
  /// HTP Exclusive profiling level that enables QHAS and gives additional
  /// performance metrics
  QHAS = 5,

  /// Any new enums should be added above this line
  NUM_PROFILING_LEVELS
};

/**
 * Enumeration of various execution priority hints.
 */
enum class ExecutionPriorityHint_t
{
  /// Usage of the priorities apart from LOW may be restricted
  /// Between Normal Low and Normal High
  NORMAL = 0,

  /// Between Normal High and High Plus
  HIGH = 1,

  /// Lowest priority
  LOW = 2,

  /// Between Normal and High priority
  NORMAL_HIGH = 3,

  /// Between Low and Normal
  NORMAL_LOW = 4,

  /// Between High and Critical
  HIGH_PLUS = 5,

  /// Between High Plus and Critical Plus
  CRITICAL = 6,

  /// Highest priority
  CRITICAL_PLUS = 7,

  /// Any new enums should be added above this line
  NUM_EXECUTION_PRIORITY_HINTS
};

/** @} */ /* end_addtogroup c_plus_plus_apis C++*/

/**
 * Enumeration that lists the supported image encoding formats.
 */
enum class ImageEncoding_t
{
  /// For unknown image type. Also used as a default value for ImageEncoding_t.
  UNKNOWN = 0,

  /// The RGB format consists of 3 bytes per pixel: one byte for
  /// Red, one for Green, and one for Blue. The byte ordering is
  /// endian independent and is always in RGB byte order.
  RGB = 1,

  /// The ARGB32 format consists of 4 bytes per pixel: one byte for
  /// Red, one for Green, one for Blue, and one for the alpha channel.
  /// The alpha channel is ignored. The byte ordering depends on the
  /// underlying CPU. For little endian CPUs, the byte order is BGRA.
  /// For big endian CPUs, the byte order is ARGB.
  ARGB32 = 2,

  /// The RGBA format consists of 4 bytes per pixel: one byte for
  /// Red, one for Green, one for Blue, and one for the alpha channel.
  /// The alpha channel is ignored. The byte ordering is endian independent
  /// and is always in RGBA byte order.
  RGBA = 3,

  /// The GRAYSCALE format is for 8-bit grayscale.
  GRAYSCALE = 4,

  /// NV21 is the Android version of YUV. The Chrominance is down
  /// sampled and has a subsampling ratio of 4:2:0. Note that this
  /// image format has 3 channels, but the U and V channels
  /// are subsampled. For every four Y pixels there is one U and one V pixel.
  NV21 = 5,

  /// The BGR format consists of 3 bytes per pixel: one byte for
  /// Red, one for Green and one for Blue. The byte ordering is
  /// endian independent and is always BGR byte order.
  BGR = 6
};

/**
 * Enumeration that lists the supported LogLevels that can be set by users.
 */
enum class LogLevel_t
{
  /// Enumeration variable to be used by user to set logging level to FATAL.
  LOG_FATAL = 0,

  /// Enumeration variable to be used by user to set logging level to ERROR.
  LOG_ERROR = 1,

  /// Enumeration variable to be used by user to set logging level to WARN.
  LOG_WARN = 2,

  /// Enumeration variable to be used by user to set logging level to INFO.
  LOG_INFO = 3,

  /// Enumeration variable to be used by user to set logging level to VERBOSE.
  LOG_VERBOSE = 4,

  /// Any new enums should be added above this line
  NUM_LOG_LEVELS
};

enum class IOBufferDataType_t : int
{
  /// Unspecified
  UNSPECIFIED = 0,

  /// 32-bit floating point
  FLOATING_POINT_32 = 1,

  /// 16-bit floating point
  FLOATING_POINT_16 = 2,

  /// 8-bit fixed point
  FIXED_POINT_8 =  3,

  /// 16-bit fixed point
  FIXED_POINT_16 = 4,

  /// int32
  INT_32 = 5,

  ///unsigned int32
  UINT_32 = 6,

  /// int8
  INT_8 =  7,

  /// uint8
  UINT_8 = 8,

  /// int16
  INT_16 = 9,

  /// uint16
  UINT_16 = 10,

  /// boolean
  BOOL_8 = 11,

  /// int64
  INT_64 = 12,

  /// uint64
  UINT_64 = 13
};

enum class CacheCompatibility_t : int
{
  /// A binary cache is compatible if it could run on the device. This is the
  /// default.
  CACHE_COMPATIBILITY_PERMISSIVE = 0,
  /// A binary cache is compatible if it could run on the device and fully
  /// utilize hardware capability.
  CACHE_COMPATIBILITY_STRICT = 1,
  /// A binary cache is always incompatible. SNPE will generate new cache.
  CACHE_COMPATIBILITY_ALWAYS_GENERATE_NEW_CACHE = 2
};

enum class HtaPerfPowerMode_t
{
  /// default mode
  HTA_POWERMODE_DEFAULT = 0,
  /// low power saver mode
  HTA_POWERMODE_LOW_POWER_SAVER = 1,
  /// power saver mode
  HTA_POWERMODE_POWER_SAVER = 2,
  /// high power saver mode
  HTA_POWERMODE_HIGH_POWER_SAVER = 3,
  /// balanced mode
  HTA_POWERMODE_BALANCED = 4,
  /// high performance mode
  HTA_POWERMODE_HIGH_PERFORMANCE = 5,
  /// burst mode
  HTA_POWERMODE_BURST = 6,
  /// UNKNOWN value that must not be used by client
  HTA_POWERMODE_UNKNOWN = 0x7fffffff
};

enum class GpuPerfHint_t
{
  /// Sets the GPU performance hint to high performance, this is the default
  GPU_PERF_HINT_HIGH = 0,
  /// Sets the GPU performance hint to normal performance
  GPU_PERF_HINT_NORMAL = 1,
  /// Sets the GPU performance hint to low performance
  GPU_PERF_HINT_LOW = 2
};

enum class DspHmx_ExpVoltageCorner_t
{
  DCVS_EXP_VCORNER_DISABLE    = 0,

  DCVS_EXP_VCORNER_MIN        = 0x100,

  DCVS_EXP_VCORNER_LOW_SVS_D2 = 0x134,

  DCVS_EXP_VCORNER_LOW_SVS_D1 = 0x138,

  DCVS_EXP_VCORNER_LOW_SVS    = 0x140,

  DCVS_EXP_VCORNER_LOW_SVS_L0 = 0x14C,

  DCVS_EXP_VCORNER_SVS        = 0x180,

  DCVS_EXP_VCORNER_SVS_L0     = 0x190,

  DCVS_EXP_VCORNER_SVS_L1     = 0x1C0,

  DCVS_EXP_VCORNER_SVS_L2     = 0x1E0,

  DCVS_EXP_VCORNER_NOM        = 0x200,

  DCVS_EXP_VCORNER_NOM_L1     = 0x240,

  DCVS_EXP_VCORNER_NOM_L2     = 0x250,

  DCVS_EXP_VCORNER_TUR        = 0x280,

  DCVS_EXP_VCORNER_TUR_L1     = 0x2A0,

  DCVS_EXP_VCORNER_TUR_L2     = 0x2B0,

  DCVS_EXP_VCORNER_TUR_L3     = 0x2C0,

  DCVS_EXP_VCORNER_MAX        = 0xFFFF,

};

enum class DspHmx_ClkPerfMode_t
{
  HMX_CLK_PERF_HIGH=0,

  HMX_CLK_PERF_LOW=1

};

enum class DeferredInit_t
{
  SNPE_DEFERRED_INIT_NONE = 0,

  SNPE_DEFERRED_INIT_WEIGHTS = 1,

  SNPE_DEFERRED_INIT_SPILL_FILL = 2,

  SNPE_DEFERRED_INIT_ALL = 3
};

typedef bool DspPerfDcvsEnable_t;

typedef uint32_t DspPerfSleepLatency_t;

typedef uint32_t DspPerfSleepDisable_t;

typedef uint32_t DspPerfRpcPollingTime_t;

typedef uint32_t DspPerfAdaptivePollingTime_t;

typedef uint32_t DspPerfHysteresisTime_t;

typedef bool  DspPerfAsyncVoteEnable_t;

typedef bool HighPerformanceModeEnabled_t;

typedef bool FastInitModeEnabled_t;

typedef uint32_t DspPerfHmxTimeoutTime_t;

} // ns DlSystem


ALIAS_IN_ZDL_NAMESPACE(DlSystem, Runtime_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, RuntimeCheckOption_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, PerformanceProfile_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, ProfilingLevel_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, ExecutionPriorityHint_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, ImageEncoding_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, LogLevel_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, IOBufferDataType_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, CacheCompatibility_t)

ALIAS_IN_ZDL_NAMESPACE(DlSystem, HtaPerfPowerMode_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, GpuPerfHint_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DeferredInit_t)

ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfVoltageCorner_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfExpVoltageCorner_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfPowerMode_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspClkPerfMode_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspHmx_ExpVoltageCorner_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspHmx_ClkPerfMode_t)

ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfDcvsEnable_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfSleepLatency_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfSleepDisable_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfRpcPollingTime_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfAdaptivePollingTime_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfHysteresisTime_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfAsyncVoteEnable_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, DspPerfHmxTimeoutTime_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, HighPerformanceModeEnabled_t)
ALIAS_IN_ZDL_NAMESPACE(DlSystem, FastInitModeEnabled_t)