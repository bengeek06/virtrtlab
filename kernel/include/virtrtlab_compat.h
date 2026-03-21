/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef _VIRTRTLAB_COMPAT_H_
#define _VIRTRTLAB_COMPAT_H_

#include <linux/hrtimer.h>
#include <linux/version.h>

/*
 * Keep compatibility gates in one place so module files stay focused on logic.
 */
#if KERNEL_VERSION(6, 6, 0) <= LINUX_VERSION_CODE
#define VIRTRTLAB_HAVE_SSIZE_T_TTY_WRITE 1
#endif

#if KERNEL_VERSION(6, 17, 0) <= LINUX_VERSION_CODE
#define VIRTRTLAB_HAVE_INT_GPIO_SET 1
#endif

static inline void virtrtlab_hrtimer_init_compat(struct hrtimer *timer,
						 enum hrtimer_restart (*fn)(struct hrtimer *))
{
#if KERNEL_VERSION(6, 15, 0) <= LINUX_VERSION_CODE
	hrtimer_setup(timer, fn, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
#else
	hrtimer_init(timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	timer->function = fn;
#endif
}

#endif /* _VIRTRTLAB_COMPAT_H_ */
