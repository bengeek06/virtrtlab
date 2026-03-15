// SPDX-License-Identifier: GPL-2.0-only
/*
 * virtrtlab_gpio.c — Virtual GPIO peripheral (native gpio_chip, per-line inject)
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/device.h>
#include <linux/gpio/driver.h>
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
 * [1] Module parameters and constants
 * -------------------------------------------------------------------------
 */

#define VIRTRTLAB_GPIO_MAX_DEVS		32
#define VIRTRTLAB_GPIO_LINES		8
/*
 * Both latency_ns and jitter_ns are individually bounded by this constant.
 * The maximum combined delivery delay is therefore 2× this value (20 seconds).
 */
#define VIRTRTLAB_GPIO_MAX_LATENCY_NS	10000000000ULL
#define VIRTRTLAB_GPIO_MAX_PPM		1000000U

static unsigned int num_gpio_devs = 1;
module_param(num_gpio_devs, uint, 0444);
MODULE_PARM_DESC(num_gpio_devs,
		 "Number of GPIO device instances (default: 1, range: 1..32)");

/* -------------------------------------------------------------------------
 * [2] Per-device structure
 * -------------------------------------------------------------------------
 */

/*
 * Per-line state.  One entry per physical GPIO line.
 * All fields protected by the parent struct virtrtlab_gpio_dev::lock.
 *
 * @parent:    back-pointer to the owning device (set once in init, immutable).
 * @index:     line position within lines[]; used to dispatch apply from work_fn.
 * @snap_gen:  incremented each time inject_store() accepts a write snapshot.
 * @apply_gen: generation of the last successfully committed inject.
 *             apply_gen == snap_gen means no pending or stale work.
 */
struct virtrtlab_gpio_line {
	struct virtrtlab_gpio_dev *parent;	/* back-pointer; immutable after init */
	unsigned int		index;		/* line index [0, VIRTRTLAB_GPIO_LINES) */

	u8			value;		/* current physical line state (0 or 1) */
	bool			is_output;	/* true when AUT owns line as output */

	/* delayed delivery infrastructure */
	struct hrtimer		delay_timer;
	struct work_struct	apply_work;

	/* snapshot captured atomically when inject_store() accepts a write */
	u8			snap_value;		/* requested physical value */
	u32			snap_bitflip_ppm;
	u32			snap_gen;
	u32			apply_gen;
};

/*
 * One instance per gpioN device, heap-allocated.  'dev' must remain the
 * first field so that container_of(d, struct virtrtlab_gpio_dev, dev) is
 * safe for device_unregister() and sysfs attribute callbacks.
 *
 * Locking rules:
 *   ->lock protects every rw field in this struct and all fields in ->lines[].
 *   Changes to ->lines[i] in per-line hrtimer callbacks are forbidden
 *   (hard-IRQ context); those callbacks only schedule apply_work, which runs
 *   in process context under ->lock.
 */
struct virtrtlab_gpio_dev {
	struct device		dev;		/* must be first */
	struct mutex		lock;

	int			index;		/* device index N → name "gpioN" */

	/* common fault / gate attrs */
	bool			enabled;	/* default true */
	u64			latency_ns;	/* default 0 */
	u64			jitter_ns;	/* default 0 */
	u32			drop_rate_ppm;	/* default 0 */
	u32			bitflip_rate_ppm; /* default 0 */

	/* native GPIO chip — registered via gpiochip_add_data() */
	struct gpio_chip	gc;
	char			chip_path[32];	/* "/dev/gpiochipN" set after registration */

	/* per-line state */
	struct virtrtlab_gpio_line lines[VIRTRTLAB_GPIO_LINES];

	/* stats */
	u64			stat_value_changes;
	u64			stat_drops;
	u64			stat_bitflips;

	/* bus event subscription */
	struct notifier_block	nb;
};

#define to_gpio_dev(d)	container_of(d, struct virtrtlab_gpio_dev, dev)

/* Array of device pointers; allocated in init, freed in exit. */
static struct virtrtlab_gpio_dev **gpio_devs;

/* -------------------------------------------------------------------------
 * [3] gpio_chip callbacks — called from gpiolib / GPIO char device
 * -------------------------------------------------------------------------
 */

static int virtrtlab_gpio_direction_input(struct gpio_chip *gc, unsigned int offset)
{
	struct virtrtlab_gpio_dev *gdev = gpiochip_get_data(gc);

	mutex_lock(&gdev->lock);
	gdev->lines[offset].is_output = false;
	mutex_unlock(&gdev->lock);
	return 0;
}

static int virtrtlab_gpio_direction_output(struct gpio_chip *gc, unsigned int offset,
					   int value)
{
	struct virtrtlab_gpio_dev *gdev = gpiochip_get_data(gc);

	mutex_lock(&gdev->lock);
	gdev->lines[offset].is_output = true;
	gdev->lines[offset].value = value ? 1 : 0;
	mutex_unlock(&gdev->lock);
	return 0;
}

static int virtrtlab_gpio_get(struct gpio_chip *gc, unsigned int offset)
{
	struct virtrtlab_gpio_dev *gdev = gpiochip_get_data(gc);
	int val;

	mutex_lock(&gdev->lock);
	val = gdev->lines[offset].value;
	mutex_unlock(&gdev->lock);
	return val;
}

static int virtrtlab_gpio_set(struct gpio_chip *gc, unsigned int offset, int value)
{
	struct virtrtlab_gpio_dev *gdev = gpiochip_get_data(gc);

	mutex_lock(&gdev->lock);
	if (gdev->lines[offset].is_output)
		gdev->lines[offset].value = value ? 1 : 0;
	mutex_unlock(&gdev->lock);
	return 0;
}

static int virtrtlab_gpio_get_multiple(struct gpio_chip *gc,
				       unsigned long *mask, unsigned long *bits)
{
	struct virtrtlab_gpio_dev *gdev = gpiochip_get_data(gc);
	unsigned int i;

	mutex_lock(&gdev->lock);
	*bits = 0;
	for_each_set_bit(i, mask, VIRTRTLAB_GPIO_LINES)
		if (gdev->lines[i].value)
			set_bit(i, bits);
	mutex_unlock(&gdev->lock);
	return 0;
}

static int virtrtlab_gpio_set_multiple(struct gpio_chip *gc,
				       unsigned long *mask, unsigned long *bits)
{
	struct virtrtlab_gpio_dev *gdev = gpiochip_get_data(gc);
	unsigned int i;

	mutex_lock(&gdev->lock);
	for_each_set_bit(i, mask, VIRTRTLAB_GPIO_LINES) {
		if (gdev->lines[i].is_output)
			gdev->lines[i].value = test_bit(i, bits) ? 1 : 0;
	}
	mutex_unlock(&gdev->lock);
	return 0;
}

static int virtrtlab_gpio_get_direction(struct gpio_chip *gc, unsigned int offset)
{
	struct virtrtlab_gpio_dev *gdev = gpiochip_get_data(gc);
	int dir;

	mutex_lock(&gdev->lock);
	dir = gdev->lines[offset].is_output ?
	      GPIO_LINE_DIRECTION_OUT : GPIO_LINE_DIRECTION_IN;
	mutex_unlock(&gdev->lock);
	return dir;
}

/* -------------------------------------------------------------------------
 * [4] Per-line apply — process context (directly or via workqueue)
 * -------------------------------------------------------------------------
 */

/*
 * virtrtlab_gpio_line_apply - commit an inject snapshot to one physical line.
 *
 * Implements the bitflip gate and state commit.  The enabled gate and drop
 * gate are evaluated earlier in inject_store() before the snapshot is taken.
 * Called with no locks held.
 *
 * Design (spec §inject attr): inject on a line currently owned by the AUT as
 * output goes through the full 7-step shim (drop / bitflip / latency) so PRNG
 * draws remain deterministic, but the commit step is silently skipped — the
 * AUT remains authoritative for the line state.  apply_gen is still advanced
 * so that stale-dispatch properly terminates.
 */
static void virtrtlab_gpio_line_apply(struct virtrtlab_gpio_dev *gdev,
				      unsigned int line_idx)
{
	struct virtrtlab_gpio_line *line = &gdev->lines[line_idx];
	u8 snap_val, new_val, old_val;
	u32 bitflip_ppm, gen;

	mutex_lock(&gdev->lock);

	gen = line->snap_gen;
	if (gen == line->apply_gen) {
		/* stale dispatch — already committed or no write pending */
		mutex_unlock(&gdev->lock);
		return;
	}

	snap_val    = line->snap_value;
	bitflip_ppm = line->snap_bitflip_ppm;

	mutex_unlock(&gdev->lock);

	/*
	 * Bitflip gate — one PRNG draw per inject write.
	 * Spec: "one PRNG draw per bitflip decision"; the flip is applied to the
	 * injected value regardless of line direction.
	 */
	new_val = snap_val;
	if (bitflip_ppm && (virtrtlab_bus_next_prng_u32() % 1000000U) < bitflip_ppm)
		new_val ^= 1;

	mutex_lock(&gdev->lock);
	/*
	 * Guard: a newer snapshot may have arrived while we were computing
	 * (between the two lock acquisitions).  If so, our result is stale —
	 * let the newer dispatch win.
	 */
	if (line->snap_gen != gen) {
		mutex_unlock(&gdev->lock);
		return;
	}
	old_val = line->value;
	line->apply_gen = gen;
	/*
	 * Commit only for input lines.  Inject on an AUT-owned output line is
	 * accepted by the shim (PRNG draws consumed, drop/latency honoured) but
	 * the stored line value is not overwritten — the AUT is authoritative.
	 * stat_bitflips is still updated when the bitflip gate fires regardless
	 * of line direction: it counts gate fires, not observable state changes.
	 */
	if (new_val != snap_val)
		gdev->stat_bitflips++;
	if (!line->is_output) {
		line->value = new_val;
		if (new_val != old_val)
			gdev->stat_value_changes++;
	}
	mutex_unlock(&gdev->lock);
}

/*
 * hrtimer callback — hard-IRQ context; cannot take mutex.
 * Recovers the line via container_of; the line carries its own apply_work.
 */
static enum hrtimer_restart virtrtlab_gpio_timer_cb(struct hrtimer *timer)
{
	struct virtrtlab_gpio_line *line =
		container_of(timer, struct virtrtlab_gpio_line, delay_timer);

	schedule_work(&line->apply_work);
	return HRTIMER_NORESTART;
}

/* Work handler — process context; may sleep and take the mutex. */
static void virtrtlab_gpio_apply_work_fn(struct work_struct *work)
{
	struct virtrtlab_gpio_line *line =
		container_of(work, struct virtrtlab_gpio_line, apply_work);

	virtrtlab_gpio_line_apply(line->parent, line->index);
}

/* -------------------------------------------------------------------------
 * [5] inject sysfs attr — harness injection surface
 * -------------------------------------------------------------------------
 */

/*
 * virtrtlab_gpio_parse_inject - parse the inject attr format "N:V".
 *
 * N is the line index [0, VIRTRTLAB_GPIO_LINES); V is '0' or '1'.
 * A trailing newline is tolerated.
 *
 * Returns 0 on success; -EINVAL on any format or range error.
 * All validation errors are normalised to -EINVAL so that userspace receives a
 * consistent errno on any malformed write, matching sysfs convention.
 */
static int virtrtlab_gpio_parse_inject(const char *buf, size_t count,
				       unsigned int *line_idx, u8 *val)
{
	const char *sep;
	char idx_str[4];
	unsigned long idx;
	size_t idx_len;
	size_t len = count;
	int ret;

	if (len > 0 && buf[len - 1] == '\n')
		len--;

	sep = memchr(buf, ':', len);
	if (!sep || sep == buf)
		return -EINVAL;

	/* V must be exactly one character after ':' */
	if (buf + len != sep + 2)
		return -EINVAL;

	if (sep[1] != '0' && sep[1] != '1')
		return -EINVAL;

	idx_len = sep - buf;
	if (idx_len == 0 || idx_len >= sizeof(idx_str))
		return -EINVAL;

	memcpy(idx_str, buf, idx_len);
	idx_str[idx_len] = '\0';
	ret = kstrtoul(idx_str, 10, &idx);
	if (ret)
		return -EINVAL;

	if (idx >= VIRTRTLAB_GPIO_LINES)
		return -EINVAL;

	*line_idx = (unsigned int)idx;
	*val = sep[1] - '0';
	return 0;
}

static ssize_t inject_store(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 latency_ns, jitter_ns, delay_ns;
	u32 drop_ppm;
	unsigned int line_idx;
	u8 val;
	int ret;

	ret = virtrtlab_gpio_parse_inject(buf, count, &line_idx, &val);
	if (ret)
		return ret;

	/*
	 * Inject semantics (spec §inject attr):
	 *   - bus gate, enabled gate, drop gate, latency, and bitflip shim all
	 *     apply regardless of line direction.
	 *   - commit (line_apply step 6) is skipped silently for AUT-owned output
	 *     lines; the write to sysfs still returns count (success).
	 *
	 * Bus gate — checked before acquiring gdev->lock.
	 * TOCTOU note: same reasoning as v0.1.0 value_store(); acceptable race
	 * for a simulator — production drivers re-check under a bus-level lock.
	 */
	if (!virtrtlab_bus_is_up())
		return -EIO;

	mutex_lock(&gdev->lock);

	/* enabled gate — silently discard if shim is disabled */
	if (!gdev->enabled) {
		mutex_unlock(&gdev->lock);
		return count;
	}

	/* drop gate — one PRNG draw */
	drop_ppm = gdev->drop_rate_ppm;
	if (drop_ppm && (virtrtlab_bus_next_prng_u32() % 1000000U) < drop_ppm) {
		gdev->stat_drops++;
		mutex_unlock(&gdev->lock);
		return count;
	}

	/*
	 * Snapshot — captured before the mutex is released so that concurrent
	 * sysfs writes to fault attrs cannot retroactively affect this inject.
	 */
	gdev->lines[line_idx].snap_value       = val;
	gdev->lines[line_idx].snap_bitflip_ppm = gdev->bitflip_rate_ppm;
	gdev->lines[line_idx].snap_gen++;
	latency_ns = gdev->latency_ns;
	jitter_ns  = gdev->jitter_ns;

	mutex_unlock(&gdev->lock);

	/* immediate or deferred commit */
	if (latency_ns == 0 && jitter_ns == 0) {
		virtrtlab_gpio_line_apply(gdev, line_idx);
	} else {
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
		hrtimer_start(&gdev->lines[line_idx].delay_timer,
			      ns_to_ktime(delay_ns), HRTIMER_MODE_REL);
	}

	return count;
}
static DEVICE_ATTR_WO(inject);

/* -------------------------------------------------------------------------
 * [6] Common sysfs attrs
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
 * [7] GPIO identity attrs
 * -------------------------------------------------------------------------
 */

static ssize_t num_lines_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%u\n", VIRTRTLAB_GPIO_LINES);
}
static DEVICE_ATTR_RO(num_lines);

static ssize_t chip_path_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);

	return sysfs_emit(buf, "%s\n", gdev->chip_path);
}
static DEVICE_ATTR_RO(chip_path);

/* -------------------------------------------------------------------------
 * [8] Stats subdir attributes
 *
 * Note: all counters aggregate events across all 8 lines of the device.
 * Per-line attribution is not available in v0.2.0.
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

static ssize_t bitflips_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_gpio_dev *gdev = to_gpio_dev(dev);
	u64 val;

	mutex_lock(&gdev->lock);
	val = gdev->stat_bitflips;
	mutex_unlock(&gdev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}
static DEVICE_ATTR_RO(bitflips);

/*
 * stats/reset — write-only; reading returns -EPERM.
 * Only the value "0" is accepted; any other value returns -EINVAL.
 * All counters are reset atomically under a single lock acquisition.
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
	gdev->stat_drops         = 0;
	gdev->stat_bitflips      = 0;
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
	&dev_attr_num_lines.attr,
	&dev_attr_chip_path.attr,
	&dev_attr_inject.attr,
	&dev_attr_enabled.attr,
	&dev_attr_latency_ns.attr,
	&dev_attr_jitter_ns.attr,
	&dev_attr_drop_rate_ppm.attr,
	&dev_attr_bitflip_rate_ppm.attr,
	NULL,
};

static const struct attribute_group gpio_dev_group = {
	.attrs = gpio_dev_attrs,
};

static struct attribute *gpio_stats_attrs[] = {
	&dev_attr_value_changes.attr,
	&dev_attr_drops.attr,
	&dev_attr_bitflips.attr,
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
	int i;

	if (event != VIRTRTLAB_BUS_EVENT_RESET)
		return NOTIFY_DONE;

	/*
	 * RESET semantics (spec §sysfs state table):
	 *   - cancel any pending delayed per-line inject
	 *   - clear fault attrs to 0
	 *   - reset all stats counters to 0
	 *   - set enabled = true
	 *   - preserve: per-line value, is_output
	 */
	for (i = 0; i < VIRTRTLAB_GPIO_LINES; i++) {
		hrtimer_cancel(&gdev->lines[i].delay_timer);
		cancel_work_sync(&gdev->lines[i].apply_work);
	}

	mutex_lock(&gdev->lock);
	gdev->latency_ns         = 0;
	gdev->jitter_ns          = 0;
	gdev->drop_rate_ppm      = 0;
	gdev->bitflip_rate_ppm   = 0;
	gdev->stat_value_changes = 0;
	gdev->stat_drops         = 0;
	gdev->stat_bitflips      = 0;
	gdev->enabled            = true;
	/*
	 * Mark each line's current snapshot generation as already committed
	 * so that any apply() instance that squeezed past cancel_work_sync()
	 * sees a stale-dispatch and exits without corrupting the reset state.
	 */
	for (i = 0; i < VIRTRTLAB_GPIO_LINES; i++)
		gdev->lines[i].apply_gen = gdev->lines[i].snap_gen;
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
	int n, i, ret;

	if (num_gpio_devs == 0 || num_gpio_devs > VIRTRTLAB_GPIO_MAX_DEVS) {
		pr_err("num_gpio_devs=%u out of range [1..%d]\n",
		       num_gpio_devs, VIRTRTLAB_GPIO_MAX_DEVS);
		return -EINVAL;
	}

	gpio_devs = kcalloc(num_gpio_devs, sizeof(*gpio_devs), GFP_KERNEL);
	if (!gpio_devs)
		return -ENOMEM;

	for (n = 0; n < (int)num_gpio_devs; n++) {
		struct virtrtlab_gpio_dev *gdev;

		gdev = kzalloc(sizeof(*gdev), GFP_KERNEL);
		if (!gdev) {
			ret = -ENOMEM;
			goto err_unwind;
		}

		mutex_init(&gdev->lock);
		gdev->index   = n;
		gdev->enabled = true;	/* all other fields zero-initialised by kzalloc */

		for (i = 0; i < VIRTRTLAB_GPIO_LINES; i++) {
			gdev->lines[i].parent = gdev;
			gdev->lines[i].index  = i;
			hrtimer_setup(&gdev->lines[i].delay_timer,
				      virtrtlab_gpio_timer_cb,
				      CLOCK_MONOTONIC, HRTIMER_MODE_REL);
			INIT_WORK(&gdev->lines[i].apply_work,
				  virtrtlab_gpio_apply_work_fn);
		}

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
			put_device(&gdev->dev);
			goto err_unwind;
		}

		gdev->gc.label          = dev_name(&gdev->dev);
		gdev->gc.parent         = &gdev->dev;
		gdev->gc.owner          = THIS_MODULE;
		gdev->gc.ngpio          = VIRTRTLAB_GPIO_LINES;
		gdev->gc.base           = -1;	/* dynamic assignment */
		gdev->gc.can_sleep      = true;
		gdev->gc.direction_input  = virtrtlab_gpio_direction_input;
		gdev->gc.direction_output = virtrtlab_gpio_direction_output;
		gdev->gc.get            = virtrtlab_gpio_get;
		gdev->gc.set            = virtrtlab_gpio_set;
		gdev->gc.set_multiple   = virtrtlab_gpio_set_multiple;
		gdev->gc.get_multiple   = virtrtlab_gpio_get_multiple;
		gdev->gc.get_direction  = virtrtlab_gpio_get_direction;

		ret = gpiochip_add_data(&gdev->gc, gdev);
		if (ret) {
			pr_err("failed to register gpio_chip for gpio%d: %d\n", n, ret);
			for (i = 0; i < VIRTRTLAB_GPIO_LINES; i++) {
				hrtimer_cancel(&gdev->lines[i].delay_timer);
				cancel_work_sync(&gdev->lines[i].apply_work);
			}
			device_unregister(&gdev->dev);
			goto err_unwind;
		}

		/*
		 * gc.base is the first GPIO number in the system GPIO numberspace,
		 * not the gpiochip character device index.
		 * gpio_device_to_device(gc.gpiodev) returns the gpio_device's
		 * struct device whose kobject name is "gpiochipN" — the same N
		 * that appears in /dev/gpiochipN.  gc.gpiodev is a public field
		 * of struct gpio_chip (driver.h); gpio_device_to_device() is the
		 * public API to dereference it without touching the opaque internals
		 * of struct gpio_device.
		 */
		snprintf(gdev->chip_path, sizeof(gdev->chip_path), "/dev/%s",
			 dev_name(gpio_device_to_device(gdev->gc.gpiodev)));

		ret = virtrtlab_bus_register_notifier(&gdev->nb);
		if (ret) {
			pr_err("failed to register notifier for gpio%d: %d\n", n, ret);
			for (i = 0; i < VIRTRTLAB_GPIO_LINES; i++) {
				hrtimer_cancel(&gdev->lines[i].delay_timer);
				cancel_work_sync(&gdev->lines[i].apply_work);
			}
			gpiochip_remove(&gdev->gc);
			device_unregister(&gdev->dev);
			goto err_unwind;
		}

		gpio_devs[n] = gdev;
		pr_info("gpio%d registered on virtrtlab bus (chip: %s)\n",
			n, gdev->chip_path);
	}

	return 0;

err_unwind:
	while (--n >= 0) {
		for (i = 0; i < VIRTRTLAB_GPIO_LINES; i++) {
			hrtimer_cancel(&gpio_devs[n]->lines[i].delay_timer);
			cancel_work_sync(&gpio_devs[n]->lines[i].apply_work);
		}
		virtrtlab_bus_unregister_notifier(&gpio_devs[n]->nb);
		gpiochip_remove(&gpio_devs[n]->gc);
		device_unregister(&gpio_devs[n]->dev);
	}
	kfree(gpio_devs);
	return ret;
}

static void __exit virtrtlab_gpio_exit(void)
{
	int n, i;

	for (n = (int)num_gpio_devs - 1; n >= 0; n--) {
		for (i = 0; i < VIRTRTLAB_GPIO_LINES; i++) {
			hrtimer_cancel(&gpio_devs[n]->lines[i].delay_timer);
			cancel_work_sync(&gpio_devs[n]->lines[i].apply_work);
		}
		virtrtlab_bus_unregister_notifier(&gpio_devs[n]->nb);
		gpiochip_remove(&gpio_devs[n]->gc);
		/*
		 * device_unregister() removes the device from bus and sysfs,
		 * then calls put_device() which triggers virtrtlab_gpio_dev_release()
		 * → kfree().
		 */
		device_unregister(&gpio_devs[n]->dev);
		pr_info("gpio%d unregistered\n", n);
	}

	kfree(gpio_devs);
}

module_init(virtrtlab_gpio_init);
module_exit(virtrtlab_gpio_exit);

MODULE_SOFTDEP("pre: virtrtlab_core");
MODULE_DESCRIPTION("VirtRTLab GPIO peripheral — native gpio_chip, per-line inject");
MODULE_AUTHOR("VirtRTLab");
MODULE_LICENSE("GPL");
