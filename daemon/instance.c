/* SPDX-License-Identifier: GPL-2.0 */
/*
 * instance.c — per-UART relay state machine for virtrtlabd
 *
 * State machine per uart_instance:
 *   WAIT_CLIENT : server_fd in epoll; wire_fd open but not monitored.
 *   RELAYING    : wire_fd + client_fd in epoll; server_fd removed.
 *   DRAINING    : wire_fd in epoll (O_NONBLOCK); client_fd closed.
 *
 * epoll registration summary:
 *   WAIT_CLIENT  → server_fd(EPOLLIN)
 *   RELAYING     → wire_fd(EPOLLIN) + client_fd(EPOLLIN)
 *   DRAINING     → wire_fd(EPOLLIN, O_NONBLOCK on the fd itself)
 *
 * Closing an fd removes it from all epoll instances automatically
 * (Linux ≥ 2.6.9), so uart_instance_destroy() needs no explicit del.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "instance.h"
#include "epoll_loop.h"

/* ---- private helpers ----------------------------------------------------- */

static int wire_open(struct uart_instance *inst)
{
	int fd;

	fd = open(inst->wire_path, O_RDWR | O_CLOEXEC);
	if (fd < 0)
		fprintf(stderr, "virtrtlabd: cannot open %s: %s\n",
			inst->wire_path, strerror(errno));
	return fd;
}

/*
 * wire_reopen - deregister current wire_fd from epoll, close it, open fresh.
 *
 * Must only be called when wire_fd is registered in epoll (RELAYING or
 * DRAINING).  The new fd is NOT added to epoll — the caller decides when.
 * Returns new fd (stored in inst->wire_fd), or -1 on error.
 */
static int wire_reopen(struct uart_instance *inst, int epoll_fd)
{
	epoll_loop_del(epoll_fd, inst->wire_fd);
	close(inst->wire_fd);
	inst->wire_fd = wire_open(inst);
	return inst->wire_fd;
}

static void client_close(struct uart_instance *inst, int epoll_fd)
{
	if (inst->client_fd < 0)
		return;
	epoll_loop_del(epoll_fd, inst->client_fd);
	close(inst->client_fd);
	inst->client_fd = -1;
}

/*
 * enter_wait_client - transition any state → WAIT_CLIENT.
 * Adds server_fd to epoll; does NOT touch wire_fd.
 */
static void enter_wait_client(struct uart_instance *inst, int epoll_fd)
{
	epoll_loop_add(epoll_fd, inst->server_fd, EPOLLIN, &inst->ctx_server);
	inst->state = WAIT_CLIENT;
}

/* ---- public API ---------------------------------------------------------- */

int uart_instance_init(struct uart_instance *inst, int idx,
		       int epoll_fd, const char *run_dir)
{
	struct sockaddr_un addr;

	inst->idx       = idx;
	inst->client_fd = -1;
	inst->wire_fd   = -1;
	inst->server_fd = -1;
	inst->state     = WAIT_CLIENT;

	snprintf(inst->wire_path, WIRE_PATH_MAX,
		 "/dev/virtrtlab-wire%d", idx);
	snprintf(inst->sock_path, SOCK_PATH_MAX,
		 "%s/uart%d.sock", run_dir, idx);

	/* Initialise dispatch contexts once; inst pointer never changes. */
	inst->ctx_wire.inst   = inst;
	inst->ctx_wire.role   = ROLE_WIRE;
	inst->ctx_server.inst = inst;
	inst->ctx_server.role = ROLE_SERVER;
	inst->ctx_client.inst = inst;
	inst->ctx_client.role = ROLE_CLIENT;

	/* Open wire device — exclusive; EBUSY if a concurrent caller got there. */
	inst->wire_fd = wire_open(inst);
	if (inst->wire_fd < 0)
		return -1;

	inst->server_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (inst->server_fd < 0) {
		perror("socket");
		goto err_wire;
	}

	unlink(inst->sock_path); /* remove stale socket from a previous run */

	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	strncpy(addr.sun_path, inst->sock_path, sizeof(addr.sun_path) - 1);

	if (bind(inst->server_fd,
		 (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		perror("bind");
		goto err_server;
	}

	if (listen(inst->server_fd, 1) < 0) {
		perror("listen");
		goto err_bound;
	}

	/*
	 * WAIT_CLIENT: only server_fd is monitored by epoll.
	 * wire_fd is open (bytes accumulate in the kernel FIFO) but epoll
	 * does not watch it until a simulator connects.
	 */
	epoll_loop_add(epoll_fd, inst->server_fd, EPOLLIN, &inst->ctx_server);
	return 0;

err_bound:
	unlink(inst->sock_path);
err_server:
	close(inst->server_fd);
	inst->server_fd = -1;
err_wire:
	close(inst->wire_fd);
	inst->wire_fd = -1;
	return -1;
}

void uart_instance_destroy(struct uart_instance *inst, int epoll_fd)
{
	/*
	 * epoll_fd is unused: closing an fd auto-removes it from every epoll
	 * instance on Linux ≥ 2.6.9, so no explicit epoll_loop_del is needed.
	 */
	(void)epoll_fd;

	unlink(inst->sock_path);

	if (inst->client_fd >= 0) {
		close(inst->client_fd);
		inst->client_fd = -1;
	}
	if (inst->server_fd >= 0) {
		close(inst->server_fd);
		inst->server_fd = -1;
	}
	if (inst->wire_fd >= 0) {
		close(inst->wire_fd);
		inst->wire_fd = -1;
	}
}

/* ---- epoll dispatch entry points ----------------------------------------- */

/*
 * on_server_readable - called in WAIT_CLIENT when a simulator connects.
 *
 * Accepts the connection, removes server_fd from epoll, and registers
 * wire_fd + client_fd to enter RELAYING state.
 */
void on_server_readable(struct uart_instance *inst, int epoll_fd)
{
	int fd;

	fd = accept4(inst->server_fd, NULL, NULL, SOCK_CLOEXEC);
	if (fd < 0) {
		perror("accept4");
		return;
	}

	inst->client_fd = fd;

	/* Stop accepting (backlog=1 already rejects concurrent connect). */
	epoll_loop_del(epoll_fd, inst->server_fd);

	/* RELAYING: watch wire (AUT→simulator) and client (simulator→AUT). */
	epoll_loop_add(epoll_fd, inst->wire_fd,   EPOLLIN, &inst->ctx_wire);
	epoll_loop_add(epoll_fd, inst->client_fd, EPOLLIN, &inst->ctx_client);

	inst->state = RELAYING;
}

/*
 * on_wire_readable - called in RELAYING when wire_fd is readable.
 *
 * Forwards bytes from the AUT to the simulator; handles EIO (state=down)
 * and EOF (state=reset).
 *
 * Note: DRAINING is no longer handled here.  On simulator disconnect,
 * on_client_readable() drains wire_fd inline in non-blocking mode so that
 * no data is required to trigger the epoll event (avoids the deadlock where
 * wire_fd has no data → EPOLLIN never fires → daemon stuck in DRAINING).
 */
void on_wire_readable(struct uart_instance *inst, int epoll_fd)
{
	ssize_t n;

	n = read(inst->wire_fd, inst->wire_buf, sizeof(inst->wire_buf));

	if (n < 0) {
		if (errno == EIO) {
			/*
			 * state=down: bus halted, wire_fd still valid.
			 * Back off 10 ms; epoll re-arms automatically.
			 */
			usleep(10000);
			return;
		}
		perror("read wire (relaying)");
		return;
	}

	if (n == 0) {
		/*
		 * EOF: state=reset has invalidated this wire_fd.
		 * Reopen, disconnect simulator, wait for next client.
		 */
		if (wire_reopen(inst, epoll_fd) < 0)
			return;
		client_close(inst, epoll_fd);
		enter_wait_client(inst, epoll_fd);
		return;
	}

	/* Forward AUT bytes to simulator. */
	if (write(inst->client_fd, inst->wire_buf, (size_t)n) < 0)
		perror("write client");
}

/*
 * on_client_readable - called in RELAYING when the simulator sends bytes
 * or closes the connection.
 */
void on_client_readable(struct uart_instance *inst, int epoll_fd)
{
	ssize_t n;
	ssize_t r;
	int     fl;

	n = recv(inst->client_fd, inst->sock_buf, sizeof(inst->sock_buf), 0);

	if (n == 0) {
		/*
		 * Simulator disconnected.
		 *
		 * Close client_fd and remove wire_fd from epoll — bytes on
		 * the wire device must be drained before the next simulator
		 * connects, but epoll-driven DRAINING is unreliable when the
		 * AUT has produced no data (wire_fd not readable → EPOLLIN
		 * never fires → deadlock).  Drain inline instead.
		 */
		client_close(inst, epoll_fd);
		epoll_loop_del(epoll_fd, inst->wire_fd);

		fl = fcntl(inst->wire_fd, F_GETFL, 0);
		if (fl >= 0 &&
		    fcntl(inst->wire_fd, F_SETFL, fl | O_NONBLOCK) < 0)
			perror("drain: fcntl O_NONBLOCK");

		while (1) {
			r = read(inst->wire_fd, inst->wire_buf,
				 sizeof(inst->wire_buf));
			if (r < 0) {
				/* EAGAIN: all stale bytes consumed.
				 * EIO:    bus went state=down; wire_fd valid.
				 * Either way: drain complete. */
				if (errno != EAGAIN && errno != EIO)
					perror("drain wire");
				break;
			}
			if (r == 0) {
				/* state=reset: reopen wire device. */
				close(inst->wire_fd);
				inst->wire_fd = -1;
				inst->wire_fd = wire_open(inst);
				if (inst->wire_fd < 0)
					return; /* cannot recover; skip */
				/* New fd is not in epoll yet; enter_wait_client
				 * will add server_fd; wire_fd added on next
				 * on_server_readable(). */
				goto wait_client;
			}
			/* r > 0: stale byte(s) — discard and continue. */
		}

		fl = fcntl(inst->wire_fd, F_GETFL, 0);
		if (fl >= 0)
			fcntl(inst->wire_fd, F_SETFL, fl & ~O_NONBLOCK);

wait_client:
		enter_wait_client(inst, epoll_fd);
		return;
	}

	if (n < 0) {
		perror("recv client");
		return;
	}

	/*
	 * Forward simulator bytes to AUT via wire device.
	 * Wire write is non-blocking per spec: EAGAIN = RX buffer full,
	 * byte is lost, kernel increments stat_overruns.
	 */
	if (write(inst->wire_fd, inst->sock_buf, (size_t)n) < 0) {
		if (errno != EAGAIN)
			perror("write wire");
	}
}
