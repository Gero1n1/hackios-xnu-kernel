/* SPDX-License-Identifier: APSL-2.0 */
#ifndef _SYS_HACKIOS_PLATFORM_H_
#define _SYS_HACKIOS_PLATFORM_H_

#include <libkern/libkern.h>
#include <pexpert/device_tree.h>

/* Qualcomm devices share the same peripheral window. Its base alone cannot
 * identify a board or authorize using another board's MMIO/power sequence.
 * This property names the loader's selected hardware profile, independently
 * of the iPhone identity presented to Apple's user space. Old loaders without
 * the property leave board-specific drivers disabled.
 */
static inline int
hackios_platform_is(const char *name)
{
	DTEntry chosen;
	const void *value = NULL;
	unsigned int size = 0;
	size_t wanted = strlen(name) + 1;

	return SecureDTLookupEntry(NULL, "/chosen", &chosen) == kSuccess &&
	       SecureDTGetProperty(chosen, "hackios-platform", &value, &size) == kSuccess &&
	       value != NULL && size == wanted && memcmp(value, name, wanted) == 0;
}

#endif
