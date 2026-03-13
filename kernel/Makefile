# Out-of-tree kernel module build
#
# Default (native Linux with headers installed):
#   make
#
# WSL (no /lib/modules/.../build): point KDIR to a prepared WSL2-Linux-Kernel tree:
#   make KDIR=~/projects/WSL2-Linux-Kernel

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

# Internal headers shared across VirtRTLab kernel modules
ccflags-y += -I$(src)/include

obj-m += virtrtlab_core.o
obj-m += virtrtlab_uart.o

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
