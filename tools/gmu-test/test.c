#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/adreno_gmu_firmware.h>

static void word(uint8_t *p, uint32_t value)
{
    for (unsigned i = 0; i < 4; i++) p[i] = (uint8_t)(value >> (8 * i));
}

static bool valid(uint8_t *data, size_t size)
{
    return adreno_gmu_blocks_valid(data, size, 0, 0x10004000, 0x4000);
}

int main(void)
{
    uint8_t data[16 + 0x4000 + 16] = {0};
    assert(!adreno_gmu_blocks_valid(NULL, 20, 0, 0x10004000, 0x4000));
    assert(!valid(data, 0));
    word(data + 4, 4); word(data + 16, 0x12345678);
    assert(valid(data, 20));
    for (size_t n = 0; n < 20; n++) {
        uint8_t *short_image = (uint8_t *)malloc(n ? n : 1);
        assert(short_image != NULL);
        memcpy(short_image, data, n);
        assert(!valid(short_image, n));
        free(short_image);
    }
    for (size_t n = 21; n < 36; n++) assert(!valid(data, n));
    assert(valid(data, 36)); /* a complete zero-size metadata block */
    word(data, 0x3ffc); word(data + 4, 8);
    assert(!valid(data, 24)); /* leaves ITCM */
    word(data, 0x10003ffc);
    assert(!valid(data, 24)); /* enters DTCM from outside */
    word(data, 0xfffffffc);
    assert(!valid(data, 24)); /* wraps a 32-bit address */
    word(data, 1); word(data + 4, 4);
    assert(!valid(data, 20));
    word(data, 0); word(data + 4, 3);
    assert(!valid(data, 19));
    word(data + 4, UINT32_MAX);
    assert(!valid(data, 16));
    word(data + 4, 0);
    assert(!valid(data, 16)); /* no firmware payload */
    word(data + 4, 0x4000);
    assert(valid(data, 16 + 0x4000)); /* exact TCM boundary */
    assert(!adreno_gmu_range_inside(0, 0x10004004, 0, 0x4000));
    assert(!adreno_gmu_range_inside(0, 0x10004004, 0x10004000, 0x4000));
    /* Bounded malformed-input corpus; also run under UBSan. */
    for (size_t n = 0; n < 512; n++) {
        for (size_t i = 0; i < n; i++) data[i] = (uint8_t)(i * 73 + n);
        (void)valid(data, n);
    }
    puts("GMU: valid, truncated, alignment, wrap, TCM boundaries and malformed corpus passed");
    return 0;
}
