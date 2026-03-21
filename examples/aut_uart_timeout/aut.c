// SPDX-License-Identifier: GPL-2.0-only
/*
 * aut_uart_timeout.c — UART reader with a missing read() timeout.
 *
 * Bug: termios is configured with VMIN=4, VTIME=0.  The read() call blocks
 * forever if fewer than 4 bytes arrive.  A correct implementation would set
 * VTIME to a finite value, or use poll()/select() with a timeout before
 * calling read().
 *
 * Under normal operation the companion harness (acting as the simulator)
 * sends exactly 4 bytes, so the bug is invisible.  When the harness drops
 * the last byte the AUT hangs forever and must be killed by an external
 * watchdog.
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
 * BUG: VMIN=MSG_LEN forces read() to block until exactly MSG_LEN
 * bytes arrive.  VTIME=0 disables the inter-character timer, so no
 * timeout will ever fire — the process hangs if one byte is missing.
 */
tio.c_cc[VMIN]  = MSG_LEN;
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
 * Blocking read — hangs if fewer than MSG_LEN bytes arrive because
 * VTIME=0 disables any inter-character timeout.
 */
unsigned char buf[MSG_LEN];
ssize_t n = read(fd, buf, MSG_LEN);

if (n < 0) {
fprintf(stderr, "read: %s\n", strerror(errno));
close(fd);
return 1;
}

/* Validate: buf[3] must equal buf[0]^buf[1]^buf[2]. */
unsigned char cksum = buf[0] ^ buf[1] ^ buf[2];

if (buf[3] != cksum) {
fprintf(stderr, "checksum error: got 0x%02x expected 0x%02x\n",
buf[3], cksum);
close(fd);
return 2;
}

printf("OK received %zd bytes, checksum 0x%02x\n", n, cksum);
close(fd);
return 0;
}
