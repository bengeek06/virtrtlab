# SPDX-License-Identifier: GPL-2.0-only
#
# examples/Makefile — Build all VirtRTLab example AUT binaries.
#
# Builds against the host libc only; no kernel headers required except for
# aut_gpio_polarity which uses <linux/gpio.h> (part of linux-libc-dev).
#
# Usage:
#   make -C examples          # build everything
#   make -C examples clean    # remove binaries
#

CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -std=c11 -D_GNU_SOURCE

TARGETS = \
	aut_uart_timeout/aut \
	aut_gpio_polarity/aut \
	aut_uart_statemachine/aut

.PHONY: all clean

all: $(TARGETS)

aut_uart_timeout/aut: aut_uart_timeout/aut.c
	$(CC) $(CFLAGS) -o $@ $<

aut_gpio_polarity/aut: aut_gpio_polarity/aut.c
	$(CC) $(CFLAGS) -o $@ $<

aut_uart_statemachine/aut: aut_uart_statemachine/aut.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f $(TARGETS)
