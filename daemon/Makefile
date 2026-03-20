# SPDX-License-Identifier: MIT

CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -std=gnu11

SRCS    = main.c epoll_loop.c instance.c
OBJS    = $(SRCS:.c=.o)
TARGET  = virtrtlabd

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $^

main.o:       main.c epoll_loop.h instance.h
	$(CC) $(CFLAGS) -c -o $@ main.c

epoll_loop.o: epoll_loop.c epoll_loop.h instance.h
	$(CC) $(CFLAGS) -c -o $@ epoll_loop.c

instance.o:   instance.c instance.h epoll_loop.h
	$(CC) $(CFLAGS) -c -o $@ instance.c

clean:
	rm -f $(OBJS) $(TARGET)
