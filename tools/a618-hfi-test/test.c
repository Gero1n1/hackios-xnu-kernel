#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <sys/adreno_hfi_codec.h>

static void
test_v2_wire_and_ack(void)
{
	uint32_t ring[8] = { 0 }, expected, status = 99, count = 0, next = 0;
	uint32_t read = 0, write = 0;
	uint32_t seq = 0xabc;
	static const uint8_t expected_bytes[16] = {
		0x04, 0x03, 0xc0, 0xab, 0x44, 0x33, 0x22, 0x11,
		0x88, 0x77, 0x66, 0x55, 0xfa, 0xfa, 0xfa, 0xfa
	};
	expected = adreno_hfi_header(4, 3, ADRENO_HFI_CMD, seq);
	assert(expected == 0xabc00304u); /* historical Redwood v2 command encoding */
	uint32_t packet[] = { expected, 0x11223344, 0x55667788 };
	assert(adreno_hfi_ring_write(ADRENO_HFI_V2, ring, 8, &read, &write, packet, 3, 0xfafafafa) == ADRENO_HFI_OK);
	assert(write == 4);
	assert(memcmp(ring, expected_bytes, sizeof(expected_bytes)) == 0);
	assert(adreno_hfi_packet_words(ADRENO_HFI_V2, 3) == 4);
	assert(adreno_hfi_peek(ADRENO_HFI_V2, 8, 0, 4, ring[0], 8, &count, &next) == ADRENO_HFI_OK);
	assert(count == 3 && next == 4 && ring[1] == 0x11223344 && ring[2] == 0x55667788);
	uint32_t ack[] = {
		adreno_hfi_header(4, 3, ADRENO_HFI_V2_ACK, 7), expected, 0, 0xfafafafa
	};
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, expected, &status) == ADRENO_HFI_OK);
	assert(status == 0);
	ack[2] = 7;
	status = 99;
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, expected, &status) == ADRENO_HFI_STATUS);
	assert(status == 7);
}

static void
test_v1_ack(void)
{
	uint32_t cmd = adreno_hfi_header(30, 4, ADRENO_HFI_CMD, 0x123);
	uint32_t ack[] = { adreno_hfi_header(30, 3, ADRENO_HFI_V1_ACK, 5), cmd, 0 };
	uint32_t status = 17;
	assert(adreno_hfi_packet_words(ADRENO_HFI_V1, 3) == 3);
	assert(adreno_hfi_ack(ADRENO_HFI_V1, ack, 3, cmd, &status) == ADRENO_HFI_OK);
	assert(status == 0);
	ack[0] = adreno_hfi_header(30, 3, ADRENO_HFI_V2_ACK, 0);
	assert(adreno_hfi_ack(ADRENO_HFI_V1, ack, 3, cmd, &status) == ADRENO_HFI_INVALID);
	ack[0] = adreno_hfi_header(30, 3, ADRENO_HFI_V1_ACK, 0);
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 3, cmd, &status) == ADRENO_HFI_INVALID);
}

static void
test_ack_rejections_and_state(void)
{
	uint32_t cmd = adreno_hfi_header(10, 1, ADRENO_HFI_CMD, 0x321);
	uint32_t ack[] = { adreno_hfi_header(10, 3, ADRENO_HFI_V2_ACK, 0), cmd, 0, 0 };
	uint32_t status = 0x55, packet_words = 77, next = 6;
	uint32_t read_index = 6;
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 2, cmd, &status) == ADRENO_HFI_PARTIAL);
	assert(status == 0x55);
	ack[1] ^= 1u; /* wrong command ID: no status is accepted */
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, cmd, &status) == ADRENO_HFI_NO_MATCH);
	assert(status == 0x55);
	ack[1] = cmd ^ (1u << 20); /* wrong sequence */
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, cmd, &status) == ADRENO_HFI_NO_MATCH);
	ack[1] = cmd ^ (1u << 16); /* wrong echoed type */
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, cmd, &status) == ADRENO_HFI_NO_MATCH);
	ack[1] = cmd ^ (1u << 8); /* wrong echoed length */
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, cmd, &status) == ADRENO_HFI_NO_MATCH);
	ack[1] = cmd;
	ack[0] = adreno_hfi_header(10, 3, ADRENO_HFI_V1_ACK, 0);
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, cmd, &status) == ADRENO_HFI_INVALID);
	assert(status == 0x55);
	ack[0] = adreno_hfi_header(10, 8, ADRENO_HFI_V2_ACK, 0);
	assert(adreno_hfi_ack(ADRENO_HFI_V2, ack, 4, cmd, &status) == ADRENO_HFI_PARTIAL);
	assert(status == 0x55);
	/* A failed/partial packet peek never writes its proposed read index. */
	assert(adreno_hfi_peek(ADRENO_HFI_V2, 8, read_index, 1, ack[0], 8,
	    &packet_words, &next) == ADRENO_HFI_PARTIAL);
	assert(read_index == 6 && packet_words == 77 && next == 6);
}

static void
test_ring_wrap_and_full(void)
{
	uint32_t used = 0, free_words = 0, words = 0, next = 0;
	uint32_t header = adreno_hfi_header(3, 3, ADRENO_HFI_CMD, 1);
	uint32_t ring[8] = { 0 }, packet[] = { header, 0xaaaa, 0xbbbb };
	uint32_t read = 4, write = 4, before_write;
	assert(adreno_hfi_ring_state(8, 6, 2, &used) && used == 4);
	assert(adreno_hfi_ring_write(ADRENO_HFI_V2, ring, 8, &read, &write, packet, 3, 0xfafafafa) == ADRENO_HFI_OK);
	assert(write == 0 && ring[4] == header && ring[5] == 0xaaaa && ring[6] == 0xbbbb && ring[7] == 0xfafafafa);
	assert(adreno_hfi_peek(ADRENO_HFI_V2, 8, 4, 0, header, 8, &words, &next) == ADRENO_HFI_OK);
	assert(words == 3 && next == 0); /* ring reaches its end and wraps to index zero */
	assert(adreno_hfi_ring_space(8, 0, 4, 3, &free_words) == ADRENO_HFI_OK && free_words == 3);
	assert(adreno_hfi_ring_space(8, 0, 4, 4, &free_words) == ADRENO_HFI_FULL && free_words == 3);
	assert(adreno_hfi_queue_valid(ADRENO_HFI_V2, 3, 1, 1 | (10u << 8), 8, 0, 4));
	assert(!adreno_hfi_queue_valid(ADRENO_HFI_V2, 3, 1, 2 | (10u << 8), 8, 0, 4));
	assert(!adreno_hfi_queue_valid(ADRENO_HFI_V2, 1, 1, 1 | (10u << 8), 8, 0, 4));
	assert(!adreno_hfi_queue_valid(ADRENO_HFI_V2, 3, 1, 1 | (10u << 8), 8, 1, 4));
	assert(!adreno_hfi_queue_valid(3u, 3, 1, 1 | (10u << 8), 8, 0, 4));
	read = 0; write = 4; before_write = write;
	assert(adreno_hfi_ring_write(ADRENO_HFI_V2, ring, 8, &read, &write, packet, 3, 0xfafafafa) == ADRENO_HFI_FULL);
	assert(write == before_write);
	assert(adreno_hfi_ring_state(8, 8, 0, &used) == false);
	assert(adreno_hfi_peek(ADRENO_HFI_V2, 8, 4, 4, header, 8, &words, &next) == ADRENO_HFI_PARTIAL);
}

int
main(void)
{
	test_v2_wire_and_ack();
	test_v1_ack();
	test_ack_rejections_and_state();
	test_ring_wrap_and_full();
	puts("A618/Redwood HFI codec fixtures passed");
	return 0;
}
