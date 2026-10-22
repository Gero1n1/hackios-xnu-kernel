#include <assert.h>
#include <stdio.h>
#include <sys/hackios_platform.h>

static int lookup_error, property_error;
static const void *property;
static unsigned int property_size;

int SecureDTLookupEntry(DTEntry start, const char *path, DTEntry *entry)
{
    assert(start == NULL && strcmp(path, "/chosen") == 0);
    *entry = (void *)1;
    return lookup_error;
}

int SecureDTGetProperty(DTEntry entry, const char *key, const void **value, unsigned int *size)
{
    assert(entry == (void *)1 && strcmp(key, "hackios-platform") == 0);
    *value = property;
    *size = property_size;
    return property_error;
}

int main(void)
{
    property = "redwood"; property_size = 8;
    assert(hackios_platform_is("redwood"));
    assert(!hackios_platform_is("sweet"));
    lookup_error = -1;
    assert(!hackios_platform_is("redwood"));
    lookup_error = 0; property_error = -1;
    assert(!hackios_platform_is("redwood"));
    property_error = 0; property = NULL;
    assert(!hackios_platform_is("redwood"));
    property = "redwood"; property_size = 7;
    assert(!hackios_platform_is("redwood"));
    property = "redwoodx"; property_size = 8;
    assert(!hackios_platform_is("redwood"));
    property = "redwood\0x"; property_size = 10;
    assert(!hackios_platform_is("redwood"));
    property = "Redwood"; property_size = 8;
    assert(!hackios_platform_is("redwood"));
    property = "sweet"; property_size = 6;
    assert(!hackios_platform_is("redwood"));
    assert(hackios_platform_is("sweet"));
    property = "qemu-virt"; property_size = 10;
    assert(!hackios_platform_is("redwood"));
    assert(hackios_platform_is("qemu-virt"));
    puts("platform: 13 assertions passed");
    return 0;
}
