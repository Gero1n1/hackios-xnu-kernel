/*
 * A bounded TSENS status reader and a deliberately synthetic QEMU backend.
 *
 * thermal=1 opts in. QEMU is identified by /chosen/hackios-platform and gets
 * deterministic synthetic samples; that path never maps or reads MMIO. The
 * sweet path requires the selected sweet profile and validates both TSENS
 * nodes' Device Tree resources before mapping them. It does not initialize
 * TSENS, calibrate its sensors, or apply thermal throttling to CPU clocks.
 */

#include <kern/clock.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include <libkern/OSByteOrder.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <pexpert/device_tree.h>
#include <pexpert/pexpert.h>
#include <sys/errno.h>
#include <sys/hackios_platform.h>
#include <sys/proc.h>
#include <sys/redwood_thermal_core.h>
#include <sys/systm.h>
#include <sys/sysctl.h>
#include <vm/vm_kern.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);

#define TSENS_TMIN_RESOURCE 0x1ffu
#define TSENS_SROT_RESOURCE 0x8u
#define THERMAL_TEXT_BYTES 4096u
#define THERMAL_PERIOD_MS 1000u

struct tsens_controller {
	const char *path;
	uint32_t srot_phys;
	uint32_t tm_phys;
	uint32_t count;
	uint32_t tm_bytes;
	vm_offset_t tm;
};

static struct tsens_controller controllers[REDWOOD_THERMAL_MAX_CONTROLLERS] = {
	{ "/soc/tsens@c222000", 0x0c222000u, 0x0c263000u, 0, 0, 0 },
	{ "/soc/tsens@c223000", 0x0c223000u, 0x0c265000u, 0, 0, 0 },
};

static enum redwood_thermal_backend backend = REDWOOD_THERMAL_BACKEND_NONE;
static bool thermal_active;
static uint32_t thermal_samples_valid;
static uint32_t thermal_running;
static uint32_t thermal_pressure;
static int32_t temperatures[REDWOOD_THERMAL_MAX_SENSORS];
static char thermal_text[THERMAL_TEXT_BYTES] = "thermal service disabled";
static thread_t thermal_thread;

static LCK_GRP_DECLARE(thermal_lck_grp, "redwood-thermal");
static LCK_MTX_DECLARE(thermal_lock, &thermal_lck_grp);

static bool
property_has_string(const void *property, unsigned bytes, const char *wanted)
{
	const char *cursor = (const char *)property;
	unsigned remaining = bytes;
	size_t wanted_bytes = strlen(wanted) + 1;

	while (property != NULL && remaining != 0) {
		size_t length = strnlen(cursor, remaining);
		if (length == remaining) {
			return false;
		}
		if (length + 1 == wanted_bytes && memcmp(cursor, wanted, wanted_bytes) == 0) {
			return true;
		}
		cursor += length + 1;
		remaining -= (unsigned)(length + 1);
	}
	return false;
}

/*
 * Read only the TM range described by the exact sweet DT node. The Android
 * live FDT has one-cell addresses/sizes here and contains SROT then TM ranges.
 * The 16-sensor fallback is the TSENS v2 hardware maximum; the live DT has no
 * #qcom,sensors property, so this is a bounded scan, not a claim that all 16
 * physical channels are populated.
 */
static bool
map_sweet_controller(struct tsens_controller *controller)
{
	DTEntry entry;
	const void *compatible = NULL, *reg = NULL, *sensor_count = NULL;
	unsigned compatible_bytes = 0, reg_bytes = 0, sensor_count_bytes = 0;
	uint32_t count = REDWOOD_THERMAL_MAX_SENSORS_PER_CONTROLLER;
	const uint8_t *cells;

	if (controller == NULL || SecureDTLookupEntry(NULL, controller->path, &entry) != kSuccess ||
	    SecureDTGetProperty(entry, "compatible", &compatible, &compatible_bytes) != kSuccess ||
	    !property_has_string(compatible, compatible_bytes, "qcom,tsens24xx") ||
	    SecureDTGetProperty(entry, "reg", &reg, &reg_bytes) != kSuccess || reg == NULL || reg_bytes != 16) {
		return false;
	}
	cells = (const uint8_t *)reg;
	if (OSReadBigInt32(cells, 0) != controller->srot_phys ||
	    OSReadBigInt32(cells, 4) != TSENS_SROT_RESOURCE ||
	    OSReadBigInt32(cells, 8) != controller->tm_phys ||
	    OSReadBigInt32(cells, 12) != TSENS_TMIN_RESOURCE) {
		return false;
	}
	if (SecureDTGetProperty(entry, "#qcom,sensors", &sensor_count, &sensor_count_bytes) == kSuccess) {
		if (sensor_count == NULL || sensor_count_bytes != sizeof(uint32_t)) {
			return false;
		}
		count = OSReadBigInt32(sensor_count, 0);
		if (count == 0 || count > REDWOOD_THERMAL_MAX_SENSORS_PER_CONTROLLER) {
			return false;
		}
	}
	controller->count = count;
	controller->tm_bytes = TSENS_TMIN_RESOURCE;
	controller->tm = ml_io_map((vm_offset_t)controller->tm_phys, controller->tm_bytes);
	return controller->tm != 0;
}

static uint32_t
read_sweet_status(void *context __unused, unsigned controller_index, unsigned sensor_index)
{
	struct tsens_controller *controller;
	unsigned offset;
	/* redwood_thermal_read_sample() has already checked these bounds. */
	controller = &controllers[controller_index];
	(void)redwood_thermal_sample_offset(sensor_index, controller->count, controller->tm_bytes, &offset);
	return *(volatile uint32_t *)(controller->tm + offset);
}

static int
thermal_sample_count(void)
{
	if (backend == REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC) {
		return (int)REDWOOD_THERMAL_QEMU_SENSORS;
	}
	if (backend == REDWOOD_THERMAL_BACKEND_SWEET_TSENS) {
		return (int)(controllers[0].count + controllers[1].count);
	}
	return 0;
}

static void
sample_all(int32_t *out, unsigned capacity)
{
	unsigned index = 0;

	for (unsigned i = 0; i < capacity; i++) {
		out[i] = REDWOOD_THERMAL_INVALID;
	}
	if (backend == REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC) {
		for (unsigned i = 0; i < REDWOOD_THERMAL_QEMU_SENSORS && i < capacity; i++) {
			(void)redwood_thermal_read_sample(backend, 0, i, 0, 0, read_sweet_status, NULL, &out[i]);
		}
		return;
	}
	if (backend != REDWOOD_THERMAL_BACKEND_SWEET_TSENS) {
		return;
	}
	for (unsigned controller = 0; controller < REDWOOD_THERMAL_MAX_CONTROLLERS; controller++) {
		for (unsigned sensor = 0; sensor < controllers[controller].count && index < capacity; sensor++) {
			int32_t value = REDWOOD_THERMAL_INVALID;
			if (controllers[controller].tm != 0 && redwood_thermal_read_sample(backend, controller, sensor,
			    controllers[controller].count, controllers[controller].tm_bytes, read_sweet_status, NULL, &value)) {
				out[index] = value;
			}
			index++;
		}
	}
}

static void
format_thermal_text(const int32_t *samples, unsigned count, enum redwood_thermal_backend which, bool samples_valid)
{
	static const char *const names[] = { "disabled", "qemu-synthetic", "sweet-tsens" };
	unsigned used;
	int n;

	used = 0;
	n = snprintf(thermal_text, sizeof(thermal_text), "backend=%s samples=%s", names[which],
	    samples_valid ? "valid" : "unavailable");
	if (n < 0 || (unsigned)n >= sizeof(thermal_text)) {
		thermal_text[sizeof(thermal_text) - 1] = '\0';
		return;
	}
	used = (unsigned)n;
	for (unsigned i = 0; i < count && used < sizeof(thermal_text) - 1; i++) {
		int32_t value = samples[i];
		if (value == REDWOOD_THERMAL_INVALID) {
			n = snprintf(thermal_text + used, sizeof(thermal_text) - used, " Sensor%u=invalid", i);
		} else {
			int32_t fraction = value % 10;
			if (fraction < 0) {
				fraction = -fraction;
			}
			n = snprintf(thermal_text + used, sizeof(thermal_text) - used,
			    " Sensor%u=%d.%dC", i, value / 10, fraction);
		}
		if (n < 0 || (unsigned)n >= sizeof(thermal_text) - used) {
			used = sizeof(thermal_text) - 1;
			break;
		}
		used += (unsigned)n;
	}
	thermal_text[used] = '\0';
}

static void
thermal_thread_func(void *arg __unused, wait_result_t wr __unused)
{
	int32_t samples[REDWOOD_THERMAL_MAX_SENSORS];
	int count;

	while (thermal_active) {
		int pressure;
		count = thermal_sample_count();
		sample_all(samples, REDWOOD_THERMAL_MAX_SENSORS);
		pressure = redwood_thermal_pressure(samples, (unsigned)count);

		lck_mtx_lock(&thermal_lock);
		memcpy(temperatures, samples, sizeof(temperatures));
		thermal_samples_valid = pressure >= 0;
		if (pressure >= 0) {
			thermal_pressure = redwood_thermal_pressure_clamp(pressure);
		}
		format_thermal_text(temperatures, (unsigned)count, backend, thermal_samples_valid);
		lck_mtx_unlock(&thermal_lock);
		delay_for_interval(THERMAL_PERIOD_MS, NSEC_PER_MSEC);
	}
}

void
redwood_thermal_init(void)
{
	uint32_t enabled = 0;

	if (!PE_parse_boot_argn("thermal", &enabled, sizeof(enabled)) || enabled == 0) {
		return;
	}
	if (hackios_platform_is("qemu-virt")) {
		backend = REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC;
	} else if (hackios_platform_is("sweet") && map_sweet_controller(&controllers[0]) &&
	    map_sweet_controller(&controllers[1])) {
		backend = REDWOOD_THERMAL_BACKEND_SWEET_TSENS;
	} else {
		printf("redwood_thermal: thermal=1 requested, but no supported platform/resource was verified\n");
		return;
	}
	thermal_active = true;
	if (kernel_thread_start(thermal_thread_func, NULL, &thermal_thread) != KERN_SUCCESS) {
		thermal_active = false;
		backend = REDWOOD_THERMAL_BACKEND_NONE;
		printf("redwood_thermal: could not start service thread\n");
		return;
	}
	thread_deallocate(thermal_thread);
	thermal_running = 1;
	printf("redwood_thermal: service started (%s, %d bounded samples; physical readings unverified)\n",
	    backend == REDWOOD_THERMAL_BACKEND_QEMU_SYNTHETIC ? "QEMU synthetic" : "sweet TSENS",
	    thermal_sample_count());
}

static int
thermal_string_sysctl(struct sysctl_oid *oidp, void *arg1, int arg2, struct sysctl_req *req)
{
	int error;
	lck_mtx_lock(&thermal_lock);
	error = sysctl_handle_string(oidp, arg1, arg2, req);
	lck_mtx_unlock(&thermal_lock);
	return error;
}

static int
thermal_pressure_sysctl(struct sysctl_oid *oidp, __unused void *arg1,
	__unused int arg2, struct sysctl_req *req)
{
	int error, value;
	lck_mtx_lock(&thermal_lock);
	if (!thermal_samples_valid) {
		lck_mtx_unlock(&thermal_lock);
		return EAGAIN;
	}
	value = (int)thermal_pressure;
	error = sysctl_io_number(req, value, sizeof(value), &value, NULL);
	lck_mtx_unlock(&thermal_lock);
	return error;
}

SYSCTL_DECL(_kern_redwood);
SYSCTL_INT(_kern_redwood, OID_AUTO, thermal_running, CTLFLAG_RD | CTLFLAG_LOCKED,
	&thermal_running, 0, "thermal sampling thread active");
SYSCTL_PROC(_kern_redwood, OID_AUTO, thermal_pressure, CTLTYPE_INT | CTLFLAG_RD | CTLFLAG_LOCKED,
	NULL, 0, thermal_pressure_sysctl, "I", "thermal pressure 0-4; EAGAIN if sensors have no valid samples");
SYSCTL_INT(_kern_redwood, OID_AUTO, thermal_samples_valid, CTLFLAG_RD | CTLFLAG_LOCKED,
	&thermal_samples_valid, 0, "1 when the latest thermal sample set contains valid sensors");
SYSCTL_PROC(_kern_redwood, OID_AUTO, thermal, CTLTYPE_STRING | CTLFLAG_RD | CTLFLAG_LOCKED,
	thermal_text, sizeof(thermal_text), thermal_string_sysctl, "A", "validated TSENS or QEMU synthetic samples");
