/* SPDX-License-Identifier: MIT */
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
#include <syslog.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
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
		syslog(LOG_ERR, "cannot open %s: %m", inst->wire_path);
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
	if (close(inst->wire_fd) < 0)
		syslog(LOG_WARNING, "uart%d: close wire_fd: %m", inst->idx);
	inst->wire_fd = wire_open(inst);
	return inst->wire_fd;
}

static void client_close(struct uart_instance *inst, int epoll_fd)
{
	if (inst->client_fd < 0)
		return;
	epoll_loop_del(epoll_fd, inst->client_fd);
	if (close(inst->client_fd) < 0)
		syslog(LOG_WARNING, "uart%d: close client_fd: %m", inst->idx);
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
		       int epoll_fd, const char *run_dir, gid_t gid)
{
	struct sockaddr_un addr;
	mode_t             old_mask;

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
		syslog(LOG_ERR, "uart%d: socket: %m", inst->idx);
		goto err_wire;
	}

	unlink(inst->sock_path); /* remove stale socket from a previous run */

	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	/*
	 * Validate that sock_path fits in sun_path before binding.
	 * A silently truncated path would bind to the wrong socket and
	 * make unlink() in destroy() miss the real file.
	 */
	if (strlen(inst->sock_path) >= sizeof(addr.sun_path)) {
		syslog(LOG_ERR,
			"uart%d: socket path too long (max %zu): %s",
			inst->idx, sizeof(addr.sun_path) - 1, inst->sock_path);
		goto err_server;
	}
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", inst->sock_path);

	/*
	 * Set a restrictive umask so bind() creates the socket as srw-rw----
	 * (0660): root can accept, virtrtlab group can connect, others cannot.
	 * Restore immediately after bind() to not affect subsequent callers.
	 */
	old_mask = umask(0117);
	if (bind(inst->server_fd,
		 (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		umask(old_mask);
		syslog(LOG_ERR, "uart%d: bind %s: %m", inst->idx, inst->sock_path);
		goto err_server;
	}
	umask(old_mask);

	/*
	 * Transfer group ownership to virtrtlab so group members can connect
	 * without running as root.  The socket file was just created by bind(),
	 * so chown() on the path is safe and necessary: fchown() on an AF_UNIX
	 * socket fd does not affect the filesystem inode created by bind().
	 */
	if (gid != (gid_t)-1 && chown(inst->sock_path, 0, gid) < 0)
		syslog(LOG_WARNING, "uart%d: chown socket: %m", inst->idx);

	if (listen(inst->server_fd, 1) < 0) {
		syslog(LOG_ERR, "uart%d: listen: %m", inst->idx);
		goto err_bound;
	}

	/*
	 * WAIT_CLIENT: only server_fd is monitored by epoll.
	 * wire_fd is open (bytes accumulate in the kernel FIFO) but epoll
	 * does not watch it until a simulator connects.
	 */
	epoll_loop_add(epoll_fd, inst->server_fd, EPOLLIN, &inst->ctx_server);
	syslog(LOG_INFO, "uart%d: ready on %s", inst->idx, inst->sock_path);
	return 0;

err_bound:
	unlink(inst->sock_path);
err_server:
	if (close(inst->server_fd) < 0)
		syslog(LOG_WARNING, "uart%d: close server_fd: %m", inst->idx);
	inst->server_fd = -1;
err_wire:
	if (close(inst->wire_fd) < 0)
		syslog(LOG_WARNING, "uart%d: close wire_fd: %m", inst->idx);
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
		if (close(inst->client_fd) < 0)
			syslog(LOG_WARNING, "uart%d: close client_fd: %m", inst->idx);
		inst->client_fd = -1;
	}
	if (inst->server_fd >= 0) {
		if (close(inst->server_fd) < 0)
			syslog(LOG_WARNING, "uart%d: close server_fd: %m", inst->idx);
		inst->server_fd = -1;
	}
	if (inst->wire_fd >= 0) {
		if (close(inst->wire_fd) < 0)
			syslog(LOG_WARNING, "uart%d: close wire_fd: %m", inst->idx);
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
		syslog(LOG_WARNING, "uart%d: drain: fcntl O_NONBLOCK: %m", inst->idx);
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
			if (close(inst->wire_fd) < 0)
				syslog(LOG_WARNING, "uart%d: close wire_fd (drain-reopen): %m",
				       inst->idx);
			inst->wire_fd = wire_open(inst);
			if (inst->wire_fd < 0)
				return; /* wire lost; instance stays with no wire */
			break; /* new fd needs no drain */
		}
		/* r < 0 */
		if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EIO)
			syslog(LOG_WARNING, "uart%d: drain wire: %m", inst->idx);
		break; /* EAGAIN / EIO: drain complete */
	}

	/* Restore original flags before re-entering WAIT_CLIENT. */
	if (fcntl(inst->wire_fd, F_SETFL, fl_save) < 0)
		syslog(LOG_WARNING, "uart%d: drain: restore fcntl: %m", inst->idx);

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

	/*
	 * State guard: a single epoll_wait batch can deliver events for fds
	 * that were removed mid-batch by an earlier event in the same call.
	 * If we are no longer in WAIT_CLIENT, discard this stale event.
	 */
	if (inst->state != WAIT_CLIENT)
		return;

	fd = accept4(inst->server_fd, NULL, NULL, SOCK_CLOEXEC);
	if (fd < 0) {
		syslog(LOG_ERR, "uart%d: accept4: %m", inst->idx);
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

	/* State guard: discard stale event delivered after a same-batch transition. */
	if (inst->state != RELAYING)
		return;

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
		syslog(LOG_WARNING, "uart%d: read wire: %m", inst->idx);
		return;
	}

	if (n == 0) {
		/*
		 * EOF: wire device was reset.
		 * Reopen, disconnect simulator, wait for next client.
		 */
		if (wire_reopen(inst, epoll_fd) < 0) {
			/* m1: wire device lost permanently — take instance offline. */
			syslog(LOG_ERR, "uart%d: wire lost, going offline", inst->idx);
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
			syslog(LOG_WARNING, "uart%d: send client: %m", inst->idx);
	} else {
		syslog(LOG_WARNING, "uart%d: partial send (%zd/%zd), dropping client",
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

	/* State guard: discard stale event delivered after a same-batch transition. */
	if (inst->state != RELAYING)
		return;

	n = recv(inst->client_fd, inst->sock_buf, sizeof(inst->sock_buf), 0);

	if (n == 0) {
		/* Simulator disconnected cleanly. */
		close_client_and_drain(inst, epoll_fd);
		return;
	}

	if (n < 0) {
		if (errno != ECONNRESET && errno != ENOTCONN)
			syslog(LOG_WARNING, "uart%d: recv client: %m", inst->idx);
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
			syslog(LOG_WARNING, "uart%d: write wire: %m", inst->idx);
	}
}
