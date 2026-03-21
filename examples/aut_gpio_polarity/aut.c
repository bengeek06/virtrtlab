// SPDX-License-Identifier: GPL-2.0-only
/*
 * aut_gpio_polarity.c — GPIO polling AUT with an active-low/active-high bug.
 *
 * The AUT monitors a GPIO line that indicates "device ready" in an
 * active-HIGH fashion: the signal is HIGH (1) when the device is ready.
 *
 * Bug: the AUT uses active-LOW logic — it interprets a LOW level (0) as
 *      "asserted / ready" and a HIGH level (1) as "de-asserted / not ready".
 *
 *   Baseline: the harness pre-sets the line LOW (0).  The AUT's buggy check
 *             (val == 0 → ready) fires immediately and exits 0.  The test
 *             appears to pass even though the logic is wrong.
 *
 *   Fault:    the harness pre-sets the line HIGH (1) — the actual "ready"
 *             level that a correct AUT would detect.  The AUT's buggy check
 *             never fires (val == 1 ≠ 0) and it times out (exit 1), exposing
 *             the polarity bug.
 *
 * Fix: change the condition to  if (val == 1)
 *
 * Reads the gpiochip device path from VIRTRTLAB_GPIOCHIP0.
 * Requires Linux ≥ 5.10 (GPIO v2 character device API, CONFIG_GPIO_CDEV).
 *
 * Part of VirtRTLab — fault-injection example suite.
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/gpio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define WATCH_LINE       1
#define POLL_INTERVAL_MS 100
#define TIMEOUT_MS       3000

static long elapsed_ms(const struct timespec *start)
{
	struct timespec now;

	clock_gettime(CLOCK_MONOTONIC, &now);
	return (now.tv_sec - start->tv_sec) * 1000L +
	       (now.tv_nsec - start->tv_nsec) / 1000000L;
}

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

	req.offsets[0]   = WATCH_LINE;
	req.num_lines    = 1;
	req.config.flags = GPIO_V2_LINE_FLAG_INPUT;
	strncpy(req.consumer, "aut_gpio_polarity", sizeof(req.consumer) - 1);

	if (ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, &req) < 0) {
		fprintf(stderr, "GPIO_V2_GET_LINE_IOCTL: %s\n", strerror(errno));
		close(chip_fd);
		return 1;
	}
	close(chip_fd);

	int line_fd = req.fd;
	struct timespec start;

	clock_gettime(CLOCK_MONOTONIC, &start);

	while (elapsed_ms(&start) < TIMEOUT_MS) {
		struct gpio_v2_line_values vals = { .mask = 1 };

		if (ioctl(line_fd, GPIO_V2_LINE_GET_VALUES_IOCTL, &vals) < 0) {
			fprintf(stderr, "GPIO_V2_LINE_GET_VALUES_IOCTL: %s\n",
				strerror(errno));
			close(line_fd);
			return 1;
		}

		int val = (int)(vals.bits & 1);

		/*
		 * BUG: active-LOW logic when the signal is active-HIGH.
		 *
		 * The line is HIGH (1) when the device is ready.  A correct
		 * implementation would check  if (val == 1).
		 * This code checks  if (val == 0) — it will only detect the
		 * "ready" condition if the signal is LOW, which is wrong.
		 */
		if (val == 0) { /* BUG: should be == 1 */
			printf("OK device ready on line %d (val=%d)\n",
			       WATCH_LINE, val);
			close(line_fd);
			return 0;
		}

		struct timespec ts = { 0, POLL_INTERVAL_MS * 1000000L };

		nanosleep(&ts, NULL);
	}

	fprintf(stderr, "TIMEOUT: device never signalled ready on line %d\n",
		WATCH_LINE);
	close(line_fd);
	return 1;
}

