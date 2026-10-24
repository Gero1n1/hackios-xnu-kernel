/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/adreno618_firmware.h>

static unsigned checks;
#define CHECK(expression) do { ++checks; assert(expression); } while (0)

static const void *board = "sweet";
static unsigned int board_bytes = 6;
static int lookup_error, property_error;

int SecureDTLookupEntry(DTEntry start, const char *path, DTEntry *entry)
{
    assert(start == NULL && strcmp(path, "/chosen") == 0);
    *entry = (void *)1;
    return lookup_error;
}

int SecureDTGetProperty(DTEntry entry, const char *key, const void **value, unsigned int *size)
{
    assert(entry == (void *)1 && strcmp(key, "hackios-platform") == 0);
    *value = board;
    *size = board_bytes;
    return property_error;
}

static uint32_t registers[ADRENO618_GMU_RESOURCE_BYTES / 4u];
static uint32_t before[ADRENO618_GMU_RESOURCE_BYTES / 4u];
static uint8_t firmware[ADRENO618_GMU_ITCM_BYTES + 4u];

static struct adreno618_gmu_mapping mapping(void)
{
    struct adreno618_gmu_mapping result = {
        registers, sizeof(registers), ADRENO618_GMU_PHYSICAL_BASE, 0x06010800
    };
    return result;
}

static void reset_registers(void)
{
    memset(registers, 0xa5, sizeof(registers));
    registers[ADRENO618_GMU_SYSRESET_OFFSET / 4u] = adreno618_native_le32(1);
    memcpy(before, registers, sizeof(registers));
}

static void refused(struct adreno618_gmu_mapping *m, const void *data, size_t bytes, int error)
{
    reset_registers();
    CHECK(adreno618_gmu_upload_checked(m, data, bytes, true) == error);
    CHECK(memcmp(before, registers, sizeof(registers)) == 0);
}

static void test_board_and_arguments(void)
{
    struct adreno618_gmu_mapping m = mapping(), bad;
    board = "redwood"; board_bytes = 8;
    refused(&m, firmware, 4, ADRENO618_FIRMWARE_BOARD);
    board = "qemu-virt"; board_bytes = 10;
    refused(NULL, NULL, 0, ADRENO618_FIRMWARE_BOARD);
    board = "sweet"; board_bytes = 5;
    refused(&m, firmware, 4, ADRENO618_FIRMWARE_BOARD);
    board = "sweetx"; board_bytes = 6;
    refused(&m, firmware, 4, ADRENO618_FIRMWARE_BOARD);
    board = "sweet\0x"; board_bytes = 8;
    refused(&m, firmware, 4, ADRENO618_FIRMWARE_BOARD);
    board = NULL;
    refused(&m, firmware, 4, ADRENO618_FIRMWARE_BOARD);
    board = "sweet"; board_bytes = 6; lookup_error = -1;
    refused(&m, firmware, 4, ADRENO618_FIRMWARE_BOARD);
    lookup_error = 0; property_error = -1;
    refused(&m, firmware, 4, ADRENO618_FIRMWARE_BOARD);
    property_error = 0;

    refused(NULL, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    refused(&m, NULL, 4, ADRENO618_FIRMWARE_ARGUMENT);
    refused(&m, firmware, 0, ADRENO618_FIRMWARE_ARGUMENT);
    refused(&m, firmware, 3, ADRENO618_FIRMWARE_ARGUMENT);
    refused(&m, firmware, sizeof(firmware), ADRENO618_FIRMWARE_ARGUMENT);
    refused(&m, registers, 4, ADRENO618_FIRMWARE_ARGUMENT);
    refused(&m, (void *)(UINTPTR_MAX - 1u), 4, ADRENO618_FIRMWARE_ARGUMENT);

    bad = m; bad.registers = NULL;
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    bad = m; bad.registers = (volatile uint32_t *)((uint8_t *)registers + 1);
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    bad = m; bad.registers = (volatile uint32_t *)(UINTPTR_MAX - 3u);
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    bad = m; bad.physical_base = 0x05000000; /* GPU base is not the GMU base */
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    bad = m; bad.chip_id = 0x06030500; /* redwood A642L */
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    bad = m; bad.bytes = ADRENO618_GMU_SYSRESET_OFFSET;
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    bad = m; bad.bytes = sizeof(registers) + 4;
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);
    bad = m; bad.bytes -= 1;
    refused(&bad, firmware, 4, ADRENO618_FIRMWARE_ARGUMENT);

    reset_registers();
    registers[ADRENO618_GMU_SYSRESET_OFFSET / 4u] = 0;
    memcpy(before, registers, sizeof(registers));
    CHECK(adreno618_gmu_upload_checked(&m, firmware, 4, true) == ADRENO618_FIRMWARE_RESET);
    CHECK(memcmp(before, registers, sizeof(registers)) == 0);
}

static void uploaded(const uint8_t *data, size_t bytes, bool verify, uint32_t chip)
{
    struct adreno618_gmu_mapping m = mapping();
    m.chip_id = chip;
    reset_registers();
    CHECK(adreno618_gmu_upload_checked(&m, data, bytes, verify) == ADRENO618_FIRMWARE_OK);
    CHECK(memcmp((const uint8_t *)registers + ADRENO618_GMU_ITCM_OFFSET, data, bytes) == 0);
    CHECK(memcmp(before, registers, ADRENO618_GMU_ITCM_OFFSET) == 0);
    CHECK(memcmp((const uint8_t *)before + ADRENO618_GMU_ITCM_OFFSET + bytes,
        (const uint8_t *)registers + ADRENO618_GMU_ITCM_OFFSET + bytes,
        sizeof(registers) - ADRENO618_GMU_ITCM_OFFSET - bytes) == 0);
    CHECK(adreno618_native_le32(registers[ADRENO618_GMU_SYSRESET_OFFSET / 4u]) == 1);
}

static void test_upload(void)
{
    for (size_t i = 0; i < sizeof(firmware); ++i) firmware[i] = (uint8_t)(i * 73u + 41u);
    uploaded(firmware, 4, true, 0x06010800);
    uploaded(firmware, ADRENO618_GMU_ITCM_BYTES, true, 0x06010800);
    uploaded(firmware, ADRENO618_GMU_ITCM_BYTES, false, 0x060108ff);
    uploaded(firmware + 1, 12, true, 0x06010800); /* source need not be aligned */
    struct adreno618_gmu_mapping m = mapping();
    m.bytes = ADRENO618_GMU_SYSRESET_OFFSET + 4;
    reset_registers();
    CHECK(adreno618_gmu_upload_checked(&m, firmware, 4, true) == ADRENO618_FIRMWARE_OK);
}

static void test_sqe(void)
{
    uint8_t data[128];
    struct adreno_sqe_view view, saved;
    memset(&saved, 0x5a, sizeof(saved));
    for (size_t i = 0; i < sizeof(data); ++i) data[i] = (uint8_t)(i * 31u + 1u);
    for (size_t n = 0; n < sizeof(data); ++n) {
        uint8_t *short_image = (uint8_t *)malloc(n ? n : 1);
        CHECK(short_image != NULL);
        memcpy(short_image, data, n);
        memcpy(&view, &saved, sizeof(view));
        bool valid = n >= 8 && (n & 3u) == 0;
        CHECK(adreno_sqe_code(short_image, n, sizeof(data), &view) == valid);
        if (valid) {
            CHECK(view.code == short_image + 4 && view.bytes == n - 4);
            CHECK(view.file_header == adreno618_le32(data));
        } else {
            CHECK(memcmp(&view, &saved, sizeof(view)) == 0);
        }
        free(short_image);
    }
    CHECK(!adreno_sqe_code(NULL, 8, 128, &view));
    CHECK(!adreno_sqe_code(data, 8, 128, NULL));
    CHECK(!adreno_sqe_code(data, 8, 7, &view));
    CHECK(!adreno_sqe_code(data, 8, 128, (struct adreno_sqe_view *)data));
    CHECK(!adreno_sqe_code((void *)(UINTPTR_MAX - 3u), 8, 128, &view));
    CHECK(!adreno_sqe_code(data, (size_t)UINT32_MAX + 1u, SIZE_MAX, &view));
    CHECK(adreno_sqe_code(data + 1, 8, 128, &view));
    CHECK(view.file_header == adreno618_le32(data + 1));
}

static uint8_t *read_image(const char *path, size_t limit, size_t *bytes)
{
    FILE *file = fopen(path, "rb");
    CHECK(file != NULL);
    CHECK(fseek(file, 0, SEEK_END) == 0);
    long size = ftell(file);
    CHECK(size > 0 && (size_t)size <= limit);
    CHECK(fseek(file, 0, SEEK_SET) == 0);
    uint8_t *data = (uint8_t *)malloc((size_t)size);
    CHECK(data != NULL);
    CHECK(fread(data, 1, (size_t)size, file) == (size_t)size);
    CHECK(fclose(file) == 0);
    *bytes = (size_t)size;
    return data;
}

int main(int argc, char **argv)
{
    CHECK(argc == 1 || argc == 3);
    test_board_and_arguments();
    test_upload();
    test_sqe();
    if (argc == 3) {
        size_t gmu_bytes, sqe_bytes;
        uint8_t *gmu = read_image(argv[1], ADRENO618_GMU_ITCM_BYTES, &gmu_bytes);
        uint8_t *sqe = read_image(argv[2], 0x40000, &sqe_bytes);
        struct adreno_sqe_view view;
        uploaded(gmu, gmu_bytes, true, 0x06010800);
        CHECK(adreno_sqe_code(sqe, sqe_bytes, 0x40000, &view));
        CHECK(view.code == sqe + 4 && view.bytes == sqe_bytes - 4);
        printf("Private firmware inert-RAM check: GMU %zu bytes, SQE %zu bytes; no firmware executed\n",
            gmu_bytes, sqe_bytes);
        free(gmu); free(sqe);
    }
    printf("A618: %u checks passed (board, layout, reset, ITCM-only upload, SQE bounds)\n", checks);
    return 0;
}
