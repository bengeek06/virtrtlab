// SPDX-License-Identifier: GPL-2.0-only
/*
 * VirtRTLab UART — virtual UART peripheral stub
 *
 * Part of VirtRTLab — Linux real-time peripheral simulation framework
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/init.h>
#include <linux/module.h>
#include <linux/printk.h>
/* Included in anticipation of device registration on virtrtlab_bus_type.
 * Actual bus registration is implemented in a subsequent issue.
 */
#include "virtrtlab_core.h"

static int __init virtrtlab_uart_init(void)
{
	pr_info("loaded\n");
	return 0;
}

static void __exit virtrtlab_uart_exit(void)
{
	pr_info("unloaded\n");
}

module_init(virtrtlab_uart_init);
module_exit(virtrtlab_uart_exit);

MODULE_DESCRIPTION("VirtRTLab UART peripheral stub");
MODULE_AUTHOR("VirtRTLab");
MODULE_LICENSE("GPL");
