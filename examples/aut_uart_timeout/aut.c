// SPDX-License-Identifier: GPL-2.0-only
/*
 * aut_uart_timeout.c — UART reader with a missing per-byte timeout.
 *
 * Bug: the AUT reads MSG_LEN bytes one at a time in a blocking loop with
 * VMIN=1, VTIME=0 (block until at least 1 byte is available, no character
 * timer).  If one byte of the frame never arrives the AUT hangs forever
 * inside read(), with no way to detect or recover from the loss.
 *
 * A correct implementation would call poll()/select() with a reasonable
 * inter-byte timeout (e.g. 500 ms) before each read(), or set VTIME to a
 * non-zero value to engage the inactivity timer.
 *
 * Under normal operation the companion harness (acting as the simulator)
 * sends all MSG_LEN bytes, so the bug is invisible.  When the harness drops
 * the last byte the AUT blocks forever on the final read() and must be
 * killed by an external watchdog.
 *
 * Part of VirtRTLab — fault-injection example suite.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

#define MSG_LEN 4

int main(void)
{
const char *dev = getenv("VIRTRTLAB_UART0");

if (!dev) {
fprintf(stderr, "error: VIRTRTLAB_UART0 is not set\n");
return 1;
}

int fd = open(dev, O_RDWR | O_NOCTTY);

if (fd < 0) {
fprintf(stderr, "open %s: %s\n", dev, strerror(errno));
return 1;
}

struct termios tio;

if (tcgetattr(fd, &tio) < 0) {
fprintf(stderr, "tcgetattr: %s\n", strerror(errno));
close(fd);
return 1;
}

cfmakeraw(&tio);
cfsetispeed(&tio, B9600);
cfsetospeed(&tio, B9600);

/*
 * VMIN=1: return as soon as 1 byte is available.
 * VTIME=0: no inactivity timer — read() blocks indefinitely if no
 *           byte arrives.
 *
 * BUG: there is no inter-byte timeout.  A correct implementation would
 * use poll()/select() before each read(), or set VTIME > 0 to let the
 * kernel deliver a timeout after a short idle period.
 */
tio.c_cc[VMIN]  = 1;
tio.c_cc[VTIME] = 0;

if (tcsetattr(fd, TCSANOW, &tio) < 0) {
fprintf(stderr, "tcsetattr: %s\n", strerror(errno));
close(fd);
return 1;
}

/* Signal readiness to the simulator. */
unsigned char ready = 0x55;

if (write(fd, &ready, 1) != 1) {
fprintf(stderr, "write ready: %s\n", strerror(errno));
close(fd);
return 1;
}

/*
 * Read MSG_LEN bytes one at a time.
 *
 * BUG: each read() blocks indefinitely (VTIME=0).  If the last byte
 * never arrives (e.g. dropped by the simulator), the AUT hangs here
 * with no timeout and no indication that something went wrong.
 */
unsigned char buf[MSG_LEN];

for (int i = 0; i < MSG_LEN; i++) {
ssize_t n = read(fd, &buf[i], 1);

if (n < 0) {
fprintf(stderr, "read byte %d: %s\n", i, strerror(errno));
close(fd);
return 1;
}
if (n == 0) {
fprintf(stderr, "read byte %d: EOF\n", i);
close(fd);
return 1;
}
}

/* Validate: buf[3] must equal buf[0]^buf[1]^buf[2]. */
unsigned char cksum = buf[0] ^ buf[1] ^ buf[2];

if (buf[3] != cksum) {
fprintf(stderr, "checksum error: got 0x%02x expected 0x%02x\n",
buf[3], cksum);
close(fd);
return 2;
}

printf("OK received %d bytes, checksum 0x%02x\n", MSG_LEN, cksum);
close(fd);
return 0;
}
