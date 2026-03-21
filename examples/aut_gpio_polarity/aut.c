// SPDX-License-Identifier: GPL-2.0-only
/*
 * aut_gpio_polarity.c — GPIO edge-detection AUT with a polarity bug.
 *
 * Bug: the AUT requests a RISING edge on line 1 (GPIO_V2_LINE_FLAG_EDGE_RISING)
 * but the physical signal of interest transitions from high to low (falling).
 * Under normal test conditions the harness drives a rising edge and the AUT
 * works correctly, hiding the bug.  When the harness drives only a falling
 * edge (the real signal) the AUT never receives an event and times out.
 *
 * A correct implementation would request EDGE_RISING|EDGE_FALLING (both) and
 * filter the event.id field, or request EDGE_FALLING if descending is the
 * only relevant transition.
 *
 * Reads the gpiochip device path from VIRTRTLAB_GPIOCHIP0.
 * Requires Linux ≥ 5.10 (GPIO v2 character device API, CONFIG_GPIO_CDEV).
 *
 * Part of VirtRTLab — fault-injection example suite.
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/gpio.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define WATCH_LINE   1
#define POLL_TIMEOUT 3000 /* ms */

int main(void)
{
const char *chip = getenv("VIRTRTLAB_GPIOCHIP0");

if (!chip) {
fprintf(stderr, "error: VIRTRTLAB_GPIOCHIP0 is not set\n");
return 1;
}

int chip_fd = open(chip, O_RDONLY | O_CLOEXEC);

if (chip_fd < 0) {
fprintf(stderr, "open %s: %s\n", chip, strerror(errno));
return 1;
}

struct gpio_v2_line_request req = { 0 };

req.offsets[0] = WATCH_LINE;
req.num_lines  = 1;
strncpy(req.consumer, "aut_gpio_polarity", sizeof(req.consumer) - 1);

/*
 * BUG: only EDGE_RISING is requested.  The physical signal that
 * indicates "event occurred" is a falling transition (1 → 0).
 *
 * Fix: replace EDGE_RISING with
 *   GPIO_V2_LINE_FLAG_EDGE_FALLING
 * or subscribe to both edges and check event.id.
 */
req.config.flags = GPIO_V2_LINE_FLAG_INPUT |
   GPIO_V2_LINE_FLAG_EDGE_RISING; /* BUG */

if (ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, &req) < 0) {
fprintf(stderr, "GPIO_V2_GET_LINE_IOCTL: %s\n", strerror(errno));
close(chip_fd);
return 1;
}
close(chip_fd);

int line_fd = req.fd;
struct pollfd pfd = { .fd = line_fd, .events = POLLIN };
int ret = poll(&pfd, 1, POLL_TIMEOUT);

if (ret < 0) {
fprintf(stderr, "poll: %s\n", strerror(errno));
close(line_fd);
return 1;
}

if (ret == 0) {
fprintf(stderr, "TIMEOUT: no edge event on line %d\n", WATCH_LINE);
close(line_fd);
return 1; /* timed out — harness considers this a PASS in fault mode */
}

struct gpio_v2_line_event event = { 0 };

if (read(line_fd, &event, sizeof(event)) != (ssize_t)sizeof(event)) {
fprintf(stderr, "read event: %s\n", strerror(errno));
close(line_fd);
return 1;
}

const char *edge = (event.id == GPIO_V2_LINE_EVENT_RISING_EDGE) ?
   "rising" : "falling";

printf("OK edge event on line %u: %s\n", event.offset, edge);
close(line_fd);
return 0;
}
