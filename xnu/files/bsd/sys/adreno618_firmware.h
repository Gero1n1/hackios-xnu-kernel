/* SPDX-License-Identifier: MIT
 * Independent A618 firmware primitives. Register/format facts: sweet KGSL
 * b2dab9baf54310ed2987689f8398a75836f3bd22, adreno_a6xx_gmu.c,
 * kgsl_gmu_core.c and a6xx_reg.h; Linux v6.12 a6xx_gmu.c legacy load path.
 * No Linux implementation code or firmware is included.
 */
#ifndef _SYS_ADRENO618_FIRMWARE_H_
#define _SYS_ADRENO618_FIRMWARE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/hackios_platform.h>

#define ADRENO618_GMU_PHYSICAL_BASE UINT64_C(0x0506a000)
#define ADRENO618_GMU_RESOURCE_BYTES 0x31000u
#define ADRENO618_GMU_ITCM_OFFSET 0x3000u
#define ADRENO618_GMU_ITCM_BYTES 0x8000u
#define ADRENO618_GMU_SYSRESET_OFFSET 0x14000u

enum adreno618_firmware_error {
	ADRENO618_FIRMWARE_OK = 0,
	ADRENO618_FIRMWARE_ARGUMENT = -1,
	ADRENO618_FIRMWARE_RESET = -2,
	ADRENO618_FIRMWARE_VERIFY = -3,
	ADRENO618_FIRMWARE_BOARD = -4
};

struct adreno618_gmu_mapping {
	volatile uint32_t *registers;
	size_t bytes;
	uint64_t physical_base;
	uint32_t chip_id; /* validated DT qcom,chipid; not an MMIO GPU-ID register */
};

/* A6xx SQE files carry one non-code word, including a630_sqe.fw used by A618
 * and a660_sqe.fw used by redwood. No chip-specific instructions are decoded.
 */
struct adreno_sqe_view {
	const uint8_t *code;
	size_t bytes;
	uint32_t file_header; /* raw word, not asserted to be a version */
};

static inline uint32_t
adreno618_le32(const uint8_t *data)
{
	return (uint32_t)data[0] | ((uint32_t)data[1] << 8) |
	       ((uint32_t)data[2] << 16) | ((uint32_t)data[3] << 24);
}

static inline bool
adreno618_disjoint(const void *source, size_t bytes,
    const volatile void *destination, size_t destination_bytes)
{
	uintptr_t a = (uintptr_t)source, b = (uintptr_t)destination;
	return bytes <= UINTPTR_MAX - a && destination_bytes <= UINTPTR_MAX - b &&
	       (a + bytes <= b || b + destination_bytes <= a);
}

/* max_file_bytes is the caller's allocation bound, not a hardware limit.
 * The immutable source and output must not overlap. On failure *out is intact.
 */
static inline bool
adreno_sqe_code(const void *firmware, size_t bytes, size_t max_file_bytes,
    struct adreno_sqe_view *out)
{
	struct adreno_sqe_view view;
	if (firmware == NULL || out == NULL || bytes < 8 || (bytes & 3u) != 0 ||
	    bytes > max_file_bytes || bytes > UINT32_MAX ||
	    !adreno618_disjoint(firmware, bytes, out, sizeof(*out))) {
		return false;
	}
	view.code = (const uint8_t *)firmware + 4;
	view.bytes = bytes - 4;
	view.file_header = adreno618_le32((const uint8_t *)firmware);
	*out = view;
	return true;
}

static inline uint32_t
adreno618_native_le32(uint32_t value)
{
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
	return __builtin_bswap32(value);
#else
	return value;
#endif
}

static inline void
adreno618_complete_io(void)
{
#if defined(__aarch64__) || defined(__arm64__)
	__asm__ volatile("dsb sy" ::: "memory");
#else
	__atomic_thread_fence(__ATOMIC_SEQ_CST);
#endif
}

/* Implementation shared with inert host fixtures. The kernel binding below
 * calls this exact function; all board/layout/input checks precede MMIO.
 */
static inline int
adreno618_gmu_upload_checked(const struct adreno618_gmu_mapping *mapping,
    const void *firmware, size_t firmware_bytes, bool verify)
{
	volatile uint32_t *registers;
	const uint8_t *source = (const uint8_t *)firmware;
	size_t i;
	if (!hackios_platform_is("sweet")) {
		return ADRENO618_FIRMWARE_BOARD;
	}
	if (mapping == NULL || mapping->registers == NULL || firmware == NULL ||
	    ((uintptr_t)mapping->registers & 3u) != 0 ||
	    mapping->physical_base != ADRENO618_GMU_PHYSICAL_BASE ||
	    (mapping->chip_id & UINT32_C(0xffffff00)) != UINT32_C(0x06010800) ||
	    mapping->bytes < ADRENO618_GMU_SYSRESET_OFFSET + 4u ||
	    mapping->bytes > ADRENO618_GMU_RESOURCE_BYTES || (mapping->bytes & 3u) != 0 ||
	    firmware_bytes == 0 || firmware_bytes > ADRENO618_GMU_ITCM_BYTES ||
	    (firmware_bytes & 3u) != 0 ||
	    !adreno618_disjoint(firmware, firmware_bytes, mapping->registers, mapping->bytes)) {
		return ADRENO618_FIRMWARE_ARGUMENT;
	}
	registers = mapping->registers;
	if ((adreno618_native_le32(registers[ADRENO618_GMU_SYSRESET_OFFSET / 4u]) & 1u) == 0) {
		return ADRENO618_FIRMWARE_RESET;
	}
	adreno618_complete_io();
	for (i = 0; i < firmware_bytes; i += 4u) {
		registers[(ADRENO618_GMU_ITCM_OFFSET + i) / 4u] =
		    adreno618_native_le32(adreno618_le32(source + i));
	}
	adreno618_complete_io();
	if (verify) {
		for (i = 0; i < firmware_bytes; i += 4u) {
			if (adreno618_native_le32(registers[(ADRENO618_GMU_ITCM_OFFSET + i) / 4u]) !=
			    adreno618_le32(source + i)) {
				return ADRENO618_FIRMWARE_VERIFY;
			}
		}
		adreno618_complete_io();
	}
	if ((adreno618_native_le32(registers[ADRENO618_GMU_SYSRESET_OFFSET / 4u]) & 1u) == 0) {
		return ADRENO618_FIRMWARE_RESET;
	}
	return ADRENO618_FIRMWARE_OK;
}

/* Future SM7150 power/startup binding, compiled in redwood_adreno.c, with NO
 * automatic startup caller. Refuses any /chosen/hackios-platform except sweet.
 * mapping must be an already-powered, accessible, Device mapping of the real
 * GMU resource at 0x0506a000, not the GPU base 0x05000000. Caller must validate
 * physical mapping/chip identity, enable correct CX rails/clocks, serialize
 * startup and keep the immutable firmware alive. Metadata checks cannot prove
 * a VA maps the claimed physical address; unpowered MMIO reads can fault.
 *
 * Uploads legacy flat firmware only, <=32 KiB; CM3 SYSRESET bit 0 must already
 * be asserted and is never changed here. On readback/reset failure ITCM may
 * already be modified: keep reset held and abort startup. This does not start
 * GMU/HFI, power the core, authenticate zap, map SMMU or submit GPU commands.
 */
#ifdef __cplusplus
extern "C" {
#endif
int adreno618_gmu_upload(const struct adreno618_gmu_mapping *mapping,
    const void *firmware, size_t firmware_bytes, bool verify);
#ifdef __cplusplus
}
#endif

#endif
