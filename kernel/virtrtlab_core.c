#include <linux/init.h>
#include <linux/module.h>
#include <linux/printk.h>

static int __init virtrtlab_core_init(void)
{
	pr_info("virtrtlab_core: loaded\n");
	return 0;
}

static void __exit virtrtlab_core_exit(void)
{
	pr_info("virtrtlab_core: unloaded\n");
}

module_init(virtrtlab_core_init);
module_exit(virtrtlab_core_exit);

MODULE_DESCRIPTION("VirtRTLab core (virtual bus + injection infra) - stub");
MODULE_AUTHOR("VirtRTLab");
MODULE_LICENSE("GPL");
