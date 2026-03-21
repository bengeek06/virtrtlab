/* SPDX-License-Identifier: MIT */
/*
 * main.c — virtrtlabd startup, argument parsing, and lifecycle
 *
 * Usage:
 *   virtrtlabd [--num-uarts N] [--run-dir DIR]
 *
 * Options:
 *   --num-uarts N    Number of UART instances to relay (1..8, default 1).
 *                    Must match the num_uarts parameter of the loaded
 *                    virtrtlab_uart kernel module.
 *   --run-dir DIR    Directory for AF_UNIX sockets (default /run/virtrtlab).
 *   --help           Print usage and exit.
 *
 * Exit codes:
 *   0   Clean shutdown via SIGTERM or SIGINT.
 *   1   Startup failure (wire device missing, bad arguments, …).
 */

#define _GNU_SOURCE

#include <errno.h>
#include <getopt.h>
#include <grp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "epoll_loop.h"
#include "instance.h"

#define MAX_UARTS      8
#define DEFAULT_UARTS  1
#define DEFAULT_RUNDIR "/run/virtrtlab"

/* ---- usage --------------------------------------------------------------- */

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s [OPTIONS]\n"
		"\n"
		"Options:\n"
		"  --num-uarts N    UART instances to relay (1..%d, default %d)\n"
		"  --run-dir DIR    Socket directory (default %s)\n"
		"  --help           Show this help\n",
		prog, MAX_UARTS, DEFAULT_UARTS, DEFAULT_RUNDIR);
}

/* ---- argument parsing ---------------------------------------------------- */

static struct option longopts[] = {
	{ "num-uarts", required_argument, NULL, 'n' },
	{ "run-dir",   required_argument, NULL, 'd' },
	{ "help",      no_argument,       NULL, 'h' },
	{ NULL,        0,                 NULL,  0  },
};

static int parse_args(int argc, char *argv[],
		      int *num_uarts, const char **run_dir)
{
	int   opt;
	int   n;
	char *endptr;

	*num_uarts = DEFAULT_UARTS;
	*run_dir   = DEFAULT_RUNDIR;

	while ((opt = getopt_long(argc, argv, "n:d:h", longopts, NULL)) != -1) {
		switch (opt) {
		case 'n':
			n = (int)strtol(optarg, &endptr, 10);
			if (*endptr != '\0' || endptr == optarg ||
			    n < 1 || n > MAX_UARTS) {
				fprintf(stderr,
					"virtrtlabd: --num-uarts must be 1..%d\n",
					MAX_UARTS);
				return -1;
			}
			*num_uarts = n;
			break;
		case 'd':
			*run_dir = optarg;
			break;
		case 'h':
			usage(argv[0]);
			exit(EXIT_SUCCESS);
		default:
			usage(argv[0]);
			return -1;
		}
	}

	return 0;
}

/* ---- run directory ------------------------------------------------------- */

/*
 * mkdir_rundir - create the socket directory if it does not exist.
 *
 * Mode 0755: non-root simulators must be able to traverse the directory
 * and connect to the AF_UNIX sockets inside it.
 *
 * On EEXIST, stat() the path to ensure it is actually a directory; a
 * regular file or symlink at that path would cause confusing failures
 * later when bind() tries to create sockets there.
 */
static int mkdir_rundir(const char *path)
{
	struct stat st;

	if (mkdir(path, 0755) < 0) {
		if (errno != EEXIST) {
			syslog(LOG_ERR, "mkdir %s: %m", path);
			return -1;
		}
		/* Path already exists — verify it is a directory. */
		if (stat(path, &st) < 0) {
			syslog(LOG_ERR, "stat %s: %m", path);
			return -1;
		}
		if (!S_ISDIR(st.st_mode)) {
			syslog(LOG_ERR, "%s exists but is not a directory", path);
			return -1;
		}
	}
	return 0;
}

/* ---- signal setup -------------------------------------------------------- */

/*
 * setup_signalfd - block SIGTERM and SIGINT from async delivery and return a
 * signalfd that becomes readable when either signal arrives.
 *
 * The returned fd must be registered in epoll with a ROLE_SIGNAL evt_ctx.
 */
static int setup_signalfd(void)
{
	sigset_t mask;
	int      fd;

	sigemptyset(&mask);
	sigaddset(&mask, SIGTERM);
	sigaddset(&mask, SIGINT);

	if (sigprocmask(SIG_BLOCK, &mask, NULL) < 0) {
		syslog(LOG_ERR, "sigprocmask: %m");
		return -1;
	}

	fd = signalfd(-1, &mask, SFD_CLOEXEC);
	if (fd < 0) {
		syslog(LOG_ERR, "signalfd: %m");
		return -1;
	}

	return fd;
}

/* ---- main ---------------------------------------------------------------- */

int main(int argc, char *argv[])
{
	struct uart_instance instances[MAX_UARTS];
	struct evt_ctx       ctx_signal;
	struct group        *gr;
	gid_t                sock_gid;
	int                  num_uarts;
	const char          *run_dir;
	int                  epoll_fd;
	int                  sig_fd;
	int                  i;

	if (parse_args(argc, argv, &num_uarts, &run_dir) < 0)
		return EXIT_FAILURE;

	openlog("virtrtlabd", LOG_PID, LOG_DAEMON);
	syslog(LOG_INFO, "starting: num_uarts=%d run_dir=%s", num_uarts, run_dir);

	/*
	 * Ignore SIGPIPE globally: write/send to a closed socket returns
	 * EPIPE instead of killing the process.  Handlers check errno.
	 */
	signal(SIGPIPE, SIG_IGN);

	/*
	 * Resolve the virtrtlab group ID once at startup so each socket can
	 * be fchown'd to root:virtrtlab after bind().
	 * If the group does not exist, sockets are created root:root and the
	 * daemon logs a warning — non-root connections will be refused until
	 * the group is created and the daemon is restarted.
	 */
	gr = getgrnam("virtrtlab");
	if (!gr) {
		syslog(LOG_WARNING,
		       "group 'virtrtlab' not found — "
		       "sockets will be root:root 0660");
		sock_gid = (gid_t)-1;
	} else {
		sock_gid = gr->gr_gid;
	}

	if (mkdir_rundir(run_dir) < 0) {
		closelog();
		return EXIT_FAILURE;
	}

	sig_fd = setup_signalfd();
	if (sig_fd < 0) {
		closelog();
		return EXIT_FAILURE;
	}

	epoll_fd = epoll_loop_create();

	/*
	 * Register the signalfd in epoll.
	 * ctx_signal.inst is reused to transport sig_fd to on_signal().
	 * The cast through intptr_t avoids a pointer-size warning on 64-bit.
	 */
	ctx_signal.inst = (struct uart_instance *)(intptr_t)sig_fd;
	ctx_signal.role = ROLE_SIGNAL;
	epoll_loop_add(epoll_fd, sig_fd, EPOLLIN, &ctx_signal);

	/*
	 * Initialise each UART instance.  If any wire device is missing
	 * (e.g. virtrtlab_uart not loaded), fail fast with a clear message
	 * and clean up already-initialised instances (AC5).
	 */
	for (i = 0; i < num_uarts; i++) {
		if (uart_instance_init(&instances[i], i, epoll_fd, run_dir, sock_gid) < 0) {
			syslog(LOG_ERR,
				"failed to initialise uart%d — "
				"is virtrtlab_uart loaded with num_uarts>=%d?",
				i, i + 1);
			/* Destroy already-initialised instances in reverse order. */
			while (--i >= 0)
				uart_instance_destroy(&instances[i], epoll_fd);
			if (close(sig_fd) < 0)
				syslog(LOG_WARNING, "failed to close signalfd: %m");
			if (close(epoll_fd) < 0)
				syslog(LOG_WARNING, "failed to close epoll fd: %m");
			closelog();
			return EXIT_FAILURE;
		}
	}

	epoll_loop_set_instances(instances, num_uarts);

	/* Never returns under normal operation; exits via on_signal(). */
	epoll_loop_run(epoll_fd);

	/* Unreachable, but satisfy the compiler. */
	return EXIT_SUCCESS;
}
