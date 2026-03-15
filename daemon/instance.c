/* SPDX-License-Identifier: GPL-2.0 */
/*
 * instance.c — per-UART relay state machine for virtrtlabd
 *
 * State machine per uart_instance:
 *   WAIT_CLIENT : server_fd in epoll; wire_fd open but not monitored.
 *   RELAYING    : wire_fd + client_fd in epoll; server_fd removed.
 *
 * epoll registration summary:
 *   WAIT_CLIENT  → server_fd(EPOLLIN)
 *   RELAYING     → wire_fd(EPOLLIN) + client_fd(EPOLLIN)
 *
 * On simulator disconnect, stale wire bytes are drained inline with
 * O_NONBLOCK before re-entering WAIT_CLIENT.  An epoll-driven DRAINING
 * state was considered but rejected: it deadlocks when the AUT has
 * produced no data (EPOLLIN never fires on an empty wire device).
 * The inline drain is bounded by the kernel ring buffer and completes
 * in O(buffer_size / read_size) iterations.
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
 * Must only be called when wire_fd is registered in epoll (RELAYING state).
 * The new fd is NOT added to epoll — the caller decides when.
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
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", inst->sock_path);

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

/* ---- Disconnect + drain helper ------------------------------------------- */

/*
 * close_client_and_drain - disconnect the simulator and drain stale wire bytes
 * before re-entering WAIT_CLIENT.
 *
 * Sequence:
 *   1. Close client_fd (removes it from epoll).
 *   2. Remove wire_fd from epoll.
 *   3. Set O_NONBLOCK on wire_fd; drain the kernel ring buffer:
 *        r > 0  — discard, continue
 *        r == 0 — EOF (wire reset); reopen wire_fd
 *        r < 0  — EAGAIN/EIO: drain complete
 *   4. Restore original flags.
 *   5. enter_wait_client().
 *
 * B3: if O_NONBLOCK cannot be set, drain is skipped to avoid blocking.
 */
static void close_client_and_drain(struct uart_instance *inst, int epoll_fd)
{
	int     fl_save;
	ssize_t r;

	client_close(inst, epoll_fd);
	epoll_loop_del(epoll_fd, inst->wire_fd);

	fl_save = fcntl(inst->wire_fd, F_GETFL, 0);
	if (fl_save < 0 ||
	    fcntl(inst->wire_fd, F_SETFL, fl_save | O_NONBLOCK) < 0) {
		/* B3: cannot set O_NONBLOCK; skip drain to avoid blocking. */
		perror("drain: fcntl O_NONBLOCK");
		enter_wait_client(inst, epoll_fd);
		return;
	}

	for (;;) {
		r = read(inst->wire_fd, inst->wire_buf, sizeof(inst->wire_buf));
		if (r > 0)
			continue; /* discard stale bytes */
		if (r == 0) {
			/*
			 * EOF: wire device was reset between simulator
			 * disconnect and drain — reopen fresh.
			 */
			close(inst->wire_fd);
			inst->wire_fd = wire_open(inst);
			if (inst->wire_fd < 0)
				return; /* wire lost; instance stays with no wire */
			break; /* new fd needs no drain */
		}
		/* r < 0 */
		if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EIO)
			perror("drain wire");
		break; /* EAGAIN / EIO: drain complete */
	}

	/* Restore original flags before re-entering WAIT_CLIENT. */
	if (fcntl(inst->wire_fd, F_SETFL, fl_save) < 0)
		perror("drain: restore fcntl");

	enter_wait_client(inst, epoll_fd);
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
 */
void on_wire_readable(struct uart_instance *inst, int epoll_fd)
{
	ssize_t n;
	ssize_t nw;

	n = read(inst->wire_fd, inst->wire_buf, sizeof(inst->wire_buf));

	if (n < 0) {
		if (errno == EIO) {
			/*
			 * state=down: bus halted, wire_fd still valid.
			 * Back off briefly; epoll re-arms automatically.
			 * m3: usleep() stalls all instances — acceptable for
			 * ≤ 8 UARTs at embedded baud rates.
			 */
			usleep(10000);
			return;
		}
		perror("read wire");
		return;
	}

	if (n == 0) {
		/*
		 * EOF: wire device was reset.
		 * Reopen, disconnect simulator, wait for next client.
		 */
		if (wire_reopen(inst, epoll_fd) < 0) {
			/* m1: wire device lost permanently — take instance offline. */
			fprintf(stderr, "virtrtlabd: uart%d: wire lost, going offline\n",
				inst->idx);
			client_close(inst, epoll_fd);
			uart_instance_destroy(inst, epoll_fd);
			return;
		}
		client_close(inst, epoll_fd);
		enter_wait_client(inst, epoll_fd);
		return;
	}

	/*
	 * Forward AUT bytes to simulator.
	 * MSG_NOSIGNAL prevents SIGPIPE on a closed socket; we handle the
	 * resulting EPIPE by disconnecting and draining.
	 * A partial send means the simulator's receive buffer is full —
	 * drop the client rather than silently losing data.
	 */
	nw = send(inst->client_fd, inst->wire_buf, (size_t)n, MSG_NOSIGNAL);
	if (nw == (ssize_t)n)
		return; /* fast path */

	if (nw < 0) {
		if (errno != EPIPE && errno != ECONNRESET)
			perror("send client");
	} else {
		fprintf(stderr, "virtrtlabd: uart%d: partial send (%zd/%zd), dropping client\n",
			inst->idx, nw, n);
	}
	close_client_and_drain(inst, epoll_fd);
}

/*
 * on_client_readable - called in RELAYING when the simulator sends bytes
 * or closes the connection.
 */
void on_client_readable(struct uart_instance *inst, int epoll_fd)
{
	ssize_t n;

	n = recv(inst->client_fd, inst->sock_buf, sizeof(inst->sock_buf), 0);

	if (n == 0) {
		/* Simulator disconnected cleanly. */
		close_client_and_drain(inst, epoll_fd);
		return;
	}

	if (n < 0) {
		if (errno != ECONNRESET && errno != ENOTCONN)
			perror("recv client");
		close_client_and_drain(inst, epoll_fd);
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
