/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * VirtRTLab core — public interface for peripheral modules
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#ifndef _VIRTRTLAB_CORE_H
#define _VIRTRTLAB_CORE_H

#include <linux/device/bus.h>

/*
 * The VirtRTLab virtual bus type.
 * Peripheral modules register their devices on this bus via device_register().
 */
extern const struct bus_type virtrtlab_bus_type;

#endif /* _VIRTRTLAB_CORE_H */
