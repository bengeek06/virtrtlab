/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * VirtRTLab core — public interface for peripheral modules
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#ifndef VIRTRTLAB_CORE_H
#define VIRTRTLAB_CORE_H

#include <linux/kobject.h>
#include <linux/device/bus.h>

/*
 * The VirtRTLab virtual bus type.
 * Peripheral modules register their devices on this bus via device_register().
 */
extern const struct bus_type virtrtlab_bus_type;

/*
 * Root kobject for /sys/kernel/virtrtlab/devices/.
 * Peripheral modules set dev.kobj.parent to this before device_add() so their
 * devices appear at /sys/kernel/virtrtlab/devices/<name>/ as primary path.
 */
extern struct kobject *virtrtlab_devices_kobj;

#endif /* VIRTRTLAB_CORE_H */
