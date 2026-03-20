/* SPDX-License-Identifier: MIT */
/*
 * epoll_loop.c — shared epoll event loop for virtrtlabd
 *
 * A single epoll instance serves all UART instances plus the signalfd.
 * Each fd is registered with a pointer to an evt_ctx struct stored directly
 * in epoll_event.data.ptr — no run-time allocation in the dispatch path.
 *
 * Signal handling uses signalfd(2): SIGTERM and SIGINT are blocked from async
 * delivery and become ordinary readable events in the epoll loop, with no
 * async-signal-safety constraints.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <unistd.h>

#include "epoll_loop.h"
#include "instance.h"

/* Module-level instance registry — set once before epoll_loop_run(). */
static struct uart_instance *g_instances;
static int                   g_num_instances;

/* ---- epoll fd management ------------------------------------------------- */

int epoll_loop_create(void)
{
	int fd;

	fd = epoll_create1(EPOLL_CLOEXEC);
	if (fd < 0) {
		perror("epoll_create1");
		abort();
	}
	return fd;
}

void epoll_loop_add(int epoll_fd, int fd, uint32_t events, struct evt_ctx *ctx)
{
	struct epoll_event ev;

	memset(&ev, 0, sizeof(ev));
	ev.events   = events;
	ev.data.ptr = ctx;

	if (epoll_ctl(epoll_fd, EPOLL_CTL_ADD, fd, &ev) < 0) {
		perror("epoll_ctl ADD");
		abort(); /* M2: unrecoverable — fd would not be monitored */
	}
}

void epoll_loop_del(int epoll_fd, int fd)
{
	/*
	 * Closing an fd auto-removes it from epoll on Linux ≥ 2.6.9, but
	 * explicit removal is required when the fd is removed while still
	 * open (e.g. server_fd removed during RELAYING, then kept open).
	 * Ignore ENOENT: the fd may have been closed already.
	 */
	if (epoll_ctl(epoll_fd, EPOLL_CTL_DEL, fd, NULL) < 0) {
		if (errno != ENOENT && errno != EBADF)
			perror("epoll_ctl DEL");
	}
}

/* ---- Instance registry --------------------------------------------------- */

void epoll_loop_set_instances(struct uart_instance *instances, int n)
{
	g_instances     = instances;
	g_num_instances = n;
}

/* ---- Signal handler ------------------------------------------------------ */

/*
 * on_signal - drain signalfd and trigger clean shutdown.
 *
 * Iterates all instances calling uart_instance_destroy(), then exits.
 * Called from dispatch() when sig_fd is readable (SIGTERM or SIGINT).
 */
static void on_signal(int sig_fd)
{
	struct signalfd_siginfo info;
	ssize_t                 n;
	int                     i;

	/*
	 * Read the pending signal info — required to re-arm the signalfd.
	 * We don't use the payload; any SIGTERM/SIGINT triggers shutdown.
	 */
	do {
		n = read(sig_fd, &info, sizeof(info));
	} while (n < 0 && errno == EINTR);

	for (i = 0; i < g_num_instances; i++)
		uart_instance_destroy(&g_instances[i], -1);

	exit(EXIT_SUCCESS);
}

/* ---- Event dispatch ------------------------------------------------------ */

/*
 * dispatch - route one epoll event to the appropriate handler.
 *
 * sig_fd is stored in the ROLE_SIGNAL evt_ctx as its "inst" field (cast to
 * uart_instance*) so that on_signal() can read from it.  This avoids a
 * separate global for sig_fd.
 */
static void dispatch(struct evt_ctx *ctx, uint32_t events, int epoll_fd)
{
	/*
	 * B2: Check for error/hangup before routing to a role handler.
	 * EPOLLHUP fires on peer close (normal for AF_UNIX clients),
	 * EPOLLERR signals an unexpected fault on the fd.  Route to the
	 * normal read handler in both cases: read/recv will return 0 or -1
	 * and the handler will transition the state machine cleanly.
	 * If EPOLLIN is also set we fall through to the switch as normal.
	 */
	if ((events & (EPOLLHUP | EPOLLERR)) && !(events & EPOLLIN)) {
		switch (ctx->role) {
		case ROLE_CLIENT:
			on_client_readable(ctx->inst, epoll_fd);
			return;
		case ROLE_WIRE:
			on_wire_readable(ctx->inst, epoll_fd);
			return;
		default:
			break; /* server/signal errors fall through */
		}
	}

	switch (ctx->role) {
	case ROLE_SERVER:
		on_server_readable(ctx->inst, epoll_fd);
		break;

	case ROLE_WIRE:
		on_wire_readable(ctx->inst, epoll_fd);
		break;

	case ROLE_CLIENT:
		on_client_readable(ctx->inst, epoll_fd);
		break;

	case ROLE_SIGNAL:
		/*
		 * ctx->inst is reused to carry sig_fd (cast).
		 * Pass sig_fd so on_signal() can read() from it.
		 */
		on_signal((int)(intptr_t)ctx->inst);
		break;

	default:
		fprintf(stderr, "virtrtlabd: unknown fd_role %d\n", ctx->role);
		break;
	}
}

/* ---- Main loop ----------------------------------------------------------- */

void epoll_loop_run(int epoll_fd)
{
	struct epoll_event events[MAX_EVENTS];
	int                n;
	int                i;

	for (;;) {
		n = epoll_wait(epoll_fd, events, MAX_EVENTS, -1);
		if (n < 0) {
			if (errno == EINTR)
				continue; /* spurious wakeup; signalfd handles signals */
			perror("epoll_wait");
			break;
		}

		for (i = 0; i < n; i++)
			dispatch(events[i].data.ptr, events[i].events, epoll_fd);
	}
}
