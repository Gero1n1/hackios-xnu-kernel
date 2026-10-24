/*
 * Darwin-on-redwood: what stands between the two sides of the station boundary
 * (sys/redwood_sta.h).
 *
 * Whoever wants a network asks from wherever it happens to be: an ioctl of wifid's, the thread
 * that brings the radio up. The station side wants to be called by one thread, one call at a
 * time, and a scan or a join takes seconds. So what is asked for is noted here and done by a
 * thread of this file's; the asker returns at once and hears the outcome as an event. One
 * request of each kind can wait: a second scan asked for while the first has not begun
 * replaces it, as does a second join.
 *
 * What the last scan heard is kept here, so that it can be read without entering the station
 * side, and so is the list of channels.
 *
 * The network the kernel was given in a file is a standing request: joined at the start, and
 * again whenever the radio is turned back on (Settings' switch), until somebody asks for
 * another network -- as iOS itself rejoins the networks it knows. If that other network cannot
 * be joined (a wrong passphrase), the standing one is joined again.
 */
#include <kern/clock.h>
#include <kern/kalloc.h>
#include <kern/locks.h>
#include <kern/sched_prim.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <pexpert/pexpert.h>
#include <sys/errno.h>
#include <sys/hackios_platform.h>
#include <sys/redwood_input.h>
#include <sys/redwood_net.h>
#include <sys/redwood_sta.h>
#include <sys/systm.h>

extern void thread_set_thread_name(thread_t thread, const char *name);

#define HEARD_MOST      128
#define CHANNELS_MOST   96

static LCK_GRP_DECLARE(sta_lock_group, "redwood-sta");
static LCK_MTX_DECLARE(sta_lock, &sta_lock_group);

static const struct redwood_sta_ops *sta_ops;   /* the station side, while there is one */
static bool sta_thread_made;
static bool sta_calls;                          /* the thread is in a call of the station side's */
static unsigned sta_reads;                      /* ... and so many others are in its state() */
static bool sta_on;                             /* the radio's power, as last asked for */

/* what has been asked for and not begun */
static bool asked_power, asked_power_on, asked_scan, asked_join, asked_leave;
static struct redwood_sta_scan asked_scan_what;
static struct redwood_sta_join asked_join_what;
static struct redwood_sta_join standing;        /* the network joined whenever the radio comes on (ssid_length 0: none) */
static struct redwood_sta_join fallback;        /* ... as it was before a join that has not come through yet */

static struct redwood_sta_bss *heard;           /* by the last scan that came to an end */
static unsigned heard_count;
static struct redwood_sta_channel channels[CHANNELS_MOST];
static unsigned channel_count;

static void
heard_one(const struct redwood_sta_bss *bss, __unused void *context)
{
	/* (only the thread writes the list, and it reads the count without the lock) */
	if (heard_count < HEARD_MOST) {
		lck_mtx_lock(&sta_lock);
		heard[heard_count++] = *bss;
		lck_mtx_unlock(&sta_lock);
	}
}

static void
sta_runs(__unused void *parameter, __unused wait_result_t result)
{
	static struct redwood_sta_scan scan;
	static struct redwood_sta_join join;

	for (;;) {
		const struct redwood_sta_ops *ops;
		bool power = false, power_on = false, leave = false, do_join = false, do_scan = false;
		int error = 0;

		lck_mtx_lock(&sta_lock);
		while (sta_ops == NULL || !(asked_power || asked_leave || asked_join || asked_scan)) {
			lck_mtx_sleep(&sta_lock, LCK_SLEEP_DEFAULT, (event_t)&sta_ops, THREAD_UNINT);
		}
		ops = sta_ops;
		/* one thing at a time, in the order that makes sense when several wait */
		if (asked_power) {
			power = true;
			power_on = asked_power_on;
			asked_power = false;
		} else if (asked_leave) {
			leave = true;
			asked_leave = false;
		} else if (asked_join) {
			do_join = true;
			join = asked_join_what;
			bzero(&asked_join_what, sizeof(asked_join_what));
			asked_join = false;
		} else {
			do_scan = true;
			scan = asked_scan_what;
			asked_scan = false;
		}
		sta_calls = true;
		lck_mtx_unlock(&sta_lock);

		if (power) {
			struct redwood_sta_state *state = kalloc_data(sizeof(*state), Z_WAITOK | Z_ZERO);
			bool was_joined = false;

			if (state != NULL) {
				ops->state(state);
				was_joined = state->phase == REDWOOD_STA_JOINED;
				kfree_data(state, sizeof(*state));
			}
			error = ops->power(power_on);
			if (!power_on && was_joined) {
				redwood_apple80211_event(REDWOOD_STA_LINK, 0);
			}
			redwood_apple80211_event(REDWOOD_STA_POWER, error == 0 ? power_on : !power_on);
			lck_mtx_lock(&sta_lock);
			if (error == 0 && power_on && standing.ssid_length != 0 && !asked_join) {
				asked_join_what = standing;
				asked_join = true;
			}
			lck_mtx_unlock(&sta_lock);
		} else if (leave) {
			ops->leave();
			redwood_apple80211_event(REDWOOD_STA_LINK, 0);
		} else if (do_join) {
			error = ops->join(&join);
			bzero(&join, sizeof(join));
			redwood_apple80211_event(REDWOOD_STA_JOIN_DONE, error);
			if (error == 0) {
				redwood_apple80211_event(REDWOOD_STA_LINK, 1);
			}
			/* a join that did not come through (a wrong passphrase) leaves things as they were: the standing network again */
			lck_mtx_lock(&sta_lock);
			if (error != 0 && fallback.ssid_length != 0 && !asked_join && !asked_leave && !asked_power) {
				standing = asked_join_what = fallback;
				asked_join = true;
			}
			bzero(&fallback, sizeof(fallback));
			lck_mtx_unlock(&sta_lock);
		} else if (do_scan) {
			error = ops->scan(&scan);
			lck_mtx_lock(&sta_lock);
			heard_count = 0;
			lck_mtx_unlock(&sta_lock);
			ops->heard(heard_one, NULL);
			redwood_apple80211_event(REDWOOD_STA_SCAN_DONE, error);
		}

		lck_mtx_lock(&sta_lock);
		sta_calls = false;
		lck_mtx_unlock(&sta_lock);
		thread_wakeup((event_t)&sta_calls);
	}
}

/* ---- the station side ---------------------------------------------------------------------------------- */

void
redwood_sta_attach(const struct redwood_sta_ops *ops)
{
	thread_t thread;

	if (ops == NULL) {
		lck_mtx_lock(&sta_lock);
		sta_ops = NULL;
		asked_power = asked_scan = asked_join = asked_leave = false;
		while (sta_calls || sta_reads != 0) {
			lck_mtx_sleep(&sta_lock, LCK_SLEEP_DEFAULT, (event_t)&sta_calls, THREAD_UNINT);
		}
		heard_count = channel_count = 0;
		lck_mtx_unlock(&sta_lock);
		return;
	}
	if (heard == NULL) {
		heard = kalloc_data(HEARD_MOST * sizeof(*heard), Z_WAITOK | Z_ZERO);
	}
	if (heard == NULL) {
		redwood_wifi_tell("wifi: no memory for what a scan hears: nobody can ask the radio for a network\n");
		return;
	}
	if (!sta_thread_made) {
		if (kernel_thread_start(sta_runs, NULL, &thread) != KERN_SUCCESS) {
			redwood_wifi_tell("wifi: no thread for the station: nobody can ask the radio for a network\n");
			return;
		}
		thread_set_thread_name(thread, "wifi-station");
		thread_deallocate(thread);
		sta_thread_made = true;
	}
	channel_count = ops->channels(channels, CHANNELS_MOST);
	lck_mtx_lock(&sta_lock);
	sta_ops = ops;
	sta_on = true;
	lck_mtx_unlock(&sta_lock);
	thread_wakeup((event_t)&sta_ops);
	redwood_apple80211_event(REDWOOD_STA_POWER, 1);
}

void
redwood_sta_lost(int error)
{
	redwood_apple80211_event(REDWOOD_STA_LINK, error != 0 ? error : ECONNRESET);
}

/* ---- who wants a network ------------------------------------------------------------------------------- */

bool
redwood_sta_present(void)
{
	return sta_ops != NULL;
}

bool
redwood_sta_powered(void)
{
	return sta_ops != NULL && sta_on;
}

bool
redwood_sta_busy(void)
{
	bool busy;

	lck_mtx_lock(&sta_lock);
	busy = sta_ops != NULL && (sta_calls || asked_power || asked_leave || asked_join || asked_scan);
	lck_mtx_unlock(&sta_lock);
	return busy;
}

int
redwood_sta_ask_power(bool on)
{
	lck_mtx_lock(&sta_lock);
	if (sta_ops == NULL) {
		lck_mtx_unlock(&sta_lock);
		return ENXIO;
	}
	sta_on = on;
	asked_power = true;
	asked_power_on = on;
	if (!on) {
		asked_scan = asked_join = asked_leave = false;
	}
	lck_mtx_unlock(&sta_lock);
	thread_wakeup((event_t)&sta_ops);
	return 0;
}

int
redwood_sta_ask_scan(const struct redwood_sta_scan *what)
{
	lck_mtx_lock(&sta_lock);
	if (sta_ops == NULL || !sta_on) {
		lck_mtx_unlock(&sta_lock);
		return sta_ops == NULL ? ENXIO : ENETDOWN;
	}
	asked_scan_what = *what;
	asked_scan = true;
	lck_mtx_unlock(&sta_lock);
	thread_wakeup((event_t)&sta_ops);
	return 0;
}

int
redwood_sta_ask_join_standing(const struct redwood_sta_join *what)
{
	int error = redwood_sta_ask_join(what);

	lck_mtx_lock(&sta_lock);
	if (error == 0) {
		standing = *what;
	}
	lck_mtx_unlock(&sta_lock);
	return error;
}

int
redwood_sta_ask_join(const struct redwood_sta_join *what)
{
	lck_mtx_lock(&sta_lock);
	if (sta_ops == NULL || !sta_on) {
		lck_mtx_unlock(&sta_lock);
		return sta_ops == NULL ? ENXIO : ENETDOWN;
	}
	if (standing.ssid_length != 0) {
		fallback = standing;
	}
	bzero(&standing, sizeof(standing));     /* (whoever asks for a network now has the say) */
	asked_join_what = *what;
	asked_join = true;
	asked_leave = false;
	lck_mtx_unlock(&sta_lock);
	thread_wakeup((event_t)&sta_ops);
	return 0;
}

int
redwood_sta_ask_leave(void)
{
	lck_mtx_lock(&sta_lock);
	if (sta_ops == NULL) {
		lck_mtx_unlock(&sta_lock);
		return ENXIO;
	}
	asked_leave = true;
	asked_join = false;
	bzero(&asked_join_what, sizeof(asked_join_what));
	bzero(&fallback, sizeof(fallback));
	lck_mtx_unlock(&sta_lock);
	thread_wakeup((event_t)&sta_ops);
	return 0;
}

bool
redwood_sta_heard(unsigned index, struct redwood_sta_bss *bss)
{
	bool there;

	lck_mtx_lock(&sta_lock);
	if ((there = index < heard_count)) {
		*bss = heard[index];
	}
	lck_mtx_unlock(&sta_lock);
	return there;
}

void
redwood_sta_state(struct redwood_sta_state *state)
{
	const struct redwood_sta_ops *ops;

	bzero(state, sizeof(*state));
	lck_mtx_lock(&sta_lock);
	if ((ops = sta_ops) != NULL) {
		sta_reads++;
	}
	lck_mtx_unlock(&sta_lock);
	if (ops != NULL) {
		ops->state(state);
		lck_mtx_lock(&sta_lock);
		sta_reads--;
		lck_mtx_unlock(&sta_lock);
		thread_wakeup((event_t)&sta_calls);
	}
}

unsigned
redwood_sta_channels(struct redwood_sta_channel *out, unsigned most)
{
	unsigned count;

	lck_mtx_lock(&sta_lock);
	count = channel_count < most ? channel_count : most;
	memcpy(out, channels, count * sizeof(*out));
	lck_mtx_unlock(&sta_lock);
	return count;
}

bool
redwood_sta_address(uint8_t *address)
{
	const struct redwood_sta_ops *ops = sta_ops;

	if (ops != NULL) {
		memcpy(address, ops->address, 6);
	}
	return ops != NULL;
}

/* ---- QEMU's synthetic station ------------------------------------------------------------------------- */

/*
 * There is no WCN3990 behind QEMU's virt machine, so this backend answers the Apple80211 side as one
 * access point would: it advertises "HackiOS-QEMU" (open, 2.4 GHz channel 1) and accepts a join for
 * that name. Frames are not touched here: they go over the virtio-net link that redwood_virtio_net.c
 * has already attached, and redwood_net_link_state() lets configd start DHCP once the join is done.
 * It is compiled for every kernel but runs only with boot-arg wifi_qemu=1 on the qemu-virt profile.
 */
#define QEMU_STATION_SSID        "HackiOS-QEMU"
#define QEMU_STATION_SSID_LENGTH 11

static const uint8_t qemu_station_bssid[6] = { 0x02, 0x48, 0x41, 0x43, 0x4b, 0x01 };

static struct redwood_sta_bss qemu_station_bss;
static struct redwood_sta_state qemu_station_status;
static bool qemu_station_started;
static bool qemu_station_on;
static bool qemu_station_heard_any;

static void
qemu_station_phase(enum redwood_sta_phase phase, int error)
{
	lck_mtx_lock(&sta_lock);
	qemu_station_status.phase = phase;
	qemu_station_status.last_error = error;
	if (phase == REDWOOD_STA_JOINED) {
		qemu_station_status.bss = qemu_station_bss;
	} else if (phase == REDWOOD_STA_OFF || phase == REDWOOD_STA_IDLE) {
		bzero(&qemu_station_status.bss, sizeof(qemu_station_status.bss));
	}
	lck_mtx_unlock(&sta_lock);
}

static int
qemu_station_power(bool on)
{
	lck_mtx_lock(&sta_lock);
	qemu_station_on = on;
	lck_mtx_unlock(&sta_lock);
	redwood_net_link_state(false);
	qemu_station_phase(on ? REDWOOD_STA_IDLE : REDWOOD_STA_OFF, 0);
	if (!on) {
		printf("qemu-wifi: radio off\n");
	}
	return 0;
}

static int
qemu_station_scan(const struct redwood_sta_scan *what)
{
	bool match = what == NULL || what->ssid_length == 0 ||
	    (what->ssid_length == QEMU_STATION_SSID_LENGTH &&
	    memcmp(what->ssid, QEMU_STATION_SSID, QEMU_STATION_SSID_LENGTH) == 0);
	enum redwood_sta_phase before;

	lck_mtx_lock(&sta_lock);
	before = qemu_station_status.phase == REDWOOD_STA_JOINED ? REDWOOD_STA_JOINED : REDWOOD_STA_IDLE;
	lck_mtx_unlock(&sta_lock);
	qemu_station_phase(REDWOOD_STA_SCANNING, 0);
	/* (a radio needs a little while on each channel; this is one channel) */
	delay_for_interval(what != NULL && what->dwell_ms != 0 ? what->dwell_ms : 120, NSEC_PER_MSEC);
	lck_mtx_lock(&sta_lock);
	qemu_station_heard_any = match;
	qemu_station_bss.age_ms = 0;
	lck_mtx_unlock(&sta_lock);
	qemu_station_phase(before, 0);
	printf("qemu-wifi: scan of channel 1 heard %s\n", match ? "\"" QEMU_STATION_SSID "\"" : "nothing");
	return 0;
}

static void
qemu_station_heard(void (*each)(const struct redwood_sta_bss *bss, void *context), void *context)
{
	struct redwood_sta_bss bss;
	bool there;

	lck_mtx_lock(&sta_lock);
	there = qemu_station_heard_any;
	bss = qemu_station_bss;
	lck_mtx_unlock(&sta_lock);
	if (there) {
		each(&bss, context);
	}
}

static int
qemu_station_join(const struct redwood_sta_join *what)
{
	bool bssid_given = false;

	if (!qemu_station_on) {
		return ENETDOWN;
	}
	if (what == NULL || what->ssid_length != QEMU_STATION_SSID_LENGTH ||
	    memcmp(what->ssid, QEMU_STATION_SSID, QEMU_STATION_SSID_LENGTH) != 0) {
		printf("qemu-wifi: join refused: the synthetic station has no such network\n");
		return ENXIO;
	}
	for (unsigned i = 0; i < sizeof(what->bssid); i++) {
		bssid_given |= what->bssid[i] != 0;
	}
	if (bssid_given && memcmp(what->bssid, qemu_station_bssid, sizeof(qemu_station_bssid)) != 0) {
		return ENXIO;
	}
	if (what->security != REDWOOD_STA_OPEN) {
		printf("qemu-wifi: join refused: \"%s\" is advertised open\n", QEMU_STATION_SSID);
		return EAUTH;
	}
	/* (association takes a moment on a radio) */
	delay_for_interval(80, NSEC_PER_MSEC);
	qemu_station_phase(REDWOOD_STA_JOINED, 0);
	redwood_net_link_state(true);
	printf("qemu-wifi: joined \"%s\" (synthetic station; frames go over virtio-net)\n", QEMU_STATION_SSID);
	return 0;
}

static int
qemu_station_leave(void)
{
	qemu_station_phase(REDWOOD_STA_IDLE, 0);
	redwood_net_link_state(false);
	printf("qemu-wifi: left \"%s\"\n", QEMU_STATION_SSID);
	return 0;
}

static void
qemu_station_state(struct redwood_sta_state *state)
{
	lck_mtx_lock(&sta_lock);
	*state = qemu_station_status;
	lck_mtx_unlock(&sta_lock);
}

static unsigned
qemu_station_channels(struct redwood_sta_channel *channels, unsigned most)
{
	static const uint16_t frequencies[] = { 2412, 2437, 2462, 5180 };
	unsigned count = (unsigned)(sizeof(frequencies) / sizeof(frequencies[0]));

	if (count > most) {
		count = most;
	}
	for (unsigned i = 0; i < count; i++) {
		channels[i].frequency = frequencies[i];
		channels[i].flags = 0;
	}
	return count;
}

static const struct redwood_sta_ops qemu_station_ops = {
	.power = qemu_station_power,
	.scan = qemu_station_scan,
	.heard = qemu_station_heard,
	.join = qemu_station_join,
	.leave = qemu_station_leave,
	.state = qemu_station_state,
	.channels = qemu_station_channels,
	.address = { 0x02, 0x48, 0x41, 0x43, 0x4b, 0x02 },
};

void
redwood_sta_qemu_start(void)
{
	struct redwood_sta_join join;
	uint32_t enabled = 0;

	if (qemu_station_started) {
		return;
	}
	if (!hackios_platform_is("qemu-virt") ||
	    !PE_parse_boot_argn("wifi_qemu", &enabled, sizeof(enabled)) || enabled == 0) {
		return;
	}
	qemu_station_started = true;

	bzero(&qemu_station_bss, sizeof(qemu_station_bss));
	memcpy(qemu_station_bss.bssid, qemu_station_bssid, sizeof(qemu_station_bss.bssid));
	memcpy(qemu_station_bss.ssid, QEMU_STATION_SSID, QEMU_STATION_SSID_LENGTH);
	qemu_station_bss.ssid_length = QEMU_STATION_SSID_LENGTH;
	qemu_station_bss.frequency = 2412;
	qemu_station_bss.rssi = -42;
	qemu_station_bss.beacon_interval = 100;
	qemu_station_bss.capability = 0x0421;           /* ESS, short preamble, short slot: an open network */
	/* rates (1) and the channel (3), so that iOS shows the band and the basic rates */
	qemu_station_bss.elements[0] = 1;
	qemu_station_bss.elements[1] = 4;
	qemu_station_bss.elements[2] = 0x82;
	qemu_station_bss.elements[3] = 0x84;
	qemu_station_bss.elements[4] = 0x8b;
	qemu_station_bss.elements[5] = 0x96;
	qemu_station_bss.elements[6] = 3;
	qemu_station_bss.elements[7] = 1;
	qemu_station_bss.elements[8] = 1;
	qemu_station_bss.elements_length = 9;
	qemu_station_phase(REDWOOD_STA_OFF, 0);

	redwood_sta_attach(&qemu_station_ops);
	qemu_station_on = true;

	bzero(&join, sizeof(join));
	memcpy(join.ssid, QEMU_STATION_SSID, QEMU_STATION_SSID_LENGTH);
	join.ssid_length = QEMU_STATION_SSID_LENGTH;
	join.security = REDWOOD_STA_OPEN;
	if (redwood_sta_ask_join_standing(&join) != 0) {
		printf("qemu-wifi: the station thread did not take the join\n");
		return;
	}
	printf("qemu-wifi: synthetic station up: \"" QEMU_STATION_SSID "\", open, channel 1; not WCN3990\n");
}
