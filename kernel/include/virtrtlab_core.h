/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * VirtRTLab core — public interface for peripheral modules
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#ifndef VIRTRTLAB_CORE_H
#define VIRTRTLAB_CORE_H

#include <linux/device/bus.h>
#include <linux/kobject.h>
#include <linux/notifier.h>
#include <linux/types.h>

#define VIRTRTLAB_DEFAULT_BUS_NAME	"vrtlbus0"
#define VIRTRTLAB_DEFAULT_SEED	1U

enum virtrtlab_bus_state {
	VIRTRTLAB_BUS_STATE_UP = 0,
	VIRTRTLAB_BUS_STATE_DOWN,
};

enum virtrtlab_bus_event {
	VIRTRTLAB_BUS_EVENT_UP = 1,
	VIRTRTLAB_BUS_EVENT_DOWN,
	VIRTRTLAB_BUS_EVENT_RESET,
};

/*
 * Bus notifier contract for peripheral modules:
 * - callbacks run in process context from the sysfs store path and may sleep
 * - notifier return codes are ignored; the core does not veto the state
 *   change or reset sequence based on them
 * - a reset emits VIRTRTLAB_BUS_EVENT_RESET first and VIRTRTLAB_BUS_EVENT_UP
 *   after the core transitions the default bus back to the up state
 */

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

/*
 * Common default bus helpers shared by peripheral modules.
 * virtrtlab_bus_next_prng_u32() exposes the shared xorshift32 stream used for
 * stochastic bus-wide fault decisions. The internal state is never allowed to
 * remain zero because xorshift32 would otherwise lock up permanently.
 */
enum virtrtlab_bus_state virtrtlab_bus_get_state(void);
bool virtrtlab_bus_is_up(void);
u32 virtrtlab_bus_get_seed(void);
u32 virtrtlab_bus_next_prng_u32(void);
int virtrtlab_bus_register_notifier(struct notifier_block *nb);
int virtrtlab_bus_unregister_notifier(struct notifier_block *nb);

#endif /* VIRTRTLAB_CORE_H */
