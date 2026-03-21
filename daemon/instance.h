/* SPDX-License-Identifier: MIT */
/*
 * instance.h — per-UART instance state for virtrtlabd
 *
 * Each UART instance N owns:
 *   wire_fd   : /dev/virtrtlab-wireN  (kernel ↔ daemon)
 *   server_fd : /run/virtrtlab/uartN.sock (listening)
 *   client_fd : connected simulator (-1 if none)
 *
 * State machine:
 *   WAIT_CLIENT → (accept)            → RELAYING
 *   RELAYING    → (client disconnect) → drain wire inline → WAIT_CLIENT
 *   RELAYING    → (wire EOF)          → reopen wire      → WAIT_CLIENT
 */

#ifndef VIRTRTLAB_INSTANCE_H
#define VIRTRTLAB_INSTANCE_H

#include <sys/types.h>  /* gid_t */

#define WIRE_BUF_SIZE   4096
#define SOCK_BUF_SIZE   4096
#define SOCK_PATH_MAX    108  /* sizeof(struct sockaddr_un.sun_path) */
#define WIRE_PATH_MAX     64

enum inst_state {
	WAIT_CLIENT,
	RELAYING,
};

enum fd_role {
	ROLE_WIRE,
	ROLE_SERVER,
	ROLE_CLIENT,
	ROLE_SIGNAL,
};

/* Forward declaration — epoll_loop.h includes this header, not the reverse. */
struct uart_instance;

struct evt_ctx {
	struct uart_instance *inst;
	enum fd_role          role;
};

struct uart_instance {
	int              idx;
	int              wire_fd;
	int              server_fd;
	int              client_fd;    /* -1 when no simulator is connected */
	enum inst_state  state;

	/* Static relay buffers — no heap allocation in the hot path. */
	char wire_buf[WIRE_BUF_SIZE]; /* AUT → simulator                  */
	char sock_buf[SOCK_BUF_SIZE]; /* simulator → AUT                   */

	/* epoll dispatch contexts — one per role, embedded to avoid malloc. */
	struct evt_ctx ctx_wire;
	struct evt_ctx ctx_server;
	struct evt_ctx ctx_client;

	char sock_path[SOCK_PATH_MAX]; /* /run/virtrtlab/uartN.sock        */
	char wire_path[WIRE_PATH_MAX]; /* /dev/virtrtlab-wireN             */
};

/* ----- Functions implemented in instance.c -------------------------------- */

/*
 * uart_instance_init - open wire device and listening socket, register in epoll.
 * Returns 0 on success, -1 on error (wire device missing → AC5 clean failure).
 */
int  uart_instance_init(struct uart_instance *inst, int idx,
			int epoll_fd, const char *run_dir, gid_t gid);

/*
 * uart_instance_destroy - unlink socket, close all fds, deregister from epoll.
 * Called on SIGTERM/SIGINT before exit.
 */
void uart_instance_destroy(struct uart_instance *inst, int epoll_fd);

/* epoll dispatch entry points — called from epoll_loop.c::dispatch(). */
void on_server_readable(struct uart_instance *inst, int epoll_fd);
void on_wire_readable(struct uart_instance *inst, int epoll_fd);
void on_client_readable(struct uart_instance *inst, int epoll_fd);

#endif /* VIRTRTLAB_INSTANCE_H */
