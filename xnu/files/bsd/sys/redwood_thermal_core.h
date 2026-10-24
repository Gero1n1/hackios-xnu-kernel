/* SPDX-License-Identifier: APSL-2.0 */
#ifndef _SYS_REDWOOD_THERMAL_CORE_H_
#define _SYS_REDWOOD_THERMAL_CORE_H_

#include <stdbool.h>
#include <stdint.h>

#define REDWOOD_THERMAL_MAX_CONTROLLERS 2u
#define REDWOOD_THERMAL_MAX_SENSORS_PER_CONTROLLER 16u
#define REDWOOD_THERMAL_MAX_SENSORS \
	(REDWOOD_THERMAL_MAX_CONTROLLERS * REDWOOD_THERMAL_MAX_SENSORS_PER_CONTROLLER)
#define REDWOOD_THERMAL_QEMU_SENSORS 27u
#define REDWOOD_THERMAL_INVALID (-2730)
#define REDWOOD_THERMAL_STATUS_VALID (1u << 21)
#define REDWOOD_THERMAL_STATUS_TEMP_MASK 0xfffu
#define REDWOOD_THERMAL_MIN_TENTHS (-400)
#define REDWOOD_THERMAL_MAX_TENTHS 1500

enum redwood_thermal_backend {
	REDWOOD_THERMAL_BACKEND_NONE = 0,
	REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC = 1,
	REDWOOD_THERMAL_BACKEND_SWEET_TSENS = 2,
};

typedef uint32_t (*redwood_thermal_status_reader)(void *context, unsigned controller, unsigned sensor);

/* Decode only a valid, physically plausible TSENS v2 sample. */
static inline bool
redwood_thermal_decode_status(uint32_t status, int32_t *tenths)
{
	int32_t value;

	if (tenths == NULL || (status & REDWOOD_THERMAL_STATUS_VALID) == 0) {
		return false;
	}
	value = (int32_t)(status & REDWOOD_THERMAL_STATUS_TEMP_MASK);
	if ((value & 0x800) != 0) {
		value -= 0x1000;
	}
	if (value < REDWOOD_THERMAL_MIN_TENTHS || value > REDWOOD_THERMAL_MAX_TENTHS) {
		return false;
	}
	*tenths = value;
	return true;
}

/* QEMU's explicit synthetic backend: no register callback is invoked. */
static inline bool
redwood_thermal_qemu_sample(unsigned sensor, int32_t *tenths)
{
	if (tenths == NULL || sensor >= REDWOOD_THERMAL_QEMU_SENSORS) {
		return false;
	}
	*tenths = 300 + (int32_t)sensor * 2;
	return true;
}

/* Bounds used before touching any mapped TSENS register. */
static inline bool
redwood_thermal_sample_offset(unsigned sensor, unsigned sensor_count, unsigned resource_bytes,
	unsigned *offset)
{
	const unsigned status_offset = 0xa0u + 4u * sensor;

	if (offset == NULL || sensor_count == 0 || sensor_count > REDWOOD_THERMAL_MAX_SENSORS_PER_CONTROLLER ||
	    sensor >= sensor_count || status_offset > resource_bytes || resource_bytes - status_offset < sizeof(uint32_t)) {
		return false;
	}
	*offset = status_offset;
	return true;
}

/* Common selector used by the kernel and the host fixture. QEMU returns before
 * the callback can be reached; the callback is the only MMIO-facing operation. */
static inline bool
redwood_thermal_read_sample(enum redwood_thermal_backend backend, unsigned controller, unsigned sensor,
	unsigned sensor_count, unsigned resource_bytes, redwood_thermal_status_reader read_status,
	void *context, int32_t *tenths)
{
	unsigned ignored_offset;
	uint32_t status;

	if (backend == REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC) {
		if (controller != 0) {
			return false;
		}
		return redwood_thermal_qemu_sample(sensor, tenths);
	}
	if (backend != REDWOOD_THERMAL_BACKEND_SWEET_TSENS || read_status == NULL ||
	    controller >= REDWOOD_THERMAL_MAX_CONTROLLERS ||
	    !redwood_thermal_sample_offset(sensor, sensor_count, resource_bytes, &ignored_offset)) {
		return false;
	}
	status = read_status(context, controller, sensor);
	return redwood_thermal_decode_status(status, tenths);
}

static inline uint32_t
redwood_thermal_pressure_clamp(int pressure)
{
	if (pressure < 0) {
		return 0;
	}
	if (pressure > 4) {
		return 4;
	}
	return (uint32_t)pressure;
}

/* Returns -1 when there are no valid samples; callers must not treat that as cool. */
static inline int
redwood_thermal_pressure(const int32_t *samples, unsigned count)
{
	int32_t maximum = REDWOOD_THERMAL_INVALID;
	bool found = false;

	if (samples == NULL || count == 0 || count > REDWOOD_THERMAL_MAX_SENSORS) {
		return -1;
	}
	for (unsigned i = 0; i < count; i++) {
		if (samples[i] == REDWOOD_THERMAL_INVALID || samples[i] < REDWOOD_THERMAL_MIN_TENTHS ||
		    samples[i] > REDWOOD_THERMAL_MAX_TENTHS) {
			continue;
		}
		if (!found || samples[i] > maximum) {
			maximum = samples[i];
			found = true;
		}
	}
	if (!found) {
		return -1;
	}
	return (int)redwood_thermal_pressure_clamp(maximum <= 400 ? 0 : maximum <= 550 ? 1 :
	    maximum <= 700 ? 2 : maximum <= 850 ? 3 : 4);
}

#endif /* _SYS_REDWOOD_THERMAL_CORE_H_ */
