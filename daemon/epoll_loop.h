/* SPDX-License-Identifier: GPL-2.0 */
/*
 * epoll_loop.h — shared epoll instance for virtrtlabd
 *
 * A single epoll fd is shared across all UART instances.
 * With num_uarts ≤ 8, at most 3 fds per instance (wire + server + client)
 * plus 1 signalfd = 25 fds total — well within MAX_EVENTS.
 *
 * Dispatch is driven by evt_ctx pointers stored in epoll_event.data.ptr.
 */

#ifndef VIRTRTLAB_EPOLL_LOOP_H
#define VIRTRTLAB_EPOLL_LOOP_H

#include <stdint.h>
#include "instance.h"

#define MAX_EVENTS 32

/* ----- epoll fd management ------------------------------------------------ */

/* Create the epoll instance. Calls abort() on failure (unrecoverable). */
int  epoll_loop_create(void);

/* Register fd with the given events mask and dispatch context. */
void epoll_loop_add(int epoll_fd, int fd, uint32_t events, struct evt_ctx *ctx);

/* Deregister fd (safe to call on fds already removed or -1). */
void epoll_loop_del(int epoll_fd, int fd);

/* Modify the events mask of an already-registered fd. */
void epoll_loop_mod(int epoll_fd, int fd, uint32_t events, struct evt_ctx *ctx);

/* ----- Instance registry -------------------------------------------------- */

/*
 * Store the instance array so the signal handler can iterate all instances
 * without a global pointer in every translation unit.
 * Must be called before epoll_loop_run().
 */
void epoll_loop_set_instances(struct uart_instance *instances, int n);

/* ----- Main loop ---------------------------------------------------------- */

/*
 * epoll_loop_run - enter the blocking event dispatch loop.
 * Never returns under normal operation; exits via on_signal() on SIGTERM/SIGINT.
 */
void epoll_loop_run(int epoll_fd);

#endif /* VIRTRTLAB_EPOLL_LOOP_H */
