// SPDX-License-Identifier: GPL-2.0-only
/*
 * VirtRTLab core — virtual bus registration and sysfs kobject tree
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/device/bus.h>
#include <linux/init.h>
#include <linux/kobject.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/notifier.h>
#include <linux/printk.h>
#include <linux/spinlock.h>
#include <linux/sysfs.h>
#include <linux/mutex.h>
/* Included to let the compiler cross-check our definition against the
 * extern declaration exposed to peripheral modules.
 */
#include "virtrtlab_core.h"

#define VIRTRTLAB_VERSION "0.1.0"

/*
 * Bus type — visible in /sys/bus/virtrtlab/
 */

const struct bus_type virtrtlab_bus_type = {
	.name = "virtrtlab",
};
EXPORT_SYMBOL_GPL(virtrtlab_bus_type);

/*
 * sysfs kobject tree:
 *
 *   /sys/kernel/virtrtlab/          <- virtrtlab_kobj
 *   /sys/kernel/virtrtlab/version   <- ro attribute
 *   /sys/kernel/virtrtlab/buses/    <- virtrtlab_buses_kobj
 *   /sys/kernel/virtrtlab/devices/  <- virtrtlab_devices_kobj
 */

static struct kobject *virtrtlab_kobj;
static struct kobject *virtrtlab_buses_kobj;
static struct kobject *virtrtlab_bus0_kobj;
struct kobject *virtrtlab_devices_kobj;
EXPORT_SYMBOL_GPL(virtrtlab_devices_kobj);

static DEFINE_SPINLOCK(virtrtlab_bus_lock);
static DEFINE_MUTEX(virtrtlab_bus_state_mutex);
static BLOCKING_NOTIFIER_HEAD(virtrtlab_bus_notifier);
static enum virtrtlab_bus_state virtrtlab_bus_state = VIRTRTLAB_BUS_STATE_UP;
static u32 virtrtlab_bus_prng_state = VIRTRTLAB_DEFAULT_SEED;

static int virtrtlab_bus_notifier_call(unsigned long event)
{
	return blocking_notifier_call_chain(&virtrtlab_bus_notifier, event, NULL);
}

static const char *virtrtlab_bus_state_name(enum virtrtlab_bus_state state)
{
	switch (state) {
	case VIRTRTLAB_BUS_STATE_UP:
		return "up";
	case VIRTRTLAB_BUS_STATE_DOWN:
		return "down";
	default:
		return "unknown";
	}
}

enum virtrtlab_bus_state virtrtlab_bus_get_state(void)
{
	return READ_ONCE(virtrtlab_bus_state);
}
EXPORT_SYMBOL_GPL(virtrtlab_bus_get_state);

bool virtrtlab_bus_is_up(void)
{
	return virtrtlab_bus_get_state() == VIRTRTLAB_BUS_STATE_UP;
}
EXPORT_SYMBOL_GPL(virtrtlab_bus_is_up);

u32 virtrtlab_bus_get_seed(void)
{
	return READ_ONCE(virtrtlab_bus_prng_state);
}
EXPORT_SYMBOL_GPL(virtrtlab_bus_get_seed);

u32 virtrtlab_bus_next_prng_u32(void)
{
	unsigned long flags;
	u32 value;

	spin_lock_irqsave(&virtrtlab_bus_lock, flags);
	value = virtrtlab_bus_prng_state;
	if (!value)
		value = VIRTRTLAB_DEFAULT_SEED;
	value ^= value << 13;
	value ^= value >> 17;
	value ^= value << 5;
	virtrtlab_bus_prng_state = value;
	spin_unlock_irqrestore(&virtrtlab_bus_lock, flags);

	return value;
}
EXPORT_SYMBOL_GPL(virtrtlab_bus_next_prng_u32);

int virtrtlab_bus_register_notifier(struct notifier_block *nb)
{
	return blocking_notifier_chain_register(&virtrtlab_bus_notifier, nb);
}
EXPORT_SYMBOL_GPL(virtrtlab_bus_register_notifier);

int virtrtlab_bus_unregister_notifier(struct notifier_block *nb)
{
	return blocking_notifier_chain_unregister(&virtrtlab_bus_notifier, nb);
}
EXPORT_SYMBOL_GPL(virtrtlab_bus_unregister_notifier);

static ssize_t version_show(struct kobject *kobj, struct kobj_attribute *attr,
			    char *buf)
{
	return sysfs_emit(buf, "%s\n", VIRTRTLAB_VERSION);
}

static struct kobj_attribute virtrtlab_version_attr = __ATTR_RO(version);

static struct attribute *virtrtlab_root_attrs[] = {
	&virtrtlab_version_attr.attr,
	NULL,
};

static const struct attribute_group virtrtlab_root_attr_group = {
	.attrs = virtrtlab_root_attrs,
};

static ssize_t state_show(struct kobject *kobj, struct kobj_attribute *attr,
			  char *buf)
{
	return sysfs_emit(buf, "%s\n",
			  virtrtlab_bus_state_name(virtrtlab_bus_get_state()));
}

static void virtrtlab_bus_set_state(enum virtrtlab_bus_state new_state)
{
	unsigned long flags;
	enum virtrtlab_bus_state old_state;

	/* Serialize full state transition + notifier emission. */
	mutex_lock(&virtrtlab_bus_state_mutex);

	spin_lock_irqsave(&virtrtlab_bus_lock, flags);
	old_state = virtrtlab_bus_state;
	virtrtlab_bus_state = new_state;
	spin_unlock_irqrestore(&virtrtlab_bus_lock, flags);

	if (old_state != new_state) {
		unsigned long ev = (new_state == VIRTRTLAB_BUS_STATE_UP) ?
				   VIRTRTLAB_BUS_EVENT_UP :
				   VIRTRTLAB_BUS_EVENT_DOWN;
		virtrtlab_bus_notifier_call(ev);
	}

	mutex_unlock(&virtrtlab_bus_state_mutex);
}

static void virtrtlab_bus_reset(void)
{
	unsigned long flags;

	/*
	 * Serialize RESET + state update + UP notification so that
	 * concurrent sysfs writes to "state" cannot interleave their own
	 * notifications and confuse peripherals.
	 */
	mutex_lock(&virtrtlab_bus_state_mutex);

	/*
	 * Fire RESET before updating state so peripheral callbacks can observe
	 * the pre-reset state via virtrtlab_bus_get_state() and perform teardown
	 * accordingly. The subsequent UP notification signals the transition is
	 * complete and data flow may resume.
	 */
	virtrtlab_bus_notifier_call(VIRTRTLAB_BUS_EVENT_RESET);

	spin_lock_irqsave(&virtrtlab_bus_lock, flags);
	virtrtlab_bus_state = VIRTRTLAB_BUS_STATE_UP;
	spin_unlock_irqrestore(&virtrtlab_bus_lock, flags);

	virtrtlab_bus_notifier_call(VIRTRTLAB_BUS_EVENT_UP);

	mutex_unlock(&virtrtlab_bus_state_mutex);
}

static ssize_t state_store(struct kobject *kobj, struct kobj_attribute *attr,
			   const char *buf, size_t count)
{
	if (sysfs_streq(buf, "up")) {
		virtrtlab_bus_set_state(VIRTRTLAB_BUS_STATE_UP);
		return count;
	}

	if (sysfs_streq(buf, "down")) {
		virtrtlab_bus_set_state(VIRTRTLAB_BUS_STATE_DOWN);
		return count;
	}

	if (sysfs_streq(buf, "reset")) {
		virtrtlab_bus_reset();
		return count;
	}

	return -EINVAL;
}

static struct kobj_attribute virtrtlab_bus_state_attr =
	__ATTR(state, 0644, state_show, state_store);

static ssize_t clock_ns_show(struct kobject *kobj, struct kobj_attribute *attr,
			     char *buf)
{
	return sysfs_emit(buf, "%llu\n", (unsigned long long)ktime_get_ns());
}

static struct kobj_attribute virtrtlab_bus_clock_ns_attr =
	__ATTR_RO(clock_ns);

static ssize_t seed_show(struct kobject *kobj, struct kobj_attribute *attr,
			 char *buf)
{
	return sysfs_emit(buf, "%u\n", virtrtlab_bus_get_seed());
}

static ssize_t seed_store(struct kobject *kobj, struct kobj_attribute *attr,
			  const char *buf, size_t count)
{
	unsigned long flags;
	u32 value;
	int ret;

	ret = kstrtou32(buf, 10, &value);
	if (ret)
		return ret;
	if (!value)
		return -EINVAL;

	spin_lock_irqsave(&virtrtlab_bus_lock, flags);
	virtrtlab_bus_prng_state = value;
	spin_unlock_irqrestore(&virtrtlab_bus_lock, flags);

	return count;
}

static struct kobj_attribute virtrtlab_bus_seed_attr =
	__ATTR(seed, 0644, seed_show, seed_store);

static struct attribute *virtrtlab_bus_attrs[] = {
	&virtrtlab_bus_state_attr.attr,
	&virtrtlab_bus_clock_ns_attr.attr,
	&virtrtlab_bus_seed_attr.attr,
	NULL,
};

static const struct attribute_group virtrtlab_bus_attr_group = {
	.attrs = virtrtlab_bus_attrs,
};

/*
 * Module init / exit
 */

static int __init virtrtlab_core_init(void)
{
	int ret;

	/* 1. Register the virtual bus type → /sys/bus/virtrtlab/ */
	ret = bus_register(&virtrtlab_bus_type);
	if (ret) {
		pr_err("failed to register bus type: %d\n", ret);
		return ret;
	}

	/* 2. Create /sys/kernel/virtrtlab/ */
	virtrtlab_kobj = kobject_create_and_add("virtrtlab", kernel_kobj);
	if (!virtrtlab_kobj) {
		pr_err("failed to create root kobject\n");
		ret = -ENOMEM;
		goto err_bus_register;
	}

	/* 3. Populate root attributes (version) */
	ret = sysfs_create_group(virtrtlab_kobj, &virtrtlab_root_attr_group);
	if (ret) {
		pr_err("failed to create sysfs group: %d\n", ret);
		goto err_group;
	}

	/* 4. Create /sys/kernel/virtrtlab/buses/ */
	virtrtlab_buses_kobj = kobject_create_and_add("buses", virtrtlab_kobj);
	if (!virtrtlab_buses_kobj) {
		pr_err("failed to create buses kobject\n");
		ret = -ENOMEM;
		goto err_buses;
	}

	/* 5. Create /sys/kernel/virtrtlab/buses/vrtlbus0/ */
	virtrtlab_bus0_kobj =
		kobject_create_and_add(VIRTRTLAB_DEFAULT_BUS_NAME,
				       virtrtlab_buses_kobj);
	if (!virtrtlab_bus0_kobj) {
		pr_err("failed to create %s kobject\n", VIRTRTLAB_DEFAULT_BUS_NAME);
		ret = -ENOMEM;
		goto err_bus0;
	}

	ret = sysfs_create_group(virtrtlab_bus0_kobj, &virtrtlab_bus_attr_group);
	if (ret) {
		pr_err("failed to create %s attrs: %d\n",
		       VIRTRTLAB_DEFAULT_BUS_NAME, ret);
		goto err_bus0_put;
	}

	/* 6. Create /sys/kernel/virtrtlab/devices/ */
	virtrtlab_devices_kobj = kobject_create_and_add("devices", virtrtlab_kobj);
	if (!virtrtlab_devices_kobj) {
		pr_err("failed to create devices kobject\n");
		ret = -ENOMEM;
		goto err_devices;
	}

	pr_info("loaded (v%s)\n", VIRTRTLAB_VERSION);
	return 0;

	/* Error unwind — reverse order of allocation */
err_devices:
	sysfs_remove_group(virtrtlab_bus0_kobj, &virtrtlab_bus_attr_group);
err_bus0_put:
	kobject_put(virtrtlab_bus0_kobj);
err_bus0:
	kobject_put(virtrtlab_buses_kobj);
err_buses:
	sysfs_remove_group(virtrtlab_kobj, &virtrtlab_root_attr_group);
err_group:
	kobject_put(virtrtlab_kobj);
err_bus_register:
	bus_unregister(&virtrtlab_bus_type);
	return ret;
}

static void __exit virtrtlab_core_exit(void)
{
	/* Reverse order of init steps */
	kobject_put(virtrtlab_devices_kobj);
	sysfs_remove_group(virtrtlab_bus0_kobj, &virtrtlab_bus_attr_group);
	kobject_put(virtrtlab_bus0_kobj);
	kobject_put(virtrtlab_buses_kobj);
	sysfs_remove_group(virtrtlab_kobj, &virtrtlab_root_attr_group);
	kobject_put(virtrtlab_kobj);
	bus_unregister(&virtrtlab_bus_type);
	pr_info("unloaded\n");
}

module_init(virtrtlab_core_init);
module_exit(virtrtlab_core_exit);

MODULE_DESCRIPTION("VirtRTLab core — virtual bus type and sysfs kobject tree");
MODULE_AUTHOR("VirtRTLab");
MODULE_LICENSE("GPL");
