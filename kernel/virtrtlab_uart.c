// SPDX-License-Identifier: GPL-2.0-only
/*
 * VirtRTLab UART — virtual UART peripheral
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/init.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/device.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/string.h>
#include "virtrtlab_core.h"

#define VIRTRTLAB_UART_DEFAULT_BAUD	115200
#define VIRTRTLAB_UART_DEFAULT_DATABITS	8
#define VIRTRTLAB_UART_DEFAULT_STOPBITS	1

/*
 * Per-device state — allocated in init, freed via dev->release().
 * All rw fields must be accessed with ->lock held.
 */
struct virtrtlab_uart_dev {
	struct device	dev;			/* must be first for container_of */
	struct mutex	lock;			/* protects all rw fields below */
	/* common attributes */
	bool		enabled;
	char		mode[16];		/* "normal" | "record" | "replay" */
	u32		latency_ns;
	u32		jitter_ns;
	u32		drop_rate_ppm;
	u32		bitflip_rate_ppm;
	char		fault_policy[32];
	/* UART-specific attributes */
	u32		baud;
	char		parity[8];		/* "none" | "even" | "odd" */
	u8		databits;		/* 5 | 6 | 7 | 8 */
	u8		stopbits;		/* 1 | 2 */
};

#define to_uart_dev(d)	container_of(d, struct virtrtlab_uart_dev, dev)

static struct virtrtlab_uart_dev *uart0;

/*
 * sysfs attributes — read-only
 */

static ssize_t type_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "uart\n");
}
static DEVICE_ATTR_RO(type);

static ssize_t bus_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "vrtlbus0\n");
}
static DEVICE_ATTR_RO(bus);

/*
 * sysfs attributes — common rw
 */

static ssize_t enabled_show(struct device *dev, struct device_attribute *attr, char *buf)
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

static ssize_t mode_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	ssize_t ret;

	mutex_lock(&udev->lock);
	ret = sysfs_emit(buf, "%s\n", udev->mode);
	mutex_unlock(&udev->lock);
	return ret;
}

static ssize_t mode_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	char tmp[16];

	strscpy(tmp, buf, sizeof(tmp));
	strim(tmp);
	if (strcmp(tmp, "normal") && strcmp(tmp, "record") && strcmp(tmp, "replay"))
		return -EINVAL;
	mutex_lock(&udev->lock);
	strscpy(udev->mode, tmp, sizeof(udev->mode));
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(mode);

static ssize_t latency_ns_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->latency_ns;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t latency_ns_store(struct device *dev, struct device_attribute *attr,
				const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	mutex_lock(&udev->lock);
	udev->latency_ns = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(latency_ns);

static ssize_t jitter_ns_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->jitter_ns;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t jitter_ns_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	mutex_lock(&udev->lock);
	udev->jitter_ns = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(jitter_ns);

static ssize_t drop_rate_ppm_show(struct device *dev, struct device_attribute *attr,
				  char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->drop_rate_ppm;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t drop_rate_ppm_store(struct device *dev, struct device_attribute *attr,
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

static ssize_t bitflip_rate_ppm_show(struct device *dev, struct device_attribute *attr,
				     char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->bitflip_rate_ppm;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t bitflip_rate_ppm_store(struct device *dev, struct device_attribute *attr,
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

static ssize_t fault_policy_show(struct device *dev, struct device_attribute *attr,
				 char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	ssize_t ret;

	mutex_lock(&udev->lock);
	ret = sysfs_emit(buf, "%s\n", udev->fault_policy);
	mutex_unlock(&udev->lock);
	return ret;
}

static ssize_t fault_policy_store(struct device *dev, struct device_attribute *attr,
				  const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	char tmp[32];

	strscpy(tmp, buf, sizeof(tmp));
	strim(tmp);
	if (!tmp[0])
		return -EINVAL;
	mutex_lock(&udev->lock);
	strscpy(udev->fault_policy, tmp, sizeof(udev->fault_policy));
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(fault_policy);

/*
 * sysfs attributes — UART-specific rw
 */

static ssize_t baud_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;

	mutex_lock(&udev->lock);
	val = udev->baud;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t baud_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u32 val;
	int ret;

	ret = kstrtou32(buf, 0, &val);
	if (ret)
		return ret;
	if (val == 0)
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->baud = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(baud);

static ssize_t parity_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	ssize_t ret;

	mutex_lock(&udev->lock);
	ret = sysfs_emit(buf, "%s\n", udev->parity);
	mutex_unlock(&udev->lock);
	return ret;
}

static ssize_t parity_store(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	char tmp[8];

	strscpy(tmp, buf, sizeof(tmp));
	strim(tmp);
	if (strcmp(tmp, "none") && strcmp(tmp, "even") && strcmp(tmp, "odd"))
		return -EINVAL;
	mutex_lock(&udev->lock);
	strscpy(udev->parity, tmp, sizeof(udev->parity));
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(parity);

static ssize_t databits_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u8 val;

	mutex_lock(&udev->lock);
	val = udev->databits;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t databits_store(struct device *dev, struct device_attribute *attr,
			      const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u8 val;
	int ret;

	ret = kstrtou8(buf, 0, &val);
	if (ret)
		return ret;
	if (val < 5 || val > 8)
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->databits = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(databits);

static ssize_t stopbits_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u8 val;

	mutex_lock(&udev->lock);
	val = udev->stopbits;
	mutex_unlock(&udev->lock);
	return sysfs_emit(buf, "%u\n", val);
}

static ssize_t stopbits_store(struct device *dev, struct device_attribute *attr,
			      const char *buf, size_t count)
{
	struct virtrtlab_uart_dev *udev = to_uart_dev(dev);
	u8 val;
	int ret;

	ret = kstrtou8(buf, 0, &val);
	if (ret)
		return ret;
	if (val != 1 && val != 2)
		return -EINVAL;
	mutex_lock(&udev->lock);
	udev->stopbits = val;
	mutex_unlock(&udev->lock);
	return count;
}
static DEVICE_ATTR_RW(stopbits);

/*
 * Attribute group — all device attributes in a single flat group.
 */

static struct attribute *uart0_attrs[] = {
	&dev_attr_type.attr,
	&dev_attr_bus.attr,
	&dev_attr_enabled.attr,
	&dev_attr_mode.attr,
	&dev_attr_latency_ns.attr,
	&dev_attr_jitter_ns.attr,
	&dev_attr_drop_rate_ppm.attr,
	&dev_attr_bitflip_rate_ppm.attr,
	&dev_attr_fault_policy.attr,
	&dev_attr_baud.attr,
	&dev_attr_parity.attr,
	&dev_attr_databits.attr,
	&dev_attr_stopbits.attr,
	NULL,
};
ATTRIBUTE_GROUPS(uart0);

/*
 * Device release — called when the last reference is dropped.
 * Frees the virtrtlab_uart_dev allocation.
 */

static void virtrtlab_uart_dev_release(struct device *dev)
{
	kfree(to_uart_dev(dev));
}

/*
 * Module init / exit
 */

static int __init virtrtlab_uart_init(void)
{
	struct virtrtlab_uart_dev *udev;
	int ret;

	udev = kzalloc(sizeof(*udev), GFP_KERNEL);
	if (!udev)
		return -ENOMEM;

	mutex_init(&udev->lock);

	/* set non-zero defaults */
	strscpy(udev->mode, "normal", sizeof(udev->mode));
	strscpy(udev->fault_policy, "none", sizeof(udev->fault_policy));
	strscpy(udev->parity, "none", sizeof(udev->parity));
	udev->baud     = VIRTRTLAB_UART_DEFAULT_BAUD;
	udev->databits = VIRTRTLAB_UART_DEFAULT_DATABITS;
	udev->stopbits = VIRTRTLAB_UART_DEFAULT_STOPBITS;

	/*
	 * Anchor uart0 under /sys/kernel/virtrtlab/devices/ by setting
	 * kobj.parent before device_add(). get_device_parent() returns NULL
	 * for our bus (no dev_root, no class), so the parent is not overridden
	 * by device_add() and our kobject placement is preserved.
	 */
	device_initialize(&udev->dev);
	udev->dev.bus          = &virtrtlab_bus_type;
	udev->dev.release      = virtrtlab_uart_dev_release;
	udev->dev.groups       = uart0_groups;
	udev->dev.kobj.parent  = virtrtlab_devices_kobj;
	dev_set_name(&udev->dev, "uart0");

	ret = device_add(&udev->dev);
	if (ret) {
		pr_err("failed to register uart0: %d\n", ret);
		/* device_initialize() took a kref; put_device triggers release */
		put_device(&udev->dev);
		return ret;
	}

	uart0 = udev;
	pr_info("uart0 registered on virtrtlab bus\n");
	return 0;
}

static void __exit virtrtlab_uart_exit(void)
{
	/*
	 * device_unregister() removes uart0 from bus and sysfs, then calls
	 * put_device() which triggers virtrtlab_uart_dev_release() -> kfree().
	 */
	device_unregister(&uart0->dev);
	pr_info("uart0 unregistered\n");
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
