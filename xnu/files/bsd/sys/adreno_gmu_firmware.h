/* SPDX-License-Identifier: APSL-2.0 */
#ifndef _SYS_ADRENO_GMU_FIRMWARE_H_
#define _SYS_ADRENO_GMU_FIRMWARE_H_
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

static inline uint32_t
adreno_gmu_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
	       (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static inline bool
adreno_gmu_range_inside(uint32_t address, uint32_t size, uint32_t base, uint32_t capacity)
{
	return size != 0 && address >= base && address - base < capacity &&
	       size <= capacity - (address - base);
}

/* Block-format GMU images only, not A618's legacy flat ITCM image. Validate
 * the entire immutable input before allocating pages or writing firmware.
 * Payloads must fit in one TCM region or be wholly outside both regions.
 */
static inline bool
adreno_gmu_blocks_valid(const void *firmware, size_t bytes,
    uint32_t itcm, uint32_t dtcm, uint32_t capacity)
{
	const uint8_t *data = (const uint8_t *)firmware;
	size_t at = 0;
	bool payload = false;
	if (data == NULL || bytes == 0 || capacity == 0) {
		return false;
	}
	while (at < bytes) {
		uint32_t address, size;
		uint64_t end;
		if (bytes - at < 16) {
			return false;
		}
		address = adreno_gmu_le32(data + at);
		size = adreno_gmu_le32(data + at + 4);
		at += 16;
		if (size > bytes - at) {
			return false;
		}
		end = (uint64_t)address + size;
		if (size != 0) {
			bool touches_itcm = address < (uint64_t)itcm + capacity && end > itcm;
			bool touches_dtcm = address < (uint64_t)dtcm + capacity && end > dtcm;
			if (((address | size) & 3) != 0 || end > UINT64_C(0x100000000) ||
			    ((touches_itcm || touches_dtcm) &&
			    !adreno_gmu_range_inside(address, size, itcm, capacity) &&
			    !adreno_gmu_range_inside(address, size, dtcm, capacity))) {
				return false;
			}
			payload = true;
		}
		at += size;
	}
	return payload;
}
#endif
