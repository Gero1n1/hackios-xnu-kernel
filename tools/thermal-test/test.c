/* SPDX-License-Identifier: APSL-2.0 */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "../../xnu/files/bsd/sys/redwood_thermal_core.h"

struct mock_mmio {
	uint32_t words[REDWOOD_THERMAL_MAX_CONTROLLERS][REDWOOD_THERMAL_MAX_SENSORS_PER_CONTROLLER];
	unsigned reads;
};

static uint32_t
mock_read(void *context, unsigned controller, unsigned sensor)
{
	struct mock_mmio *mock = (struct mock_mmio *)context;
	assert(controller < REDWOOD_THERMAL_MAX_CONTROLLERS);
	assert(sensor < REDWOOD_THERMAL_MAX_SENSORS_PER_CONTROLLER);
	mock->reads++;
	return mock->words[controller][sensor];
}

static uint32_t
status_for(int32_t tenths, bool valid)
{
	return (valid ? REDWOOD_THERMAL_STATUS_VALID : 0u) | ((uint32_t)tenths & REDWOOD_THERMAL_STATUS_TEMP_MASK);
}

int
main(void)
{
	struct mock_mmio mock;
	int32_t sample = REDWOOD_THERMAL_INVALID;
	int32_t qemu[REDWOOD_THERMAL_QEMU_SENSORS];
	unsigned offset = 0;

	/* QEMU's synthetic readings are deterministic and never call the MMIO reader. */
	memset(&mock, 0, sizeof(mock));
	for (unsigned i = 0; i < REDWOOD_THERMAL_QEMU_SENSORS; i++) {
		assert(redwood_thermal_read_sample(REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC, 0, i,
		    0, 0, mock_read, &mock, &qemu[i]));
		assert(qemu[i] == 300 + (int32_t)i * 2);
	}
	assert(!redwood_thermal_read_sample(REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC, 0,
	    REDWOOD_THERMAL_QEMU_SENSORS, 0, 0, mock_read, &mock, &sample));
	assert(!redwood_thermal_read_sample(REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC, 1, 0,
	    0, 0, mock_read, &mock, &sample));
	assert(mock.reads == 0);

	/* Valid signed 12-bit samples, including below-zero values. */
	assert(redwood_thermal_decode_status(status_for(425, true), &sample) && sample == 425);
	assert(redwood_thermal_decode_status(status_for(-55, true), &sample) && sample == -55);
	assert(!redwood_thermal_decode_status(status_for(425, false), &sample));
	assert(!redwood_thermal_decode_status(status_for(-2048, true), &sample));
	assert(!redwood_thermal_decode_status(status_for(1501, true), &sample));
	assert(!redwood_thermal_decode_status(status_for(2047, true), &sample));
	assert(!redwood_thermal_decode_status(REDWOOD_THERMAL_STATUS_VALID, NULL));

	/* A resource may be absent or truncated; only validated offsets are readable. */
	assert(redwood_thermal_sample_offset(0, 16, 0x1ff, &offset) && offset == 0xa0);
	assert(redwood_thermal_sample_offset(15, 16, 0x1ff, &offset) && offset == 0xdc);
	assert(!redwood_thermal_sample_offset(16, 16, 0x1ff, &offset));
	assert(!redwood_thermal_sample_offset(0, 0, 0x1ff, &offset));
	assert(!redwood_thermal_sample_offset(0, 17, 0x1ff, &offset));
	assert(!redwood_thermal_sample_offset(15, 16, 0xdf, &offset));

	/* Invalid/missing sensors are excluded; an all-invalid set is unknown, never cool. */
	{
		int32_t values[] = { 330, REDWOOD_THERMAL_INVALID, 710, 1501 };
		assert(redwood_thermal_pressure(values, 4) == 3);
		values[0] = REDWOOD_THERMAL_INVALID;
		values[2] = REDWOOD_THERMAL_INVALID;
		assert(redwood_thermal_pressure(values, 4) == -1);
		assert(redwood_thermal_pressure(NULL, 4) == -1);
		assert(redwood_thermal_pressure(values, REDWOOD_THERMAL_MAX_SENSORS + 1) == -1);
	}

	/* Threshold edges and clamping of externally supplied pressure values. */
	{
		const int32_t thresholds[] = { 400, 401, 550, 551, 700, 701, 850, 851, 900, 901 };
		const int expected[] = { 0, 1, 1, 2, 2, 3, 3, 4, 4, 4 };
		for (unsigned i = 0; i < sizeof(thresholds) / sizeof(thresholds[0]); i++) {
			assert(redwood_thermal_pressure(&thresholds[i], 1) == expected[i]);
		}
		assert(redwood_thermal_pressure_clamp(-3) == 0);
		assert(redwood_thermal_pressure_clamp(0) == 0);
		assert(redwood_thermal_pressure_clamp(4) == 4);
		assert(redwood_thermal_pressure_clamp(99) == 4);
	}

	/* The callback is reached only after the sweet backend's layout and bounds checks. */
	mock.words[1][0] = status_for(365, true);
	assert(redwood_thermal_read_sample(REDWOOD_THERMAL_BACKEND_SWEET_TSENS, 1, 0,
	    16, 0x1ff, mock_read, &mock, &sample) && sample == 365);
	assert(mock.reads == 1);
	assert(!redwood_thermal_read_sample(REDWOOD_THERMAL_BACKEND_SWEET_TSENS, 1, 16,
	    16, 0x1ff, mock_read, &mock, &sample));
	assert(!redwood_thermal_read_sample(REDWOOD_THERMAL_BACKEND_SWEET_TSENS, 1, 15,
	    16, 0xdf, mock_read, &mock, &sample));
	assert(!redwood_thermal_read_sample(REDWOOD_THERMAL_BACKEND_NONE, 1, 0,
	    16, 0x1ff, mock_read, &mock, &sample));
	assert(mock.reads == 1);

	puts("thermal core: QEMU synthetic, TSENS validation, bounds, pressure thresholds passed");
	return 0;
}
