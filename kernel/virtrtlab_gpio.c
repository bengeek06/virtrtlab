// SPDX-License-Identifier: GPL-2.0-only
/*
 * virtrtlab_gpio.c — Virtual GPIO peripheral (8-line bank model)
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/device.h>
#include <linux/hrtimer.h>
#include <linux/init.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/notifier.h>
#include <linux/printk.h>
#include <linux/slab.h>
#include <linux/types.h>
#include <linux/workqueue.h>
#include "virtrtlab_core.h"

/* -------------------------------------------------------------------------
 * [1] Module parameter
 * -------------------------------------------------------------------------
 */

#define VIRTRTLAB_GPIO_MAX_BANKS	32
#define VIRTRTLAB_GPIO_MAX_LATENCY_NS	10000000000ULL
#define VIRTRTLAB_GPIO_MAX_PPM		1000000U

static unsigned int num_gpio_banks = 1;
module_param(num_gpio_banks, uint, 0444);
MODULE_PARM_DESC(num_gpio_banks,
		 "Number of GPIO bank instances (default: 1, range: 1..32)");

/* -------------------------------------------------------------------------
 * [2] Per-bank device structure
 * -------------------------------------------------------------------------
 */

/*
 * One instance per bank, heap-allocated.  'dev' must remain the first field
 * so that container_of(d, struct virtrtlab_gpio_dev, dev) is safe.
 *
 * Locking rules:
 *   ->lock protects every rw field listed below.
 *   stat_* fields are also under ->lock; callers that only increment a stat
 *   may hold ->lock in any context (spinlock-free here because sysfs store
 *   callbacks always run in process context).
 */
struct virtrtlab_gpio_dev {
	struct device		dev;		/* must be first */
	struct mutex		lock;

	int			index;		/* bank index N → device name "gpioN" */

	/* common fault / gate attrs */
	bool			enabled;	/* default true */
	u64			latency_ns;	/* default 0 */
	u64			jitter_ns;	/* default 0 */
	u32			drop_rate_ppm;	/* default 0 */
	u32			bitflip_rate_ppm; /* default 0 */

	/*
	 * GPIO-specific state.
	 *
	 * Note: @value is stored in the physical domain (pre-active_low), and
	 * sysfs helpers / edge logic apply @active_low as needed when exposing
	 * or interpreting logical state.
	 */
	u8			direction;	/* 1=AUT output, 0=AUT input; default 0x00 */
	u8			value;		/* current physical bank state; default 0x00 */
	u8			active_low;	/* per-bit inversion mask; default 0x00 */
	u8			edge_rising;	/* per-bit rising-edge enable; default 0x00 */
	u8			edge_falling;	/* per-bit falling-edge enable; default 0x00 */

	/* per-bank stats */
	u64			stat_value_changes; /* logical bit transitions applied */
	u64			stat_edge_events;   /* transitions matching edge masks */
	u64			stat_drops;	    /* suppressed writes (drop_rate_ppm) */

	/* bus event subscription */
	struct notifier_block	nb;

	/* delayed delivery — hrtimer fires, workqueue item applies the update */
	struct hrtimer		delay_timer;
	struct work_struct	apply_work;

	/*
	 * Snapshot captured atomically when a delayed value write is accepted
	 * (sysfs spec: "the kernel snapshots … before the sysfs store returns").
	 * Fields listed below match the spec explicitly:
	 *   requested_value, direction, active_low, bitflip_rate_ppm.
	 * latency_ns/jitter_ns are consumed for hrtimer setup, not stored.
	 * edge_rising/edge_falling are read from live state at apply() time.
	 *
	 * @snap_gen is incremented each time a new snapshot is taken.  It lets
	 * virtrtlab_gpio_apply() detect and discard stale dispatches:
	 *   - a duplicate apply (same work item running twice)
	 *   - an apply superseded by a newer snapshot accepted while computation
	 *     was in progress between the two lock acquisitions in apply().
	 * @apply_gen tracks the generation of the last successfully committed
	 * apply so that the stale-dispatch check is lock-safe.
	 */
	u8			snap_requested;
	u8			snap_direction;
	u8			snap_active_low;
	u32			snap_bitflip_ppm;
	u32			snap_gen;	/* generation counter — under ->lock */
	u32			apply_gen;	/* last committed generation — under ->lock */
};

#define to_gpio_dev(d)	container_of(d, struct virtrtlab_gpio_dev, dev)

/* Array of bank pointers; allocated in init, freed in exit. */
static struct virtrtlab_gpio_dev **gpio_banks;

/* -------------------------------------------------------------------------
 * [3] Mask format helper
 * -------------------------------------------------------------------------
 */

/*
 * virtrtlab_gpio_parse_mask - parse a strict "0xNN" hex mask from sysfs.
 *
 * The spec mandates exactly the form "0xNN" (or "0XNN") — two hex digits,
 * no padding, no short form, no decimal.  A trailing newline appended by
 * sysfs is tolerated and stripped before the length check.
 *
 * Returns 0 and writes *out on success; -EINVAL on any format violation.
 */
static int virtrtlab_gpio_parse_mask(const char *buf, u8 *out)
{
	size_t len = strlen(buf);
	u8 val = 0;
	int i;

	/* strip the trailing newline that sysfs unconditionally appends */
	if (len > 0 && buf[len - 1] == '\n')
		len--;

	/* strict length: "0xNN" == 4 chars */
	if (len != 4)
		return -EINVAL;

	/* mandatory "0x" or "0X" prefix */
	if (buf[0] != '0' || (buf[1] != 'x' && buf[1] != 'X'))
		return -EINVAL;

	/* exactly two hex nibbles — manual parse to avoid kstrtou8 ambiguities */
	for (i = 2; i < 4; i++) {
		char c = buf[i];

		val <<= 4;
		if (c >= '0' && c <= '9')
			val |= c - '0';
		else if (c >= 'a' && c <= 'f')
			val |= c - 'a' + 10;
		else if (c >= 'A' && c <= 'F')
			val |= c - 'A' + 10;
		else
			return -EINVAL;
	}

	*out = val;
	return 0;
}

/* -------------------------------------------------------------------------
 * [4] Common sysfs attrs
 * -------------------------------------------------------------------------
 */

static ssize_t type_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "gpio\n");
}
static DEVICE_ATTR_RO(type);

static ssize_t bus_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%s\n", VIRTRTLAB_DEFAULT_BUS_NAME);
}
static DEVICE_ATTR_RO(bus);

static ssize_t enabled_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	bool val;

	mutex_lock(&gdev->lock);
	val = gdev->enabled;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%d\n", val ? 1 : 0);
}

static ssize_t enabled_store(struct device *dev, struct device_attribute *attr,
			     const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	bool val;
	int ret;

	ret = kstrtobool(buf, &val);
	if (ret)
		return ret;
	mutex_lock(&gdev->lock);
	gdev->enabled = val;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(enabled);

static ssize_t latency_ns_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;

	mutex_lock(&gdev->lock);
	val = gdev->latency_ns;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}

static ssize_t latency_ns_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;
	int ret;

	ret = kstrtou64(buf, 0, &val);
	if (ret)
		return ret;
	if (val > VIRTRTLAB_GPIO_MAX_LATENCY_NS)
		return -EINVAL;
	mutex_lock(&gdev->lock);
	gdev->latency_ns = val;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(latency_ns);

static ssize_t jitter_ns_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;

	mutex_lock(&gdev->lock);
	val = gdev->jitter_ns;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}

static ssize_t jitter_ns_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;
	int ret;

	ret = kstrtou64(buf, 0, &val);
	if (ret)
		return ret;
	if (val > VIRTRTLAB_GPIO_MAX_LATENCY_NS)
		return -EINVAL;
	mutex_lock(&gdev->lock);
	gdev->jitter_ns = val;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(jitter_ns);

static ssize_t drop_rate_ppm_show(struct device *dev, struct device_attribute *attr,
				  char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u32 val;

	mutex_lock(&gdev->lock);
	val = gdev->drop_rate_ppm;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t drop_rate_ppm_store(struct device *dev, struct device_attribute *attr,
				   const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (val > VIRTRTLAB_GPIO_MAX_PPM)
		return -EINVAL;
	mutex_lock(&gdev->lock);
	gdev->drop_rate_ppm = val;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(drop_rate_ppm);

static ssize_t bitflip_rate_ppm_show(struct device *dev, struct device_attribute *attr,
				     char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u32 val;

	mutex_lock(&gdev->lock);
	val = gdev->bitflip_rate_ppm;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t bitflip_rate_ppm_store(struct device *dev, struct device_attribute *attr,
				      const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (val > VIRTRTLAB_GPIO_MAX_PPM)
		return -EINVAL;
	mutex_lock(&gdev->lock);
	gdev->bitflip_rate_ppm = val;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(bitflip_rate_ppm);

/* -------------------------------------------------------------------------
 * [5] GPIO-specific sysfs attrs
 * -------------------------------------------------------------------------
 */

static ssize_t direction_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;

	mutex_lock(&gdev->lock);
	val = gdev->direction;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "0x%02x\n", val);
}

static ssize_t direction_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;
	int ret;

	ret = virtrtlab_gpio_parse_mask(buf, &val);
	if (ret)
		return ret;
	mutex_lock(&gdev->lock);
	gdev->direction    = val;
	/* spec: output-owned bits are stored as 0 in the edge masks */
	gdev->edge_rising  &= ~val;
	gdev->edge_falling &= ~val;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(direction);

/* -------------------------------------------------------------------------
 * [6] Value write infrastructure
 *
 * Flow: value_store → (immediate) → virtrtlab_gpio_apply()
 *              or → hrtimer_start → timer_cb → schedule_work → virtrtlab_gpio_apply()
 *
 * virtrtlab_gpio_apply() is defined in [7], forward-declared here.
 * -------------------------------------------------------------------------
 */

/* Forward declaration — defined in [7] */
static void virtrtlab_gpio_apply(struct virtrtlab_gpio_dev *gdev);

/*
 * hrtimer callback — runs in hard-IRQ context; cannot take a mutex.
 * Hand off to the per-bank work_struct so apply() runs in process context.
 */
static enum hrtimer_restart virtrtlab_gpio_timer_cb(struct hrtimer *timer)
{
	struct virtrtlab_gpio_dev *gdev =
		container_of(timer, struct virtrtlab_gpio_dev, delay_timer);

	schedule_work(&gdev->apply_work);
	return HRTIMER_NORESTART;
}

/*
 * Work handler — process context; may sleep and take the mutex.
 */
static void virtrtlab_gpio_apply_work_fn(struct work_struct *work)
{
	struct virtrtlab_gpio_dev *gdev =
		container_of(work, struct virtrtlab_gpio_dev, apply_work);

	virtrtlab_gpio_apply(gdev);
}

static ssize_t value_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;

	mutex_lock(&gdev->lock);
	val = gdev->value ^ gdev->active_low;	/* physical → logical */
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "0x%02x\n", val);
}

static ssize_t value_store(struct device *dev, struct device_attribute *attr,
			   const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 latency_ns, jitter_ns, delay_ns;
	u32 drop_ppm;
	u8 requested;
	int ret;

	/* Step 1 — strict mask format */
	ret = virtrtlab_gpio_parse_mask(buf, &requested);
	if (ret)
		return ret;

	/*
	 * Step 2 — bus gate, then device gate (spec: both must be active).
	 * TOCTOU note: the bus may transition down between virtrtlab_bus_is_up()
	 * and mutex_lock().  For a simulator this is an acceptable race; the
	 * write is already in flight and the outcome is non-deterministic by
	 * design.  Production drivers would re-check under a bus-level lock.
	 */
	if (!virtrtlab_bus_is_up())
		return -EIO;

	mutex_lock(&gdev->lock);

	if (!gdev->enabled) {
		mutex_unlock(&gdev->lock);
		return -EIO;
	}

	/*
	 * Step 3 — drop decision.
	 * virtrtlab_bus_next_prng_u32() acquires a spinlock internally;
	 * calling it while holding a mutex (process context) is safe.
	 */
	drop_ppm = gdev->drop_rate_ppm;
	if (drop_ppm && (virtrtlab_bus_next_prng_u32() % 1000000U) < drop_ppm) {
		gdev->stat_drops++;
		mutex_unlock(&gdev->lock);
		return count;
	}

	/*
	 * Step 4 — snapshot (spec: "the kernel snapshots … before the sysfs
	 * store returns"; later attr writes do not affect an already-accepted
	 * delayed bank write).
	 */
	gdev->snap_requested   = requested;
	gdev->snap_direction   = gdev->direction;
	gdev->snap_active_low  = gdev->active_low;
	gdev->snap_bitflip_ppm = gdev->bitflip_rate_ppm;
	gdev->snap_gen++;          /* advance generation before unlocking */
	latency_ns             = gdev->latency_ns;
	jitter_ns              = gdev->jitter_ns;

	mutex_unlock(&gdev->lock);

	/* Step 5 — immediate or deferred delivery */
	if (latency_ns == 0 && jitter_ns == 0) {
		/* 5a — apply synchronously in the sysfs store context */
		virtrtlab_gpio_apply(gdev);
	} else {
		/*
		 * 5b — (re)arm the per-bank hrtimer.
		 *
		 * hrtimer_start() atomically cancels any already-pending timer
		 * and re-arms it; no explicit hrtimer_cancel() is needed here.
		 *
		 * We do NOT call cancel_work_sync() — that would block sysfs
		 * writers if an apply_work instance from a previous timer expiry
		 * is currently running, turning rapid consecutive writes into a
		 * serialised queue.  Instead, the snap_gen/apply_gen guard in
		 * virtrtlab_gpio_apply() detects and discards stale dispatches
		 * cheaply, without blocking the caller.
		 */
		delay_ns = latency_ns;
		if (jitter_ns) {
			/*
			 * Two u32 draws combined into a u64 so that the full
			 * [0, jitter_ns] range is reachable when jitter_ns > UINT32_MAX.
			 */
			u64 rnd = (u64)virtrtlab_bus_next_prng_u32() << 32 |
				  (u64)virtrtlab_bus_next_prng_u32();

			delay_ns += rnd % (jitter_ns + 1);
		}
		hrtimer_start(&gdev->delay_timer, ns_to_ktime(delay_ns),
			      HRTIMER_MODE_REL);
	}

	return count;
}
static DEVICE_ATTR_RW(value);

static ssize_t active_low_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;

	mutex_lock(&gdev->lock);
	val = gdev->active_low;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "0x%02x\n", val);
}

static ssize_t active_low_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;
	int ret;

	ret = virtrtlab_gpio_parse_mask(buf, &val);
	if (ret)
		return ret;
	mutex_lock(&gdev->lock);
	gdev->active_low = val;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(active_low);

static ssize_t edge_rising_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;

	mutex_lock(&gdev->lock);
	val = gdev->edge_rising;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "0x%02x\n", val);
}

static ssize_t edge_rising_store(struct device *dev, struct device_attribute *attr,
				 const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;
	int ret;

	ret = virtrtlab_gpio_parse_mask(buf, &val);
	if (ret)
		return ret;
	mutex_lock(&gdev->lock);
	/* spec: output-owned bits are always stored as 0 */
	gdev->edge_rising = val & ~gdev->direction;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(edge_rising);

static ssize_t edge_falling_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;

	mutex_lock(&gdev->lock);
	val = gdev->edge_falling;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "0x%02x\n", val);
}

static ssize_t edge_falling_store(struct device *dev, struct device_attribute *attr,
				  const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u8 val;
	int ret;

	ret = virtrtlab_gpio_parse_mask(buf, &val);
	if (ret)
		return ret;
	mutex_lock(&gdev->lock);
	/* spec: output-owned bits are always stored as 0 */
	gdev->edge_falling = val & ~gdev->direction;
	mutex_unlock(&gdev->lock);
	return count;
}
static DEVICE_ATTR_RW(edge_falling);

/* -------------------------------------------------------------------------
 * [7] Apply bank value — called in process context (directly or via workqueue)
 * -------------------------------------------------------------------------
 */

/*
 * virtrtlab_gpio_apply - commit a snapshotted bank write to the logical state.
 *
 * Uses the snapshot fields captured in value_store() (snap_requested,
 * snap_direction, snap_active_low, snap_bitflip_ppm) so spec-required
 * isolation from later sysfs writes is guaranteed.
 *
 * edge_rising and edge_falling are read from live state at apply time —
 * this is deliberate: the spec only snapshots the four fields listed above.
 * Called with no locks held.
 */
static void virtrtlab_gpio_apply(struct virtrtlab_gpio_dev *gdev)
{
	u8 input_mask, flipped, new_val, new_logical, old_val, changed;
	u8 rising_events, falling_events;
	u8 snap_req, snap_dir, snap_al, edge_r, edge_f;
	u32 bitflip_ppm;
	u32 gen;

	mutex_lock(&gdev->lock);

	gen = gdev->snap_gen;
	if (gen == gdev->apply_gen) {
		/*
		 * Stale dispatch: either this snapshot was already committed by
		 * a previous run of this work item, or no deferred write is
		 * pending.  Drop silently.
		 */
		mutex_unlock(&gdev->lock);
		return;
	}

	snap_req     = gdev->snap_requested;
	snap_dir     = gdev->snap_direction;
	snap_al      = gdev->snap_active_low;
	bitflip_ppm  = gdev->snap_bitflip_ppm;
	edge_r       = gdev->edge_rising;
	edge_f       = gdev->edge_falling;
	old_val      = gdev->value;			/* physical */

	mutex_unlock(&gdev->lock);

	/*
	 * a. bitflip decision — flip one random AUT-input bit.
	 *    input_mask selects the bits that userspace may drive.
	 *    If there are no input bits, a flip is a no-op.
	 */
	input_mask = ~snap_dir & 0xFF;
	flipped    = snap_req;

	if (bitflip_ppm && input_mask) {
		/*
		 * Spec: "one PRNG draw per bitflip decision".
		 *
		 * A single u32 draw is split into two independent fields:
		 *   gate:      (rnd % 1000000U) < bitflip_ppm
		 *              Uniform in [0, 999 999]; gives exactly the
		 *              requested ppm probability with no bias.
		 *   bit index: (rnd / 1000000U) % hweight8(input_mask)
		 *              Quotient lies in [0, 4294]; bias from the
		 *              modulo is negligible for up to 8 active lines.
		 */
		u32 rnd    = virtrtlab_bus_next_prng_u32();
		u8 m       = input_mask;
		u32 bit;	/* u32 avoids silent wrap-around after 8 left-shifts */
		u8 i;

		if ((rnd % 1000000U) < bitflip_ppm) {
			u8 bit_idx = (u8)((rnd / 1000000U) % hweight8(input_mask));

			for (i = 0, bit = 1; m; bit <<= 1) {
				if (!(m & bit))
					continue;
				m &= ~bit;
				if (i++ == bit_idx) {
					flipped ^= bit;
					break;
				}
			}
		}
	}

	/*
	 * b-c. Merge: AUT-output bits keep their current physical value;
	 *       AUT-input bits receive the (possibly flipped) logical value
	 *       converted to the physical domain via snap_al.
	 *
	 * Direction-change race note: if direction changed between value_store()
	 * and now, snap_dir reflects the old ownership.  Bits that became outputs
	 * after the snapshot will be temporarily overwritten but will be
	 * corrected on the next AUT-driven output transition.  This is acceptable
	 * for v0.1.0; strict prevention would require cancelling the pending
	 * write whenever direction changes.
	 */
	new_val = (old_val & snap_dir) | ((flipped ^ snap_al) & input_mask);

	/*
	 * d. Detect bit transitions on input bits only.
	 *    changed is the same in both physical and logical domains because
	 *    XOR-ing by the static snap_al mask cancels out.
	 */
	changed = (new_val ^ old_val) & input_mask;

	/*
	 * e-g. Update counters and commit under lock.
	 *
	 * Edge detection operates in the logical domain (physical XOR snap_al).
	 * rising  = changed AND new logical 1 AND rising-edge detection enabled
	 * falling = changed AND new logical 0 AND falling-edge detection enabled
	 */
	new_logical    = new_val ^ snap_al;
	rising_events  = changed & new_logical  & edge_r;
	falling_events = changed & ~new_logical & edge_f;

	mutex_lock(&gdev->lock);
	/*
	 * Guard: a newer snapshot may have been accepted while we were
	 * computing (between the two lock acquisitions).  If so, our result
	 * is stale — discard it and let the newer dispatch win.
	 */
	if (gdev->snap_gen != gen) {
		mutex_unlock(&gdev->lock);
		return;
	}
	gdev->apply_gen = gen;
	gdev->value = new_val;
	gdev->stat_value_changes += hweight8(changed);
	gdev->stat_edge_events   += hweight8(rising_events) + hweight8(falling_events);
	mutex_unlock(&gdev->lock);
}

/* -------------------------------------------------------------------------
 * [8] Stats subdir attributes
 * -------------------------------------------------------------------------
 */

static ssize_t value_changes_show(struct device *dev, struct device_attribute *attr,
				  char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;

	mutex_lock(&gdev->lock);
	val = gdev->stat_value_changes;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}
static DEVICE_ATTR_RO(value_changes);

static ssize_t edge_events_show(struct device *dev, struct device_attribute *attr,
				char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;

	mutex_lock(&gdev->lock);
	val = gdev->stat_edge_events;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}
static DEVICE_ATTR_RO(edge_events);

static ssize_t drops_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;

	mutex_lock(&gdev->lock);
	val = gdev->stat_drops;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}
static DEVICE_ATTR_RO(drops);

/*
 * stats/reset — write-only; reading returns -EPERM.
 * Only the value "0" is accepted; any other value returns -EINVAL.
 * All three counters are reset atomically under a single lock acquisition.
 */
static ssize_t reset_store(struct device *dev, struct device_attribute *attr,
			   const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (val != 0)
		return -EINVAL;

	mutex_lock(&gdev->lock);
	gdev->stat_value_changes = 0;
	gdev->stat_edge_events   = 0;
	gdev->stat_drops         = 0;
	mutex_unlock(&gdev->lock);
	return count;
}

/* No show callback — reading returns -EPERM automatically. */
static DEVICE_ATTR_WO(reset);

/* -------------------------------------------------------------------------
 * [9] Attribute groups
 * -------------------------------------------------------------------------
 */

static struct attribute *gpio_dev_attrs[] = {
	&dev_attr_type.attr,
	&dev_attr_bus.attr,
	&dev_attr_enabled.attr,
	&dev_attr_latency_ns.attr,
	&dev_attr_jitter_ns.attr,
	&dev_attr_drop_rate_ppm.attr,
	&dev_attr_bitflip_rate_ppm.attr,
	&dev_attr_direction.attr,
	&dev_attr_value.attr,
	&dev_attr_active_low.attr,
	&dev_attr_edge_rising.attr,
	&dev_attr_edge_falling.attr,
	NULL,
};

static const struct attribute_group gpio_dev_group = {
	.attrs = gpio_dev_attrs,
};

static struct attribute *gpio_stats_attrs[] = {
	&dev_attr_value_changes.attr,
	&dev_attr_edge_events.attr,
	&dev_attr_drops.attr,
	&dev_attr_reset.attr,
	NULL,
};

static const struct attribute_group gpio_stats_group = {
	.name  = "stats",
	.attrs = gpio_stats_attrs,
};

static const struct attribute_group *gpio_dev_groups[] = {
	&gpio_dev_group,
	&gpio_stats_group,
	NULL,
};

/* -------------------------------------------------------------------------
 * [10] Bus notifier
 * -------------------------------------------------------------------------
 */

static int virtrtlab_gpio_bus_notifier_call(struct notifier_block *nb,
					    unsigned long event, void *data)
{
	struct virtrtlab_gpio_dev *gdev =
		container_of(nb, struct virtrtlab_gpio_dev, nb);

	if (event != VIRTRTLAB_BUS_EVENT_RESET)
		return NOTIFY_DONE;

	/*
	 * RESET semantics (spec §sysfs state table):
	 *   - cancel any pending delayed bank write
	 *   - clear fault attrs to 0
	 *   - reset all stats counters to 0
	 *   - set enabled = true
	 *   - preserve: direction, active_low, edge_rising, edge_falling, value
	 */
	hrtimer_cancel(&gdev->delay_timer);
	cancel_work_sync(&gdev->apply_work);

	mutex_lock(&gdev->lock);
	gdev->latency_ns        = 0;
	gdev->jitter_ns         = 0;
	gdev->drop_rate_ppm     = 0;
	gdev->bitflip_rate_ppm  = 0;
	gdev->stat_value_changes = 0;
	gdev->stat_edge_events   = 0;
	gdev->stat_drops         = 0;
	gdev->enabled            = true;
	/*
	 * Mark the current snapshot generation as already committed so that
	 * any apply() instance that squeezed past cancel_work_sync() sees a
	 * stale-dispatch and exits without corrupting the just-reset state.
	 */
	gdev->apply_gen = gdev->snap_gen;
	mutex_unlock(&gdev->lock);

	return NOTIFY_OK;
}

/* -------------------------------------------------------------------------
 * [11] Module init / exit
 * -------------------------------------------------------------------------
 */

static void virtrtlab_gpio_dev_release(struct device *dev)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);

	mutex_destroy(&gdev->lock);
	kfree(gdev);
}

static int __init virtrtlab_gpio_init(void)
{
	int n, ret;

	if (num_gpio_banks == 0 || num_gpio_banks > VIRTRTLAB_GPIO_MAX_BANKS) {
		pr_err("num_gpio_banks=%u out of range [1..%d]\n",
		       num_gpio_banks, VIRTRTLAB_GPIO_MAX_BANKS);
		return -EINVAL;
	}

	gpio_banks = kcalloc(num_gpio_banks, sizeof(*gpio_banks), GFP_KERNEL);
	if (!gpio_banks)
		return -ENOMEM;

	for (n = 0; n < (int)num_gpio_banks; n++) {
		struct virtrtlab_gpio_dev *gdev;

		gdev = kzalloc(sizeof(*gdev), GFP_KERNEL);
		if (!gdev) {
			ret = -ENOMEM;
			goto err_unwind;
		}

		mutex_init(&gdev->lock);
		gdev->index   = n;
		gdev->enabled = true;	/* all other fields zero-initialised by kzalloc */

		hrtimer_init(&gdev->delay_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
		gdev->delay_timer.function = virtrtlab_gpio_timer_cb;
		INIT_WORK(&gdev->apply_work, virtrtlab_gpio_apply_work_fn);

		gdev->nb.notifier_call = virtrtlab_gpio_bus_notifier_call;

		device_initialize(&gdev->dev);
		gdev->dev.bus     = &virtrtlab_bus_type;
		gdev->dev.release = virtrtlab_gpio_dev_release;
		gdev->dev.groups  = gpio_dev_groups;
		gdev->dev.kobj.parent = virtrtlab_devices_kobj;
		dev_set_name(&gdev->dev, "gpio%d", n);

		ret = device_add(&gdev->dev);
		if (ret) {
			pr_err("failed to register gpio%d: %d\n", n, ret);
			hrtimer_cancel(&gdev->delay_timer);
			cancel_work_sync(&gdev->apply_work);
			/* device_initialize() took a kref; put_device triggers release */
			put_device(&gdev->dev);
			goto err_unwind;
		}

		ret = virtrtlab_bus_register_notifier(&gdev->nb);
		if (ret) {
			pr_err("failed to register notifier for gpio%d: %d\n", n, ret);
			hrtimer_cancel(&gdev->delay_timer);
			cancel_work_sync(&gdev->apply_work);
			device_unregister(&gdev->dev);
			goto err_unwind;
		}

		gpio_banks[n] = gdev;
		pr_info("gpio%d registered on virtrtlab bus\n", n);
	}

	return 0;

err_unwind:
	/* unwind in reverse order — only the successfully registered banks */
	while (--n >= 0) {
		hrtimer_cancel(&gpio_banks[n]->delay_timer);
		cancel_work_sync(&gpio_banks[n]->apply_work);
		virtrtlab_bus_unregister_notifier(&gpio_banks[n]->nb);
		device_unregister(&gpio_banks[n]->dev);
	}
	kfree(gpio_banks);
	return ret;
}

static void __exit virtrtlab_gpio_exit(void)
{
	int n;

	for (n = (int)num_gpio_banks - 1; n >= 0; n--) {
		hrtimer_cancel(&gpio_banks[n]->delay_timer);
		cancel_work_sync(&gpio_banks[n]->apply_work);
		virtrtlab_bus_unregister_notifier(&gpio_banks[n]->nb);
		/*
		 * device_unregister() removes the device from bus and sysfs,
		 * then calls put_device() which triggers virtrtlab_gpio_dev_release()
		 * → kfree().
		 */
		device_unregister(&gpio_banks[n]->dev);
		pr_info("gpio%d unregistered\n", n);
	}

	kfree(gpio_banks);
}

module_init(virtrtlab_gpio_init);
module_exit(virtrtlab_gpio_exit);

MODULE_SOFTDEP("pre: virtrtlab_core");
MODULE_DESCRIPTION("VirtRTLab GPIO peripheral — 8-line bank model");
MODULE_AUTHOR("VirtRTLab");
MODULE_LICENSE("GPL");
