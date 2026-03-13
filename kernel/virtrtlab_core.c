// SPDX-License-Identifier: GPL-2.0-only
/*
 * VirtRTLab core — virtual bus registration and sysfs kobject tree
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/init.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/device/bus.h>

#define VIRTRTLAB_VERSION "0.1.0"

/*
 * Bus type — visible in /sys/bus/virtrtlab/
 */

static struct bus_type virtrtlab_bus_type = {
	.name = "virtrtlab",
};

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
static struct kobject *virtrtlab_devices_kobj;

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

	/* 5. Create /sys/kernel/virtrtlab/devices/ */
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
