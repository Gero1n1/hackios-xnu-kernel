#include <stddef.h>
typedef void *DTEntry;
enum { kSuccess = 0 };
int SecureDTLookupEntry(DTEntry, const char *, DTEntry *);
int SecureDTGetProperty(DTEntry, const char *, const void **, unsigned int *);
