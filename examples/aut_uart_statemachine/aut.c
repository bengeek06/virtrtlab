// SPDX-License-Identifier: GPL-2.0-only
/*
 * aut_uart_statemachine.c — Protocol state machine without out-of-order handling.
 *
 * Protocol: the simulator sends a 3-byte frame:
 *   [0x01 (SOF/cmd), 0x42 (data), cmd ^ data (checksum)]
 *
 * The AUT drives a state machine through three states:
 *   IDLE → GOT_CMD → GOT_DATA → done
 *
 * Bug: the state machine has no RESET path.  It reads blindly one byte per
 * state transition.  If an extra byte is injected between the data byte and
 * the checksum byte (simulating a retransmit or framing slip), the state
 * machine consumes the injected byte as the checksum, computes the wrong
 * expected value, reports a bad-state error and exits 2.
 *
 * Under normal operation the simulator sends exactly [0x01, 0x42, 0x43] and
 * the AUT exits 0.
 *
 * A correct implementation would either:
 *   (a) use a length-prefixed or delimiter-framed protocol, or
 *   (b) have an explicit RESET state that re-synchronises on SOF after any
 *       unexpected byte.
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

#define SOF  0x01u
#define DATA 0x42u

enum sm_state { IDLE, GOT_CMD, GOT_DATA };

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
 * Read one byte at a time; VTIME=30 (3 s inactivity timeout) prevents
 * a permanent hang if the simulator disconnects unexpectedly.
 */
tio.c_cc[VMIN]  = 1;
tio.c_cc[VTIME] = 30;

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

enum sm_state st = IDLE;
unsigned char cmd = 0, data = 0;

while (1) {
unsigned char b;
ssize_t n = read(fd, &b, 1);

if (n == 0) {
fprintf(stderr, "TIMEOUT: no data received\n");
close(fd);
return 1;
}
if (n < 0) {
fprintf(stderr, "read: %s\n", strerror(errno));
close(fd);
return 1;
}

switch (st) {
case IDLE:
/*
 * Wait for SOF.  Unexpected bytes are silently ignored
 * here — this is intentional resync in the first state.
 */
if (b == SOF) {
cmd = b;
st = GOT_CMD;
}
break;

case GOT_CMD:
data = b;
st = GOT_DATA;
break;

case GOT_DATA: {
/*
 * BUG: the next byte is blindly treated as checksum.
 * If an extra byte was injected by the harness before
 * the real checksum, this check fails and the state
 * machine has no way to recover (no RESET state).
 */
unsigned char expected = cmd ^ data;

if (b != expected) {
fprintf(stderr,
"BAD STATE: checksum mismatch "
"got 0x%02x expected 0x%02x\n",
b, expected);
close(fd);
return 2; /* wrong state — unrecoverable */
}
printf("OK cmd=0x%02x data=0x%02x checksum=0x%02x\n",
       cmd, data, b);
close(fd);
return 0;
}
} /* switch */
}
}
