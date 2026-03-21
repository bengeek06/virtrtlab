// SPDX-License-Identifier: GPL-2.0-only
/*
 * virtrtlab_uart.c — VirtRTLab virtual UART peripheral
 *
 * Implements the sysfs device model and per-device stats counters (issue #3).
 *
 * Issue #2 (TTY driver (/dev/ttyVIRTLABx) + wire misc device
 * (/dev/virtrtlab-wireN) + kfifos + set_termios mirrors) is COMPLETE.
 * Issue #3 (per-device stats counters) is COMPLETE.
 * Issue #4 (inline fault injection engine: drop, bitflip, latency, jitter) is COMPLETE.
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/atomic.h>
#include <linux/device.h>
#include <linux/hrtimer.h>
#include <linux/init.h>
#include <linux/kfifo.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/notifier.h>
#include <linux/poll.h>
#include <linux/printk.h>
#include <linux/serial.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/tty.h>
#include <linux/tty_driver.h>
#include <linux/tty_flip.h>
#include <linux/version.h>
#include <linux/wait.h>
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
 * Burst size for one TX work invocation.  Kept small to bound latency
 * and per-call stack usage while still amortising spinlock overhead.
 */
#define VIRTRTLAB_UART_BURST_SZ		64U

/*
 * tty_std_termios defaults reflected at device creation (module load).
 * baud=38400 matches tty_std_termios speed B38400.
 */
#define VIRTRTLAB_UART_DEFAULT_BAUD	38400U

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
#define virtrtlab_hrtimer_init(_timer, _fn) \
	hrtimer_setup((_timer), (_fn), CLOCK_MONOTONIC, HRTIMER_MODE_REL)
#else
#define virtrtlab_hrtimer_init(_timer, _fn) \
	do { \
		hrtimer_init((_timer), CLOCK_MONOTONIC, HRTIMER_MODE_REL); \
		(_timer)->function = (_fn); \
	} while (0)
#endif

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
	 * module load / device creation; subsequent updates reflect AUT
	 * tcsetattr() calls and are not implicitly reset by bus RESET.
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

	/* issue #2-A1: TTY port — one per UART instance */
	struct tty_port		port;

	/* issue #2-A2: wire device exclusive-open accounting */
	atomic_t		wire_open_count;

	/*
	 * Set to 1 in port_activate(), cleared (under rx_lock) in
	 * port_shutdown().  Guards wire_read() against accessing a freed
	 * rx_fifo (B1 UAF fix) and gates buf_sz resize.
	 */
	atomic_t		port_active;

	/*
	 * issue #2-A3: TX kfifo — AUT → wire (power-of-two, allocated in
	 *   tty_port.activate() so the current tx_buf_sz takes effect on
	 *   the next open of /dev/ttyVIRTLABx).
	 */
	DECLARE_KFIFO_PTR(tx_fifo, u8);
	spinlock_t		tx_lock;	/* protects tx_fifo */

	/*
	 * issue #2-A4: wire-side kfifo — AUT → wire (power-of-two).
	 *   Filled by virtrtlab_uart_tx_work_fn(), drained by wire_read().
	 *   Overflow evicts the new bytes and increments stat_overruns.
	 */
	DECLARE_KFIFO_PTR(rx_fifo, u8);
	spinlock_t		rx_lock;	/* protects rx_fifo */

	/* wake wire_read() and wire_poll() when tx_fifo gets data */
	wait_queue_head_t	wire_read_wq;

	/* set to 1 on bus RESET to make wire_read() return EOF (0) */
	atomic_t		wire_reset_pending;

	/* issue #2-A5: /dev/virtrtlab-wireN misc char device */
	struct miscdevice	wire_dev;
	char			wire_name[24];

	/*
	 * Per-device stats counters (issue #3).
	 * atomic64_t ensures lockless, race-free updates from any context.
	 * Counters wrap silently at UINT64_MAX (modular arithmetic, no
	 * saturation), as required by the spec.
	 */
	atomic64_t		stat_tx_bytes;	/* counted before fault injection */
	atomic64_t		stat_rx_bytes;
	atomic64_t		stat_overruns;	/* wire-side fifo overruns (rx_fifo evictions) */
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
	/* Reject resize while kfifos are live. */
	if (atomic_read(&udev->wire_open_count) ||
	    atomic_read(&udev->port_active))
		return -EBUSY;
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
	/* Reject resize while kfifos are live. */
	if (atomic_read(&udev->wire_open_count) ||
	    atomic_read(&udev->port_active))
		return -EBUSY;
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
 * TTY driver (issue #2) — /dev/ttyVIRTLABx
 * -----------------------------------------------------------------------
 */

static struct tty_driver *virtrtlab_tty_driver;

/*
 * tty_port_operations:
 *   activate() — called on first open; allocates TX/RX kfifos at the sizes
 *                currently configured via tx_buf_sz / rx_buf_sz sysfs attrs.
 *   shutdown()  — called on last close; frees kfifos and stops the timer.
 */
static int virtrtlab_port_activate(struct tty_port *port, struct tty_struct *tty)
{
	struct virtrtlab_uart_dev *udev =
		container_of(port, struct virtrtlab_uart_dev, port);
	u32 tx_sz, rx_sz;
	int ret;

	mutex_lock(&udev->lock);
	tx_sz = udev->tx_buf_sz;
	rx_sz = udev->rx_buf_sz;
	mutex_unlock(&udev->lock);

	ret = kfifo_alloc(&udev->tx_fifo, tx_sz, GFP_KERNEL);
	if (ret)
		return ret;
	ret = kfifo_alloc(&udev->rx_fifo, rx_sz, GFP_KERNEL);
	if (ret) {
		kfifo_free(&udev->tx_fifo);
		return ret;
	}
	atomic_set(&udev->port_active, 1);
	return 0;
}

static void virtrtlab_port_shutdown(struct tty_port *port)
{
	struct virtrtlab_uart_dev *udev =
		container_of(port, struct virtrtlab_uart_dev, port);
	unsigned long flags;
	unsigned int pending;

	hrtimer_cancel(&udev->tx_timer);
	cancel_work_sync(&udev->tx_work);

	/* Account for bytes that will never reach the wire. */
	pending = kfifo_len(&udev->tx_fifo);
	if (pending)
		atomic64_add(pending, &udev->stat_drops);
	kfifo_free(&udev->tx_fifo);

	/*
	 * Clear port_active and free rx_fifo under rx_lock so that a
	 * concurrent wire_read() cannot race with kfifo_free() on SMP.
	 * wire_read() checks port_active under the same lock before any
	 * kfifo access, eliminating the use-after-free (B1 fix).
	 */
	spin_lock_irqsave(&udev->rx_lock, flags);
	atomic_set(&udev->port_active, 0);
	kfifo_free(&udev->rx_fifo);
	spin_unlock_irqrestore(&udev->rx_lock, flags);
	wake_up_interruptible(&udev->wire_read_wq);
}

static const struct tty_port_operations virtrtlab_port_ops = {
	.activate = virtrtlab_port_activate,
	.shutdown = virtrtlab_port_shutdown,
};

/*
 * tty_operations — install wires tty->driver_data; open/close/hangup are
 * handled by tty_port helpers for free.  write, write_room, chars_in_buffer,
 * and set_termios are filled in at steps 3 and 4.
 */
static int virtrtlab_tty_install(struct tty_driver *driver,
				 struct tty_struct *tty)
{
	struct virtrtlab_uart_dev *udev = uart_devs[tty->index];

	tty->driver_data = udev;
	return tty_port_install(&udev->port, driver, tty);
}

/*
 * open/close/hangup — wrappers required because kernel ≥ 6.12 changed
 * tty_operations.open/close/hangup signatures to omit the tty_port * arg,
 * while tty_port_open/close/hangup still take it.
 */
static int virtrtlab_tty_open(struct tty_struct *tty, struct file *filp)
{
	struct virtrtlab_uart_dev *udev = tty->driver_data;

	return tty_port_open(&udev->port, tty, filp);
}

static void virtrtlab_tty_close(struct tty_struct *tty, struct file *filp)
{
	struct virtrtlab_uart_dev *udev = tty->driver_data;

	tty_port_close(&udev->port, tty, filp);
}

static void virtrtlab_tty_hangup(struct tty_struct *tty)
{
	struct virtrtlab_uart_dev *udev = tty->driver_data;

	tty_port_hangup(&udev->port);
}

/*
 * virtrtlab_uart_byte_ns - time in nanoseconds to transmit one UART byte
 *
 * Uses 10 bits per byte (1 start + 8 data + 1 stop).  Falls back to 9600 baud
 * when the configured baud rate is zero.
 */
static u64 virtrtlab_uart_byte_ns(u32 baud)
{
	if (!baud)
		baud = 9600;
	return div64_u64(10ULL * NSEC_PER_SEC, (u64)baud);
}

static ssize_t virtrtlab_tty_write_common(struct tty_struct *tty,
					  const u8 *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = tty->driver_data;
	unsigned int copied;
	u32 baud;

	/* B2 fix: reject writes while the bus is not operational. */
	if (!virtrtlab_bus_is_up())
		return -EIO;

	/*
	 * Per-device gate: reject when disabled.  Read enabled and baud
	 * together under lock to avoid a second lock acquisition below.
	 */
	mutex_lock(&udev->lock);
	if (!udev->enabled) {
		mutex_unlock(&udev->lock);
		return -EIO;
	}
	baud = udev->baud;
	mutex_unlock(&udev->lock);

	copied = kfifo_in_spinlocked(&udev->tx_fifo, buf, count, &udev->tx_lock);
	if (copied) {
		/*
		 * hrtimer_start() on an already-active timer is safe: the
		 * timer is rearmed with the new expiry.  Skipping the
		 * hrtimer_active() pre-check removes the TOCTOU window
		 * between the check and the arm (m4 fix).
		 */
		hrtimer_start(&udev->tx_timer,
			      ns_to_ktime(virtrtlab_uart_byte_ns(baud)),
			      HRTIMER_MODE_REL);
	}
	return copied;
}

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)
static ssize_t virtrtlab_tty_write(struct tty_struct *tty, const u8 *buf,
			   size_t count)
{
	return virtrtlab_tty_write_common(tty, buf, count);
}
#else
static int virtrtlab_tty_write(struct tty_struct *tty,
			      const unsigned char *buf, int count)
{
	ssize_t ret;

	ret = virtrtlab_tty_write_common(tty, buf, count);
	if (ret < 0)
		return (int)ret;
	return (int)ret;
}
#endif

static unsigned int virtrtlab_tty_write_room(struct tty_struct *tty)
{
	struct virtrtlab_uart_dev *udev = tty->driver_data;

	return kfifo_avail(&udev->tx_fifo);
}

static unsigned int virtrtlab_tty_chars_in_buffer(struct tty_struct *tty)
{
	struct virtrtlab_uart_dev *udev = tty->driver_data;

	return kfifo_len(&udev->tx_fifo);
}

static void virtrtlab_tty_set_termios(struct tty_struct *tty,
				      const struct ktermios *old)
{
	struct virtrtlab_uart_dev *udev = tty->driver_data;
	u32 baud;
	const char *parity;
	u8 databits, stopbits;

	baud = tty_termios_baud_rate(&tty->termios);

	if (tty->termios.c_cflag & PARENB)
		parity = (tty->termios.c_cflag & PARODD) ? "odd" : "even";
	else
		parity = "none";

	switch (tty->termios.c_cflag & CSIZE) {
	case CS5:
		databits = 5;
		break;
	case CS6:
		databits = 6;
		break;
	case CS7:
		databits = 7;
		break;
	default:
		databits = 8;
		break;
	}

	stopbits = (tty->termios.c_cflag & CSTOPB) ? 2 : 1;

	mutex_lock(&udev->lock);
	udev->baud     = baud;
	strscpy(udev->parity, parity, sizeof(udev->parity));
	udev->databits = databits;
	udev->stopbits = stopbits;
	mutex_unlock(&udev->lock);
}

static const struct tty_operations virtrtlab_tty_ops = {
	.install	 = virtrtlab_tty_install,
	.open		 = virtrtlab_tty_open,
	.close		 = virtrtlab_tty_close,
	.hangup		 = virtrtlab_tty_hangup,
	.write		 = virtrtlab_tty_write,
	.write_room	 = virtrtlab_tty_write_room,
	.chars_in_buffer = virtrtlab_tty_chars_in_buffer,
	.set_termios	 = virtrtlab_tty_set_termios,
};

/* -----------------------------------------------------------------------
 * Fault injection TX engine (issue #4)
 *
 * Data path per burst:
 *   B1. kfifo_out() — dequeue up to BURST_SZ bytes from TX fifo.
 *   B2. stat_tx_bytes += burst_len (before any fault decision).
 *   Enabled gate: if !enabled, stat_drops += burst_len, goto rearm.
 *   B3. Drop: one PRNG draw; if gate fires, stat_drops += burst_len, goto rearm.
 *   B4. Bitflip: draw1 for gate + byte index; draw2 (low 3 bits) for bit pos.
 *   B5. kfifo_in() to wire-side rx_fifo; overflow increments stat_overruns.
 *   B6. Rearm hrtimer with baud pacing + latency_ns + jitter sample.
 * -----------------------------------------------------------------------
 */

static void virtrtlab_uart_tx_work_fn(struct work_struct *work)
{
	struct virtrtlab_uart_dev *udev =
		container_of(work, struct virtrtlab_uart_dev, tx_work);
	u8 burst[VIRTRTLAB_UART_BURST_SZ];
	unsigned int burst_len, written;
	bool enabled;
	u32 baud, drop_ppm, flip_ppm;
	u64 latency_ns, jitter_ns, delay_ns;

	/* B1: dequeue one burst from the TX fifo. */
	burst_len = kfifo_out_spinlocked(&udev->tx_fifo, burst,
					 sizeof(burst), &udev->tx_lock);
	if (!burst_len)
		return;

	/* B2: count bytes before any fault decision. */
	atomic64_add(burst_len, &udev->stat_tx_bytes);

	/*
	 * Snapshot all fault attrs in one critical section so a concurrent
	 * sysfs write cannot partially update them mid-burst.
	 */
	mutex_lock(&udev->lock);
	enabled    = udev->enabled;
	baud       = udev->baud;
	drop_ppm   = udev->drop_rate_ppm;
	flip_ppm   = udev->bitflip_rate_ppm;
	latency_ns = udev->latency_ns;
	jitter_ns  = udev->jitter_ns;
	mutex_unlock(&udev->lock);

	/*
	 * Enabled gate: bytes already queued when enabled transitions to false
	 * are drained as drops so the TX fifo does not accumulate stale data.
	 * The next tty_write() will rearm the timer on re-enable.
	 */
	if (!enabled) {
		atomic64_add(burst_len, &udev->stat_drops);
		goto rearm;
	}

	/*
	 * B3: drop decision — one PRNG draw per burst.
	 * virtrtlab_bus_next_prng_u32() acquires a spinlock internally;
	 * calling it from process context (workqueue) is safe.
	 */
	if (drop_ppm && (virtrtlab_bus_next_prng_u32() % 1000000U) < drop_ppm) {
		atomic64_add(burst_len, &udev->stat_drops);
		goto rearm;
	}

	/*
	 * B4: bitflip decision — first draw for gate + byte index:
	 *   gate:     rnd % 1000000U < flip_ppm
	 *   byte idx: rnd / 1000000U % burst_len  (quotient in [0, 4294])
	 * A second draw supplies the bit position (low 3 bits) so the gate,
	 * index, and bit fields are statistically independent.
	 */
	if (flip_ppm) {
		u32 rnd = virtrtlab_bus_next_prng_u32();

		if ((rnd % 1000000U) < flip_ppm) {
			u32 rnd2 = virtrtlab_bus_next_prng_u32();

			burst[(rnd / 1000000U) % burst_len] ^=
				(u8)(1U << (rnd2 & 7U));
		}
	}

	/* B5: deliver (possibly corrupted) burst to the wire-side kfifo. */
	written = kfifo_in_spinlocked(&udev->rx_fifo, burst,
				      burst_len, &udev->rx_lock);
	if (written < burst_len)
		atomic64_add(burst_len - written, &udev->stat_overruns);

	if (written)
		wake_up_interruptible(&udev->wire_read_wq);

rearm:
	/*
	 * B6: rearm hrtimer if more bytes remain in the TX fifo.
	 * kfifo_is_empty() is checked without tx_lock — intentional lockless
	 * hint; the worst case is a missed rearm, recovered on the next
	 * tty_write() hrtimer_start() call.
	 * Delay = baud pacing for burst_len bytes + latency_ns + uniform jitter
	 * in [0, jitter_ns].  Scaling by burst_len keeps the average delivered
	 * rate aligned with the configured baud rate regardless of burst size.
	 * Two u32 PRNG draws combined into a u64 so the full range is reachable
	 * when jitter_ns > UINT32_MAX (spec ceiling: 10 s = 10^10 ns).
	 */
	if (!kfifo_is_empty(&udev->tx_fifo)) {
		delay_ns = virtrtlab_uart_byte_ns(baud) * burst_len + latency_ns;
		if (jitter_ns) {
			u64 rnd64 = (u64)virtrtlab_bus_next_prng_u32() << 32 |
				    (u64)virtrtlab_bus_next_prng_u32();

			delay_ns += rnd64 % (jitter_ns + 1);
		}
		hrtimer_start(&udev->tx_timer, ns_to_ktime(delay_ns),
			      HRTIMER_MODE_REL);
	}
}

static enum hrtimer_restart virtrtlab_uart_tx_timer_cb(struct hrtimer *timer)
{
	struct virtrtlab_uart_dev *udev =
		container_of(timer, struct virtrtlab_uart_dev, tx_timer);

	schedule_work(&udev->tx_work);
	return HRTIMER_NORESTART;
}

/* -----------------------------------------------------------------------
 * Wire character device — /dev/virtrtlab-wireN
 *
 * Provides the simulator side of the virtual UART pair.  Only one fd may
 * be open at a time (enforced with wire_open_count).
 *
 * Data flow:
 *   AUT: write(/dev/ttyVIRTLABx) → tx_fifo → rx_fifo → wire_read()
 *   Simulator: wire_write() → tty_insert_flip_string → AUT read(/dev/ttyVIRTLABx)
 * -----------------------------------------------------------------------
 */

static int virtrtlab_wire_open(struct inode *inode, struct file *filp)
{
	struct miscdevice *misc = filp->private_data;
	struct virtrtlab_uart_dev *udev =
		container_of(misc, struct virtrtlab_uart_dev, wire_dev);

	if (atomic_cmpxchg(&udev->wire_open_count, 0, 1) != 0)
		return -EBUSY;

	/* Clear any stale reset flag from a previous session. */
	atomic_set(&udev->wire_reset_pending, 0);
	filp->private_data = udev;
	return stream_open(inode, filp);
}

static int virtrtlab_wire_release(struct inode *inode, struct file *filp)
{
	struct virtrtlab_uart_dev *udev = filp->private_data;
	unsigned long flags;

	/* Drop unread bytes so the next open starts clean. */
	spin_lock_irqsave(&udev->rx_lock, flags);
	kfifo_reset(&udev->rx_fifo);
	spin_unlock_irqrestore(&udev->rx_lock, flags);

	atomic_set(&udev->wire_open_count, 0);
	return 0;
}

static ssize_t virtrtlab_wire_read(struct file *filp, char __user *buf,
				   size_t count, loff_t *ppos)
{
	struct virtrtlab_uart_dev *udev = filp->private_data;
	size_t rd_sz;
	u8 *kbuf;
	unsigned int n;
	unsigned long flags;
	int ret;

	/* POSIX: zero-length read must return 0 immediately without blocking. */
	if (!count)
		return 0;

	/* Return EOF when a bus reset is pending; simulator must re-open. */
	if (atomic_read(&udev->wire_reset_pending))
		return 0;

	/*
	 * Cap to PAGE_SIZE so the allocation stays in the small-object
	 * slab and the call cannot block arbitrarily long under rx_lock
	 * (m1 fix: removes the old hard 256-byte ceiling).
	 * Allocate once here so that the retry loop below does not
	 * reallocate on spurious wakeups.
	 */
	rd_sz = min_t(size_t, count, PAGE_SIZE);
	kbuf = kmalloc(rd_sz, GFP_KERNEL);
	if (!kbuf)
		return -ENOMEM;

retry:
	if (!(filp->f_flags & O_NONBLOCK)) {
		/*
		 * !kfifo_is_empty() evaluated without lock is a hint only;
		 * the authoritative check happens under rx_lock below.
		 */
		ret = wait_event_interruptible(udev->wire_read_wq,
					       !kfifo_is_empty(&udev->rx_fifo) ||
					       !atomic_read(&udev->port_active) ||
					       atomic_read(&udev->wire_reset_pending));
		if (ret) {
			kfree(kbuf);
			return ret;
		}
		if (atomic_read(&udev->wire_reset_pending)) {
			kfree(kbuf);
			return 0;
		}
	}

	/*
	 * Acquire rx_lock before touching rx_fifo.  port_shutdown() clears
	 * port_active and calls kfifo_free() under the same lock, so this
	 * check is race-free: if port_active is 0 here, rx_fifo is already
	 * freed and must not be dereferenced (B1 fix).
	 */
	spin_lock_irqsave(&udev->rx_lock, flags);
	if (!atomic_read(&udev->port_active)) {
		spin_unlock_irqrestore(&udev->rx_lock, flags);
		kfree(kbuf);
		return 0;	/* port closed — EOF */
	}
	n = kfifo_out(&udev->rx_fifo, kbuf, rd_sz);
	spin_unlock_irqrestore(&udev->rx_lock, flags);

	if (!n) {
		/* Non-blocking fd: surface EAGAIN when fifo is empty. */
		if (filp->f_flags & O_NONBLOCK) {
			kfree(kbuf);
			return -EAGAIN;
		}
		/* Blocking fd: spurious wakeup or competing reader — re-wait. */
		goto retry;
	}

	ret = copy_to_user(buf, kbuf, n) ? -EFAULT : (ssize_t)n;
	kfree(kbuf);
	return ret;
}

static ssize_t virtrtlab_wire_write(struct file *filp, const char __user *buf,
				    size_t count, loff_t *ppos)
{
	struct virtrtlab_uart_dev *udev = filp->private_data;
	u8 kbuf[256];
	size_t chunk, done = 0;
	int n;

	/* POSIX: zero-length write must return 0. */
	if (!count)
		return 0;

	while (done < count) {
		chunk = min(count - done, sizeof(kbuf));
		if (copy_from_user(kbuf, buf + done, chunk))
			return done ? (ssize_t)done : -EFAULT;

		n = tty_insert_flip_string(&udev->port, kbuf, chunk);
		if (n > 0) {
			atomic64_add(n, &udev->stat_rx_bytes);
			if ((size_t)n < chunk) {
				atomic64_add(chunk - n, &udev->stat_overruns);
				done += n;
				break; /* flip buffer full */
			}
			done += n;
		} else {
			/* Flip buffer full on first byte. */
			atomic64_add(chunk, &udev->stat_overruns);
			break;
		}
	}

	if (done)
		tty_flip_buffer_push(&udev->port);

	/*
	 * The write path is non-blocking with respect to flip buffer pressure:
	 * tty_insert_flip_string() never sleeps.  When the flip buffer is full
	 * and no bytes were written, return -EAGAIN regardless of O_NONBLOCK so
	 * the simulator can detect back-pressure and retry (or use poll()).
	 */
	return done ? (ssize_t)done : -EAGAIN;
}

static __poll_t virtrtlab_wire_poll(struct file *filp, poll_table *wait)
{
	struct virtrtlab_uart_dev *udev = filp->private_data;
	__poll_t mask = 0;

	poll_wait(filp, &udev->wire_read_wq, wait);

	/*
	 * Lockless hint: kfifo_is_empty() is read without rx_lock.  This is
	 * a best-effort snapshot; poll() re-evaluates after the next wakeup.
	 * The race with kfifo_free() in port_shutdown() reads only the size
	 * fields, not the buffer pointer, so no memory corruption is possible.
	 */
	if (!kfifo_is_empty(&udev->rx_fifo))
		mask |= EPOLLIN | EPOLLRDNORM;
	/*
	 * Only signal writability when the port is active: wire_write() returns
	 * -EAGAIN when port_active is 0, so advertising EPOLLOUT then would
	 * cause poll/epoll callers to busy-loop.
	 */
	if (atomic_read(&udev->port_active))
		mask |= EPOLLOUT | EPOLLWRNORM;
	if (atomic_read(&udev->wire_reset_pending) ||
	    !atomic_read(&udev->port_active))
		mask |= EPOLLHUP;

	return mask;
}

static const struct file_operations virtrtlab_wire_fops = {
	.owner   = THIS_MODULE,
	.open    = virtrtlab_wire_open,
	.release = virtrtlab_wire_release,
	.read    = virtrtlab_wire_read,
	.write   = virtrtlab_wire_write,
	.poll    = virtrtlab_wire_poll,
	.llseek  = noop_llseek,
};

/* -----------------------------------------------------------------------
 * Bus event notifier
 * -----------------------------------------------------------------------
 */

static int virtrtlab_uart_bus_notifier(struct notifier_block *nb,
				       unsigned long event, void *data)
{
	struct virtrtlab_uart_dev *udev =
		container_of(nb, struct virtrtlab_uart_dev, nb);
	unsigned long flags;
	unsigned int pending;

	switch (event) {
	case VIRTRTLAB_BUS_EVENT_RESET:
		/*
		 * Spec reset semantics: (1) cancel pending TX, (2) clear all
		 * fault attrs to 0, (3) re-enable device, (4) reset stats.
		 * Termios mirrors and buffer sizes are unaffected by reset.
		 *
		 * Signal the wire side with wire_reset_pending so that
		 * wire_read() returns EOF and the simulator can detect the
		 * event.  Hangup the AUT TTY so the application gets SIGHUP.
		 */
		hrtimer_cancel(&udev->tx_timer);
		cancel_work_sync(&udev->tx_work);
		/* Flush pending TX bytes; stats are cleared below anyway. */
		spin_lock_irqsave(&udev->tx_lock, flags);
		kfifo_reset(&udev->tx_fifo);
		spin_unlock_irqrestore(&udev->tx_lock, flags);
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
		atomic_set(&udev->wire_reset_pending, 1);
		wake_up_interruptible(&udev->wire_read_wq);
		tty_port_tty_hangup(&udev->port, false);
		break;
	case VIRTRTLAB_BUS_EVENT_DOWN:
		/*
		 * Cancel pending TX and discard queued bytes — they can no
		 * longer be delivered.  Account for the loss in stat_drops.
		 * Wake wire_read() so poll() callers see the state change.
		 */
		hrtimer_cancel(&udev->tx_timer);
		cancel_work_sync(&udev->tx_work);
		spin_lock_irqsave(&udev->tx_lock, flags);
		pending = kfifo_len(&udev->tx_fifo);
		kfifo_reset(&udev->tx_fifo);
		spin_unlock_irqrestore(&udev->tx_lock, flags);
		if (pending)
			atomic64_add(pending, &udev->stat_drops);
		wake_up_interruptible(&udev->wire_read_wq);
		break;
	case VIRTRTLAB_BUS_EVENT_UP:
		/*
		 * Bus returned to UP state.  tx_fifo was flushed on DOWN or
		 * RESET; the next tty_write() will rearm the TX timer.
		 */
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

	tty_port_destroy(&udev->port);
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

	/* Allocate and configure the TTY driver — /dev/ttyVIRTLABx (issue #2) */
	virtrtlab_tty_driver = tty_alloc_driver(num_uart_devices,
						TTY_DRIVER_REAL_RAW);
	if (IS_ERR(virtrtlab_tty_driver))
		return PTR_ERR(virtrtlab_tty_driver);

	virtrtlab_tty_driver->driver_name  = "virtrtlab_uart";
	virtrtlab_tty_driver->name         = "ttyVIRTLAB";
	virtrtlab_tty_driver->major        = 0;  /* dynamic major */
	virtrtlab_tty_driver->type         = TTY_DRIVER_TYPE_SERIAL;
	virtrtlab_tty_driver->subtype      = SERIAL_TYPE_NORMAL;
	virtrtlab_tty_driver->init_termios = tty_std_termios;
	tty_set_operations(virtrtlab_tty_driver, &virtrtlab_tty_ops);

	for (i = 0; i < num_uart_devices; i++) {
		struct virtrtlab_uart_dev *udev;

		udev = kzalloc(sizeof(*udev), GFP_KERNEL);
		if (!udev) {
			ret = -ENOMEM;
			goto err_unwind;
		}

		mutex_init(&udev->lock);
		tty_port_init(&udev->port);
		spin_lock_init(&udev->tx_lock);
		spin_lock_init(&udev->rx_lock);
		init_waitqueue_head(&udev->wire_read_wq);
		atomic_set(&udev->wire_open_count, 0);
		atomic_set(&udev->wire_reset_pending, 0);
		atomic_set(&udev->port_active, 0);
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

		/* fault injection engine placeholders */
		virtrtlab_hrtimer_init(&udev->tx_timer,
				      virtrtlab_uart_tx_timer_cb);
		INIT_WORK(&udev->tx_work, virtrtlab_uart_tx_work_fn);

		udev->port.ops = &virtrtlab_port_ops;
		tty_port_link_device(&udev->port, virtrtlab_tty_driver, i);

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

		snprintf(udev->wire_name, sizeof(udev->wire_name),
			 "virtrtlab-wire%u", i);
		udev->wire_dev.minor = MISC_DYNAMIC_MINOR;
		udev->wire_dev.name  = udev->wire_name;
		udev->wire_dev.fops  = &virtrtlab_wire_fops;
		ret = misc_register(&udev->wire_dev);
		if (ret) {
			pr_err("failed to register wire%u: %d\n", i, ret);
			virtrtlab_bus_unregister_notifier(&udev->nb);
			hrtimer_cancel(&udev->tx_timer);
			device_unregister(&udev->dev);
			uart_devs[i] = NULL;
			goto err_unwind;
		}

		pr_info("uart%u registered\n", i);
	}

	ret = tty_register_driver(virtrtlab_tty_driver);
	if (ret) {
		pr_err("failed to register TTY driver: %d\n", ret);
		goto err_unwind;
	}

	return 0;

err_unwind:
	while (i--) {
		misc_deregister(&uart_devs[i]->wire_dev);
		virtrtlab_bus_unregister_notifier(&uart_devs[i]->nb);
		hrtimer_cancel(&uart_devs[i]->tx_timer);
		cancel_work_sync(&uart_devs[i]->tx_work);
		device_unregister(&uart_devs[i]->dev);
		uart_devs[i] = NULL;
	}
	tty_driver_kref_put(virtrtlab_tty_driver);
	return ret;
}

static void __exit virtrtlab_uart_exit(void)
{
	unsigned int i = num_uart_devices;

	tty_unregister_driver(virtrtlab_tty_driver);

	while (i--) {
		misc_deregister(&uart_devs[i]->wire_dev);
		virtrtlab_bus_unregister_notifier(&uart_devs[i]->nb);
		hrtimer_cancel(&uart_devs[i]->tx_timer);
		cancel_work_sync(&uart_devs[i]->tx_work);
		device_unregister(&uart_devs[i]->dev);
		uart_devs[i] = NULL;
		pr_info("uart%u unregistered\n", i);
	}

	tty_driver_kref_put(virtrtlab_tty_driver);
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
