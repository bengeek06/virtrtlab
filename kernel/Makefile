# Out-of-tree kernel module build
#
# Default (native Linux with headers installed):
#   make
#
# Custom kernel tree (e.g. WSL with no /lib/modules/.../build): override KDIR:
#   make KDIR=/path/to/linux-kernel

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

# Internal headers shared across VirtRTLab kernel modules
ccflags-y += -I$(src)/include

obj-m += virtrtlab_core.o
obj-m += virtrtlab_gpio.o
obj-m += virtrtlab_uart.o

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
