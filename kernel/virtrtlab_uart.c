// SPDX-License-Identifier: GPL-2.0-only
/*
 * virtrtlab_uart.c — VirtRTLab virtual UART peripheral
 *
 * Implements the sysfs device model, per-device stats counters (issue #3),
 * and the inline fault injection engine (issue #4).
 * The TTY/wire-device transport path is tracked in issue #2; tx_timer and
 * tx_work are structural placeholders for that integration.
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/atomic.h>
#include <linux/device.h>
#include <linux/hrtimer.h>
#include <linux/init.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/notifier.h>
#include <linux/printk.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/workqueue.h>
#include "virtrtlab_core.h"

/* latency_ns / jitter_ns ceiling: 10 s as per spec */
#define VIRTRTLAB_UART_MAX_FAULT_NS	10000000000ULL

/* TX/RX circular buffer bounds; must be power-of-two */
#define VIRTRTLAB_UART_BUF_SZ_MIN	64U
#define VIRTRTLAB_UART_BUF_SZ_MAX	65536U
#define VIRTRTLAB_UART_DEFAULT_BUF_SZ	4096U

/* Maximum number of UART instances this module can create */
#define VIRTRTLAB_UART_MAX_DEVICES	4U

/*
 * tty_std_termios defaults reflected before any AUT open (or after bus reset).
 * baud=38400 matches tty_std_termios speed B38400.
 */
#define VIRTRTLAB_UART_DEFAULT_BAUD	38400U

/*
 * Per-device state — allocated in init, freed via dev->release().
 * All rw fields must be accessed with ->lock held, except atomic64_t stats
 * which are updated locklessly.
 */
struct virtrtlab_uart_dev {
	struct device		dev;		/* must be first for container_of */
	struct mutex		lock;		/* protects all rw fields below */
	unsigned int		index;

	/* common fault attrs */
	bool			enabled;	/* gate; default true */
	u64			latency_ns;	/* 0 .. VIRTRTLAB_UART_MAX_FAULT_NS */
	u64			jitter_ns;	/* 0 .. VIRTRTLAB_UART_MAX_FAULT_NS */
	u32			drop_rate_ppm;	/* 0 .. 1 000 000 */
	u32			bitflip_rate_ppm; /* 0 .. 1 000 000 */

	/*
	 * UART termios mirrors — read-only via sysfs; written by the TTY
	 * driver (issue #2).  Initialised to tty_std_termios defaults on
	 * module load and after a bus reset.
	 */
	u32			baud;		/* 0 if AUT sets unsupported B0/Bx */
	char			parity[8];	/* "none" | "even" | "odd" */
	u8			databits;	/* 5 | 6 | 7 | 8 */
	u8			stopbits;	/* 1 | 2 */

	/*
	 * TX/RX circular buffer sizes (power-of-two, 64..65536).
	 * Changes take effect on the next open of /dev/ttyVIRTLABx.
	 */
	u32			tx_buf_sz;
	u32			rx_buf_sz;

	/*
	 * Per-device stats counters (issue #3).
	 * atomic64_t ensures lockless, race-free updates from any context.
	 * Counters wrap silently at UINT64_MAX (modular arithmetic, no
	 * saturation), as required by the spec.
	 */
	atomic64_t		stat_tx_bytes;	/* counted before fault injection */
	atomic64_t		stat_rx_bytes;
	atomic64_t		stat_overruns;	/* RX buffer evictions */
	atomic64_t		stat_drops;	/* fault-injected or state=down drops */

	/*
	 * Fault injection TX engine (issue #4).
	 * tx_timer applies latency_ns + jitter_ns (sampled from the shared PRNG)
	 * to each TX burst.  tx_work performs the drop/bitflip decisions and
	 * the actual write to the wire misc device.
	 */
	struct hrtimer		tx_timer;
	struct work_struct	tx_work;

	/* bus event notifier */
	struct notifier_block	nb;
};

#define to_uart_dev(d)	container_of(d, struct virtrtlab_uart_dev, dev)

static struct virtrtlab_uart_dev *uart_devs[VIRTRTLAB_UART_MAX_DEVICES];

static unsigned int num_uart_devices = 1;
module_param(num_uart_devices, uint, 0444);
MODULE_PARM_DESC(num_uart_devices,
		 "Number of UART instances to create (1..4, default 1)");

/* -----------------------------------------------------------------------
 * sysfs attrs — read-only informational
 * -----------------------------------------------------------------------
 */

static ssize_t type_show(struct device *dev, struct device_attribute *attr,
			 char *buf)
{
	return sysfs_emit(buf, "uart\n");
}
static DEVICE_ATTR_RO(type);

static ssize_t bus_show(struct device *dev, struct device_attribute *attr,
			char *buf)
{
	return sysfs_emit(buf, "vrtlbus0\n");
}
static DEVICE_ATTR_RO(bus);

/* -----------------------------------------------------------------------
 * sysfs attrs — common rw
 * -----------------------------------------------------------------------
 */

static ssize_t enabled_show(struct device *dev, struct device_attribute *attr,
			    char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	bool val;

	mutex_lock(&udev->lock);
	val = udev->enabled;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%d\n", val ? 1 : 0);
}

static ssize_t enabled_store(struct device *dev, struct device_attribute *attr,
			     const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	bool val;
	int ret;

	ret = kstrtobool(buf, &val);
	if (ret)
		return ret;
	mutex_lock(&udev->lock);
	udev->enabled = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(enabled);

static ssize_t latency_ns_show(struct device *dev, struct device_attribute *attr,
			       char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u64 val;

	mutex_lock(&udev->lock);
	val = udev->latency_ns;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}

static ssize_t latency_ns_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u64 val;
	int ret;

	ret = kstrtou64(buf, 0, &val);
	if (ret)
		return ret;
	if (val > VIRTRTLAB_UART_MAX_FAULT_NS)
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->latency_ns = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(latency_ns);

static ssize_t jitter_ns_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u64 val;

	mutex_lock(&udev->lock);
	val = udev->jitter_ns;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%llu\n", val);
}

static ssize_t jitter_ns_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u64 val;
	int ret;

	ret = kstrtou64(buf, 0, &val);
	if (ret)
		return ret;
	if (val > VIRTRTLAB_UART_MAX_FAULT_NS)
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->jitter_ns = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(jitter_ns);

static ssize_t drop_rate_ppm_show(struct device *dev,
				  struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->drop_rate_ppm;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t drop_rate_ppm_store(struct device *dev,
				   struct device_attribute *attr,
				   const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (val > 1000000)
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->drop_rate_ppm = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(drop_rate_ppm);

static ssize_t bitflip_rate_ppm_show(struct device *dev,
				     struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->bitflip_rate_ppm;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t bitflip_rate_ppm_store(struct device *dev,
				      struct device_attribute *attr,
				      const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (val > 1000000)
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->bitflip_rate_ppm = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(bitflip_rate_ppm);

/* -----------------------------------------------------------------------
 * sysfs attrs — UART termios mirrors (read-only)
 *
 * These reflect the AUT's tcsetattr() calls, updated by the TTY driver in
 * issue #2.  Until the AUT opens /dev/ttyVIRTLABx (or after bus reset),
 * they show the tty_std_termios defaults.
 * -----------------------------------------------------------------------
 */

static ssize_t baud_show(struct device *dev, struct device_attribute *attr,
			 char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->baud;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}
static DEVICE_ATTR_RO(baud);

static ssize_t parity_show(struct device *dev, struct device_attribute *attr,
			   char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	ssize_t ret;

	mutex_lock(&udev->lock);
	ret = sysfs_emit(buf, "%s\n", udev->parity);
	mutex_unlock(&udev->lock);
	return ret;
}
static DEVICE_ATTR_RO(parity);

static ssize_t databits_show(struct device *dev, struct device_attribute *attr,
			     char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u8 val;

	mutex_lock(&udev->lock);
	val = udev->databits;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}
static DEVICE_ATTR_RO(databits);

static ssize_t stopbits_show(struct device *dev, struct device_attribute *attr,
			     char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u8 val;

	mutex_lock(&udev->lock);
	val = udev->stopbits;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}
static DEVICE_ATTR_RO(stopbits);

/* -----------------------------------------------------------------------
 * sysfs attrs — buffer configuration (rw)
 *
 * Must be a power-of-two in [64, 65536].  Changes take effect on the next
 * open of /dev/ttyVIRTLABx (live resize deferred to v0.2.0).  Writing while
 * the device is open returns -EBUSY (enforced in issue #2).
 * -----------------------------------------------------------------------
 */

static bool virtrtlab_uart_buf_sz_valid(u32 val)
{
	return val >= VIRTRTLAB_UART_BUF_SZ_MIN &&
	       val <= VIRTRTLAB_UART_BUF_SZ_MAX &&
	       (val & (val - 1)) == 0;
}

static ssize_t tx_buf_sz_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->tx_buf_sz;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t tx_buf_sz_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (!virtrtlab_uart_buf_sz_valid(val))
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->tx_buf_sz = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(tx_buf_sz);

static ssize_t rx_buf_sz_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->rx_buf_sz;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t rx_buf_sz_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (!virtrtlab_uart_buf_sz_valid(val))
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->rx_buf_sz = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(rx_buf_sz);

/* -----------------------------------------------------------------------
 * Main device attribute group (flat, under /sys/kernel/virtrtlab/devices/uartN/)
 * -----------------------------------------------------------------------
 */

static struct attribute *uart_main_attrs[] = {
	&dev_attr_type.attr,
	&dev_attr_bus.attr,
	&dev_attr_enabled.attr,
	&dev_attr_latency_ns.attr,
	&dev_attr_jitter_ns.attr,
	&dev_attr_drop_rate_ppm.attr,
	&dev_attr_bitflip_rate_ppm.attr,
	&dev_attr_baud.attr,
	&dev_attr_parity.attr,
	&dev_attr_databits.attr,
	&dev_attr_stopbits.attr,
	&dev_attr_tx_buf_sz.attr,
	&dev_attr_rx_buf_sz.attr,
	NULL,
};

static const struct attribute_group uart_main_group = {
	.attrs = uart_main_attrs,
};

/* -----------------------------------------------------------------------
 * Stats attribute group (under .../uartN/stats/)
 *
 * All counters use atomic64_t for lockless, race-free updates from any
 * context.  Wrap silently at UINT64_MAX (modular arithmetic, no saturation).
 * -----------------------------------------------------------------------
 */

static ssize_t tx_bytes_show(struct device *dev, struct device_attribute *attr,
			     char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);

	return sysfs_emit(buf, "%llu\n",
			  (unsigned long long)atomic64_read(&udev->stat_tx_bytes));
}
static DEVICE_ATTR_RO(tx_bytes);

static ssize_t rx_bytes_show(struct device *dev, struct device_attribute *attr,
			     char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);

	return sysfs_emit(buf, "%llu\n",
			  (unsigned long long)atomic64_read(&udev->stat_rx_bytes));
}
static DEVICE_ATTR_RO(rx_bytes);

static ssize_t overruns_show(struct device *dev, struct device_attribute *attr,
			     char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);

	return sysfs_emit(buf, "%llu\n",
			  (unsigned long long)atomic64_read(&udev->stat_overruns));
}
static DEVICE_ATTR_RO(overruns);

static ssize_t drops_show(struct device *dev, struct device_attribute *attr,
			  char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);

	return sysfs_emit(buf, "%llu\n",
			  (unsigned long long)atomic64_read(&udev->stat_drops));
}
static DEVICE_ATTR_RO(drops);

/*
 * stats/reset — write-only; accepts only "0".  Any other value returns
 * -EINVAL.  Each counter is reset atomically (individual stores; cross-counter
 * coherence is not guaranteed — matches spec intent of "reset to zero").
 */
static ssize_t reset_store(struct device *dev, struct device_attribute *attr,
			   const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (val != 0)
		return -EINVAL;
	atomic64_set(&udev->stat_tx_bytes, 0);
	atomic64_set(&udev->stat_rx_bytes, 0);
	atomic64_set(&udev->stat_overruns, 0);
	atomic64_set(&udev->stat_drops, 0);
	return count;
}
static DEVICE_ATTR_WO(reset);

static struct attribute *uart_stats_attrs[] = {
	&dev_attr_tx_bytes.attr,
	&dev_attr_rx_bytes.attr,
	&dev_attr_overruns.attr,
	&dev_attr_drops.attr,
	&dev_attr_reset.attr,
	NULL,
};

static const struct attribute_group uart_stats_group = {
	.name  = "stats",
	.attrs = uart_stats_attrs,
};

static const struct attribute_group *uart_groups[] = {
	&uart_main_group,
	&uart_stats_group,
	NULL,
};

/* -----------------------------------------------------------------------
 * Fault injection TX engine (issue #4)
 *
 * TX delivery sequence per hrtimer burst:
 *   1. Draw one u32 from virtrtlab_bus_next_prng_u32().
 *      bits[19:0]: gate against drop_rate_ppm.  Threshold:
 *        (rnd & 0xFFFFF) < drop_ppm * 0xFFFFF / 1000000
 *      If drop → atomic64_add(burst_len, stat_drops); return.
 *   2. Draw another u32.
 *      bits[19:0]: gate against bitflip_rate_ppm (same formula).
 *      bits[31:20]: byte index within burst to flip a random bit.
 *   3. Deliver the (possibly corrupted) burst to the wire misc device.
 *      Increment stat_tx_bytes by burst_len before step 1.
 *
 * The tx_work and tx_timer below are structural placeholders; the actual
 * circular-buffer wiring is done in issue #2.
 * -----------------------------------------------------------------------
 */

static void virtrtlab_uart_tx_work_fn(struct work_struct *work)
{
	/* Placeholder: wired to the TX circular buffer in issue #2. */
}

static enum hrtimer_restart virtrtlab_uart_tx_timer_cb(struct hrtimer *timer)
{
	struct virtrtlab_uart_dev *udev =
		container_of(timer, struct virtrtlab_uart_dev, tx_timer);

	schedule_work(&udev->tx_work);
	return HRTIMER_NORESTART;
}

/* -----------------------------------------------------------------------
 * Bus event notifier
 * -----------------------------------------------------------------------
 */

static int virtrtlab_uart_bus_notifier(struct notifier_block *nb,
				       unsigned long event, void *data)
{
	struct virtrtlab_uart_dev *udev =
		container_of(nb, struct virtrtlab_uart_dev, nb);

	switch (event) {
	case VIRTRTLAB_BUS_EVENT_RESET:
		/*
		 * Spec reset semantics: (1) cancel pending TX, (2) clear all
		 * fault attrs to 0, (3) re-enable device, (4) reset stats.
		 * Termios mirrors and buffer sizes are unaffected by reset.
		 */
		hrtimer_cancel(&udev->tx_timer);
		cancel_work_sync(&udev->tx_work);
		mutex_lock(&udev->lock);
		udev->latency_ns     = 0;
		udev->jitter_ns      = 0;
		udev->drop_rate_ppm  = 0;
		udev->bitflip_rate_ppm = 0;
		udev->enabled        = true;
		mutex_unlock(&udev->lock);
		atomic64_set(&udev->stat_tx_bytes, 0);
		atomic64_set(&udev->stat_rx_bytes, 0);
		atomic64_set(&udev->stat_overruns, 0);
		atomic64_set(&udev->stat_drops, 0);
		break;
	case VIRTRTLAB_BUS_EVENT_DOWN:
		/*
		 * Cancel pending TX.  In-flight bytes are drained if a wire
		 * daemon has the fd open; otherwise dropped (stat_drops
		 * incremented in tx_work — issue #2).
		 */
		hrtimer_cancel(&udev->tx_timer);
		cancel_work_sync(&udev->tx_work);
		break;
	default:
		break;
	}
	return NOTIFY_OK;
}

/* -----------------------------------------------------------------------
 * Device release
 * -----------------------------------------------------------------------
 */

static void virtrtlab_uart_dev_release(struct device *dev)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);

	mutex_destroy(&udev->lock);
	kfree(udev);
}

/* -----------------------------------------------------------------------
 * Module init / exit
 * -----------------------------------------------------------------------
 */

static int __init virtrtlab_uart_init(void)
{
	unsigned int i;
	int ret;

	if (num_uart_devices == 0 ||
	    num_uart_devices > VIRTRTLAB_UART_MAX_DEVICES) {
		pr_err("num_uart_devices must be 1..%u\n",
		       VIRTRTLAB_UART_MAX_DEVICES);
		return -EINVAL;
	}

	for (i = 0; i < num_uart_devices; i++) {
		struct virtrtlab_uart_dev *udev;

		udev = kzalloc(sizeof(*udev), GFP_KERNEL);
		if (!udev) {
			ret = -ENOMEM;
			goto err_unwind;
		}

		mutex_init(&udev->lock);
		udev->index = i;

		/* common defaults */
		udev->enabled   = true;
		udev->tx_buf_sz = VIRTRTLAB_UART_DEFAULT_BUF_SZ;
		udev->rx_buf_sz = VIRTRTLAB_UART_DEFAULT_BUF_SZ;

		/* tty_std_termios defaults (before first AUT open) */
		udev->baud     = VIRTRTLAB_UART_DEFAULT_BAUD;
		strscpy(udev->parity, "none", sizeof(udev->parity));
		udev->databits = 8;
		udev->stopbits = 1;

		/* fault injection engine */
		hrtimer_init(&udev->tx_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
		udev->tx_timer.function = virtrtlab_uart_tx_timer_cb;
		INIT_WORK(&udev->tx_work, virtrtlab_uart_tx_work_fn);

		device_initialize(&udev->dev);
		udev->dev.bus         = &virtrtlab_bus_type;
		udev->dev.release     = virtrtlab_uart_dev_release;
		udev->dev.groups      = uart_groups;
		udev->dev.kobj.parent = virtrtlab_devices_kobj;
		dev_set_name(&udev->dev, "uart%u", i);

		ret = device_add(&udev->dev);
		if (ret) {
			pr_err("failed to register uart%u: %d\n", i, ret);
			hrtimer_cancel(&udev->tx_timer);
			put_device(&udev->dev);
			goto err_unwind;
		}

		udev->nb.notifier_call = virtrtlab_uart_bus_notifier;
		ret = virtrtlab_bus_register_notifier(&udev->nb);
		if (ret) {
			pr_err("failed to register notifier for uart%u: %d\n",
			       i, ret);
			hrtimer_cancel(&udev->tx_timer);
			device_unregister(&udev->dev);
			goto err_unwind;
		}

		uart_devs[i] = udev;
		pr_info("uart%u registered on virtrtlab bus\n", i);
	}

	return 0;

err_unwind:
	while (i--) {
		virtrtlab_bus_unregister_notifier(&uart_devs[i]->nb);
		hrtimer_cancel(&uart_devs[i]->tx_timer);
		cancel_work_sync(&uart_devs[i]->tx_work);
		device_unregister(&uart_devs[i]->dev);
		uart_devs[i] = NULL;
	}
	return ret;
}

static void __exit virtrtlab_uart_exit(void)
{
	unsigned int i = num_uart_devices;

	while (i--) {
		virtrtlab_bus_unregister_notifier(&uart_devs[i]->nb);
		hrtimer_cancel(&uart_devs[i]->tx_timer);
		cancel_work_sync(&uart_devs[i]->tx_work);
		device_unregister(&uart_devs[i]->dev);
		uart_devs[i] = NULL;
		pr_info("uart%u unregistered\n", i);
	}
}

module_init(virtrtlab_uart_init);
module_exit(virtrtlab_uart_exit);

/*
 * Ensure virtrtlab_core is loaded before this module so virtrtlab_bus_type
 * and virtrtlab_devices_kobj are valid when our init runs.
 */
MODULE_SOFTDEP("pre: virtrtlab_core");
MODULE_DESCRIPTION("VirtRTLab UART peripheral");
MODULE_AUTHOR("VirtRTLab");
MODULE_LICENSE("GPL");
