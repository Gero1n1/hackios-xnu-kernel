/*
 * Pure HFI packet helpers.  Keep protocol-version differences explicit: the
 * legacy A6xx packet codec is useful for host fixtures, while current callers
 * must opt into the newer Redwood v2 framing themselves.
 */
#ifndef _SYS_ADRENO_HFI_CODEC_H_
#define _SYS_ADRENO_HFI_CODEC_H_

#include <stdbool.h>
#include <stdint.h>

#define ADRENO_HFI_V1 1u
#define ADRENO_HFI_V2 2u
#define ADRENO_HFI_CMD 0u
#define ADRENO_HFI_V2_ACK 1u
#define ADRENO_HFI_V1_POST 1u
#define ADRENO_HFI_V1_ACK 2u

struct adreno_hfi_fields {
	uint32_t id;
	uint32_t words;
	uint32_t type;
	uint32_t sequence;
};

enum adreno_hfi_result {
	ADRENO_HFI_OK = 0,
	ADRENO_HFI_INVALID = -1,
	ADRENO_HFI_PARTIAL = -2,
	ADRENO_HFI_FULL = -3,
	ADRENO_HFI_NO_MATCH = -4,
	ADRENO_HFI_STATUS = -5
};

static inline uint32_t
adreno_hfi_header(uint32_t id, uint32_t words, uint32_t type, uint32_t sequence)
{
	return (id & 0xffu) | ((words & 0xffu) << 8) |
	    ((type & 0xfu) << 16) | ((sequence & 0xfffu) << 20);
}

static inline struct adreno_hfi_fields
adreno_hfi_decode(uint32_t header)
{
	struct adreno_hfi_fields fields;
	fields.id = header & 0xffu;
	fields.words = (header >> 8) & 0xffu;
	fields.type = (header >> 16) & 0xfu;
	fields.sequence = header >> 20;
	return fields;
}

static inline bool
adreno_hfi_type_valid(uint32_t version, uint32_t type)
{
	return version == ADRENO_HFI_V2 ?
	    (type == ADRENO_HFI_CMD || type == ADRENO_HFI_V2_ACK) :
	    version == ADRENO_HFI_V1 &&
	    (type == ADRENO_HFI_CMD || type == ADRENO_HFI_V1_POST || type == ADRENO_HFI_V1_ACK);
}

static inline uint32_t
adreno_hfi_packet_words(uint32_t version, uint32_t words)
{
	if (words == 0 || words > 255 || (version != ADRENO_HFI_V1 && version != ADRENO_HFI_V2))
		return 0;
	return version == ADRENO_HFI_V2 ? (words + 3u) & ~3u : words;
}

/* Circular indices have one reserved word so read == write means empty. */
static inline bool
adreno_hfi_ring_state(uint32_t size, uint32_t read, uint32_t write, uint32_t *used)
{
	if (size < 2 || read >= size || write >= size)
		return false;
	*used = (write + size - read) % size;
	return true;
}

static inline bool
adreno_hfi_queue_valid(uint32_t version, uint32_t active_queues, uint32_t queue_id,
	    uint32_t queue_type, uint32_t size, uint32_t read, uint32_t write)
{
	uint32_t used;
	if ((version != ADRENO_HFI_V1 && version != ADRENO_HFI_V2) || queue_id >= active_queues ||
	    (queue_type & 0xffu) != queue_id || size < 2 ||
	    !adreno_hfi_ring_state(size, read, write, &used))
		return false;
	return version != ADRENO_HFI_V2 ||
	    ((size & 3u) == 0 && (read & 3u) == 0 && (write & 3u) == 0);
}

static inline enum adreno_hfi_result
adreno_hfi_ring_space(uint32_t size, uint32_t read, uint32_t write,
	    uint32_t need, uint32_t *free_words)
{
	uint32_t used;
	if (need == 0 || !adreno_hfi_ring_state(size, read, write, &used))
		return ADRENO_HFI_INVALID;
	*free_words = size - used - 1;
	return need <= *free_words ? ADRENO_HFI_OK : ADRENO_HFI_FULL;
}

/* Derive a complete packet extent without changing the consumer index. */
static inline enum adreno_hfi_result
adreno_hfi_peek(uint32_t version, uint32_t size, uint32_t read, uint32_t write,
	    uint32_t header, uint32_t max_words, uint32_t *packet_words, uint32_t *next_read)
{
	struct adreno_hfi_fields fields = adreno_hfi_decode(header);
	uint32_t used, padded;
	if (!adreno_hfi_ring_state(size, read, write, &used) || max_words == 0)
		return ADRENO_HFI_INVALID;
	if (used == 0)
		return ADRENO_HFI_PARTIAL;
	if (!adreno_hfi_type_valid(version, fields.type))
		return ADRENO_HFI_INVALID;
	padded = adreno_hfi_packet_words(version, fields.words);
	if (padded == 0 || fields.words > max_words)
		return ADRENO_HFI_INVALID;
	if (padded > used)
		return ADRENO_HFI_PARTIAL;
	*packet_words = fields.words;
	*next_read = (read + padded) % size;
	return ADRENO_HFI_OK;
}

/* Preflight and publish a whole message; the producer index changes last. */
static inline enum adreno_hfi_result
adreno_hfi_ring_write(uint32_t version, volatile uint32_t *ring, uint32_t size,
	    volatile uint32_t *readp, volatile uint32_t *writep, const uint32_t *packet,
	    uint32_t words, uint32_t padding_word)
{
	struct adreno_hfi_fields fields;
	uint32_t read, write, padded, free_words, at;
	enum adreno_hfi_result result;
	if (ring == 0 || readp == 0 || writep == 0 || packet == 0 || words == 0)
		return ADRENO_HFI_INVALID;
	read = *readp;
	write = *writep;
	padded = adreno_hfi_packet_words(version, words);
	if (padded == 0 || (version == ADRENO_HFI_V2 && ((read | write) & 3u) != 0) ||
	    !adreno_hfi_ring_state(size, read, write, &free_words))
		return ADRENO_HFI_INVALID;
	fields = adreno_hfi_decode(packet[0]);
	if (fields.words != words || !adreno_hfi_type_valid(version, fields.type) ||
	    fields.type != ADRENO_HFI_CMD)
		return ADRENO_HFI_INVALID;
	result = adreno_hfi_ring_space(size, read, write, padded, &free_words);
	if (result != ADRENO_HFI_OK)
		return result;
	at = write;
	for (uint32_t i = 0; i < padded; i++) {
		ring[at] = i < words ? packet[i] : padding_word;
		at = (at + 1) % size;
	}
	*writep = at;
	return ADRENO_HFI_OK;
}

/* Validate a complete ACK before exposing its status or moving a ring index. */
static inline enum adreno_hfi_result
adreno_hfi_ack(uint32_t version, const uint32_t *packet, uint32_t available,
	    uint32_t expected_command, uint32_t *status)
{
	struct adreno_hfi_fields ack, echoed;
	uint32_t padded;
	if (packet == 0 || status == 0 || available < 3 ||
	    (version != ADRENO_HFI_V1 && version != ADRENO_HFI_V2))
		return ADRENO_HFI_PARTIAL;
	ack = adreno_hfi_decode(packet[0]);
	echoed = adreno_hfi_decode(packet[1]);
	if (!adreno_hfi_type_valid(version, ack.type) ||
	    ack.type != (version == ADRENO_HFI_V1 ? ADRENO_HFI_V1_ACK : ADRENO_HFI_V2_ACK) ||
	    ack.words < 3 || ack.words > available)
		return ack.words > available ? ADRENO_HFI_PARTIAL : ADRENO_HFI_INVALID;
	if (!adreno_hfi_type_valid(version, echoed.type) || echoed.type != ADRENO_HFI_CMD ||
	    packet[1] != expected_command)
		return ADRENO_HFI_NO_MATCH;
	padded = adreno_hfi_packet_words(version, ack.words);
	if (padded == 0 || padded > available)
		return ADRENO_HFI_PARTIAL;
	*status = packet[2];
	return *status == 0 ? ADRENO_HFI_OK : ADRENO_HFI_STATUS;
}

#endif /* _SYS_ADRENO_HFI_CODEC_H_ */
