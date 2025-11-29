// SPDX-License-Identifier: GPL-2.0
/*
 * open-ntimes <ntimes> <errno> <path>
 *
 * LL_FS_RO="/" LL_FS_RW="/" ./perf trace -s -e openat -- sandboxer ./open-ntimes 10000000 0 /mnt/1/2/3/4/5/6/7/8/9/
 *
 * Copyright © 2025 Mickaël Salaün <mic@digikod.net>.
 */

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* z-score for 99.9% confidence interval (two-tailed) */
#define Z_999 3.291

/* Threshold for outlier warning: if more than 5% of samples are outliers */
#define OUTLIER_THRESHOLD_PERCENT 5.0

static bool has_verbose(void)
{
	const char *verbose = getenv("VERBOSE");
	return verbose && strcmp(verbose, "0") != 0;
}

struct stats {
	uint64_t sum;
	uint64_t sum_of_squares;
	uint64_t min;
	uint64_t max;
	uint64_t count;
};

/*
 * Outlier detection state - computed after 1/3 of samples are collected.
 * Subsequent samples outside the 99.9% interval are counted as outliers.
 */
struct outlier_detection {
	bool initialized;
	double interval_low;
	double interval_high;
	uint64_t outlier_count;
	uint64_t samples_after_init;
};

static void add_stat(struct stats *stats, uint64_t value)
{
	stats->sum += value;
	stats->sum_of_squares += value * value;
	stats->count++;
	if (value < stats->min)
		stats->min = value;
	if (value > stats->max)
		stats->max = value;
}

/*
 * Compute mean and stddev from current stats.
 * Returns false if stats are invalid (count == 0).
 */
static bool compute_mean_stddev(const struct stats *stats, double *mean,
				double *stddev)
{
	double variance;

	if (stats->count == 0) {
		*mean = 0;
		*stddev = 0;
		return false;
	}

	*mean = (double)stats->sum / stats->count;
	/* var = E[X^2] - (E[X])^2 */
	variance = ((double)stats->sum_of_squares / stats->count) -
		   (*mean * *mean);
	/* Guard against negative variance from floating-point errors */
	if (variance < 0)
		variance = 0;
	*stddev = sqrt(variance);
	return true;
}

/*
 * Initialize outlier detection with 99.9% confidence interval based on
 * current statistics.
 * Returns false if stats are invalid.
 */
static bool init_outlier_detection(struct outlier_detection *od,
				   const struct stats *stats)
{
	double mean, stddev;

	if (!compute_mean_stddev(stats, &mean, &stddev))
		return false;

	od->interval_low = mean - Z_999 * stddev;
	od->interval_high = mean + Z_999 * stddev;
	od->initialized = true;
	od->outlier_count = 0;
	od->samples_after_init = 0;
	return true;
}

/*
 * Check if a sample is an outlier and update tracking.
 * Returns true if the sample is outside the 99.9% interval.
 */
static bool check_outlier(struct outlier_detection *od, uint64_t value)
{
	if (!od->initialized)
		return false;

	od->samples_after_init++;
	if ((double)value < od->interval_low ||
	    (double)value > od->interval_high) {
		od->outlier_count++;
		return true;
	}
	return false;
}

/*
 * Check if outlier percentage exceeds threshold and print warning if so.
 */
static void check_outlier_warning(const struct outlier_detection *od)
{
	double outlier_percent;

	if (!od->initialized || od->samples_after_init == 0)
		return;

	outlier_percent =
		(double)od->outlier_count / od->samples_after_init * 100.0;
	if (outlier_percent > OUTLIER_THRESHOLD_PERCENT) {
		fprintf(stderr,
			"[*] WARNING: %.1f%% of samples (%lu/%lu) are outliers "
			"(outside 99.9%% interval ( %.2f +/- %.2f ))\n",
			outlier_percent, od->outlier_count,
			od->samples_after_init,
			(od->interval_low + od->interval_high) / 2.0,
			(od->interval_high - od->interval_low) / 2.0);
	}
}

static void print_stats(const struct stats *stats, uint64_t ntimes)
{
	double mean = (double)stats->sum / ntimes;
	/* var = E[X^2] - (E[X])^2 */
	double variance =
		((double)stats->sum_of_squares / ntimes) - (mean * mean);
	double stddev = sqrt(variance);

	printf("{\"type\":\"cstats\",\"ntimes\":%lu,\"mean\":%.4f,\"stddev\":%.4f,"
	       "\"min\":%lu,\"max\":%lu,\"sum_of_squares\":%lu}\n",
	       ntimes, mean, stddev, stats->min, stats->max,
	       stats->sum_of_squares);
}

int main(int argc, char *argv[])
{
	ssize_t ntimes, prepare_ntimes, outlier_init_point;
	int err;
	const char *path;
	bool verbose = has_verbose();
	struct stats stats = {
		.sum = 0,
		.sum_of_squares = 0,
		.min = UINT64_MAX,
		.max = 0,
		.count = 0,
	};
	struct outlier_detection od = {
		.initialized = false,
		.interval_low = 0,
		.interval_high = 0,
		.outlier_count = 0,
		.samples_after_init = 0,
	};
	struct timespec test_start = {}, test_end = {};
	uint64_t nsecs_total;

	if (argc != 4)
		return 1;

	ntimes = atoi(argv[1]);
	if (verbose)
		printf("[#] ntimes: %ld\n", ntimes);
	if (ntimes <= 0)
		return 1;

	prepare_ntimes = ntimes / 5;
	if (verbose)
		printf("[#] running open for %ld times to warm up first.\n",
		       prepare_ntimes);

	/*
	 * Initialize outlier detection after collecting 1/3 of samples.
	 * Require at least 30 samples before initialization for statistical
	 * reliability. If ntimes is too small, outlier detection is skipped.
	 */
	if (ntimes >= 90)
		outlier_init_point = prepare_ntimes + (ntimes / 3);
	else
		outlier_init_point = -1; /* Skip outlier detection */

	err = atoi(argv[2]);
	if (verbose)
		printf("[#] expected errno: %d\n", err);

	path = argv[3];
	if (verbose)
		printf("[#] path: %s\n", path);

	for (size_t i = 0; i < prepare_ntimes + ntimes; i++) {
		struct timespec start, end;
		int fd;
		uint64_t ns_elapsed;

		if (clock_gettime(CLOCK_MONOTONIC, &start))
			perror("clock_gettime");
		fd = open(path, O_RDONLY);
		if (clock_gettime(CLOCK_MONOTONIC, &end))
			perror("clock_gettime");

		if (fd < 0) {
			if (err != errno) {
				perror("Unexpected error");
				return 1;
			}
		} else {
			if (err) {
				fprintf(stderr, "Unexpected success");
				return 1;
			}
			close(fd);
		}

		ns_elapsed = (end.tv_sec - start.tv_sec) * 1000000000ULL +
			     (end.tv_nsec - start.tv_nsec);

		if (verbose && i == prepare_ntimes) {
			if (clock_gettime(CLOCK_MONOTONIC, &test_start))
				perror("clock_gettime");
			printf("[#] Done warming up.\n");
		}
		if (i >= prepare_ntimes) {
			add_stat(&stats, ns_elapsed);

			/* Initialize outlier detection after 1/3 of samples */
			if (outlier_init_point >= 0 &&
			    (ssize_t)i == outlier_init_point) {
				if (init_outlier_detection(&od, &stats) &&
				    verbose) {
					printf("[#] Outlier detection initialized: "
					       "99.9%% interval [%.2f, %.2f]\n",
					       od.interval_low,
					       od.interval_high);
				}
			} else if (od.initialized) {
				check_outlier(&od, ns_elapsed);
			}
		}
	}

	/* Check and warn about outliers */
	check_outlier_warning(&od);

	if (verbose) {
		if (clock_gettime(CLOCK_MONOTONIC, &test_end))
			perror("clock_gettime");

		nsecs_total =
			(test_end.tv_sec - test_start.tv_sec) * 1000000000ULL +
			(test_end.tv_nsec - test_start.tv_nsec);

		printf("[#] Total time: %lu ns => %.4f avg\n", nsecs_total,
		       (double)nsecs_total / ntimes);
	}

	print_stats(&stats, ntimes);
	return 0;
}
