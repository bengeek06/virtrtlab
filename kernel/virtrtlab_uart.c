#include <linux/init.h>
#include <linux/module.h>
#include <linux/printk.h>

static int __init virtrtlab_uart_init(void)
{
	pr_info("virtrtlab_uart: loaded\n");
	return 0;
}

static void __exit virtrtlab_uart_exit(void)
{
	pr_info("virtrtlab_uart: unloaded\n");
}

module_init(virtrtlab_uart_init);
module_exit(virtrtlab_uart_exit);

MODULE_DESCRIPTION("VirtRTLab UART peripheral - stub");
MODULE_AUTHOR("VirtRTLab");
MODULE_LICENSE("GPL");
