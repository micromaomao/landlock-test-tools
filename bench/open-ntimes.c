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

/* Number of samples to use for outlier calibration */
#define OUTLIER_CALIBRATION_SAMPLES 50

/* Threshold multiplier: samples exceeding this multiple of the calibration average are outliers */
#define OUTLIER_THRESHOLD_MULTIPLIER 10

/* Threshold for outlier warning: if more than 5% of samples are outliers */
#define OUTLIER_THRESHOLD_PERCENT 5.0

/* Histogram configuration (in nanoseconds) */
#define HIST_MIN 100
#define HIST_STEP 100
#define HIST_MAX 2000

/* Number of histogram buckets */
#define HIST_BUCKETS (((HIST_MAX) - (HIST_MIN)) / (HIST_STEP))

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
 * Outlier detection state - computed after OUTLIER_CALIBRATION_SAMPLES samples.
 * Subsequent samples exceeding OUTLIER_THRESHOLD_MULTIPLIER times the
 * calibration average are counted as outliers and excluded from statistics.
 */
struct outlier_detection {
	bool initialized;
	double calibration_avg;
	double threshold;
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
 * Compute mean from current stats.
 * Returns false if stats are invalid (count == 0).
 */
static bool compute_mean(const struct stats *stats, double *mean)
{
	if (stats->count == 0) {
		*mean = 0;
		return false;
	}

	*mean = (double)stats->sum / stats->count;
	return true;
}

/*
 * Initialize outlier detection based on average from calibration samples.
 * Sets threshold to OUTLIER_THRESHOLD_MULTIPLIER times the calibration average.
 * Returns false if stats are invalid.
 */
static bool init_outlier_detection(struct outlier_detection *od,
				   const struct stats *stats)
{
	double mean;

	if (!compute_mean(stats, &mean))
		return false;

	od->calibration_avg = mean;
	od->threshold = mean * OUTLIER_THRESHOLD_MULTIPLIER;
	od->initialized = true;
	od->outlier_count = 0;
	od->samples_after_init = 0;
	return true;
}

/*
 * Check if a sample is an outlier and update tracking.
 * Returns true if the sample exceeds the threshold.
 */
static bool check_outlier(struct outlier_detection *od, uint64_t value)
{
	if (!od->initialized)
		return false;

	od->samples_after_init++;
	if ((double)value > od->threshold) {
		od->outlier_count++;
		return true;
	}
	return false;
}

/*
 * Check if outlier percentage exceeds threshold and print warning if so.
 * Warning is printed to stdout so it's included in log files.
 */
static void check_outlier_warning(const struct outlier_detection *od)
{
	double outlier_percent;

	if (!od->initialized || od->samples_after_init == 0)
		return;

	outlier_percent =
		(double)od->outlier_count / od->samples_after_init * 100.0;
	if (outlier_percent > OUTLIER_THRESHOLD_PERCENT) {
		printf("[*] WARNING: %.1f%% of samples (%lu/%lu) are outliers "
		       "(exceeding %.2f, which is %dx calibration avg %.2f)\n",
		       outlier_percent, od->outlier_count,
		       od->samples_after_init, od->threshold,
		       OUTLIER_THRESHOLD_MULTIPLIER, od->calibration_avg);
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

/*
 * Get the histogram bucket index for a given value.
 * Returns -1 if value is below HIST_MIN or above HIST_MAX.
 */
static int get_hist_bucket(uint64_t value)
{
	if (value < HIST_MIN || value >= HIST_MAX)
		return -1;
	return (value - HIST_MIN) / HIST_STEP;
}

/*
 * Add a sample to the histogram.
 */
static void add_to_histogram(uint64_t *histogram, uint64_t value)
{
	int bucket = get_hist_bucket(value);
	if (bucket >= 0 && bucket < HIST_BUCKETS)
		histogram[bucket]++;
}

/*
 * Print the histogram in JSON format.
 */
static void print_histogram(const uint64_t *histogram)
{
	printf("{\"type\":\"chist\",\"buckets\":[");
	for (int i = 0; i < HIST_BUCKETS; i++) {
		if (i > 0)
			printf(",");
		printf("{\"min\":%d,\"max\":%d,\"count\":%lu}",
		       HIST_MIN + i * HIST_STEP,
		       HIST_MIN + (i + 1) * HIST_STEP - 1,
		       histogram[i]);
	}
	printf("]}\n");
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
		.calibration_avg = 0,
		.threshold = 0,
		.outlier_count = 0,
		.samples_after_init = 0,
	};
	struct timespec test_start = {}, test_end = {};
	uint64_t nsecs_total;
	uint64_t *histogram;

	if (argc != 4)
		return 1;

	/* Allocate histogram array */
	histogram = calloc(HIST_BUCKETS, sizeof(uint64_t));
	if (!histogram) {
		perror("Failed to allocate histogram");
		return 1;
	}

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
	 * Initialize outlier detection after collecting OUTLIER_CALIBRATION_SAMPLES.
	 * If ntimes is too small, outlier detection is skipped.
	 */
	if (ntimes >= OUTLIER_CALIBRATION_SAMPLES)
		outlier_init_point = prepare_ntimes + OUTLIER_CALIBRATION_SAMPLES;
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
			/* Always add to histogram */
			add_to_histogram(histogram, ns_elapsed);

			/* Initialize outlier detection after calibration samples */
			if (outlier_init_point >= 0 &&
			    (ssize_t)i == outlier_init_point) {
				if (init_outlier_detection(&od, &stats) &&
				    verbose) {
					printf("[#] Outlier detection initialized: "
					       "calibration avg=%.2f, threshold=%.2f\n",
					       od.calibration_avg,
					       od.threshold);
				}
				/* Also add this sample to stats */
				add_stat(&stats, ns_elapsed);
			} else if (od.initialized) {
				/* After calibration: only add to stats if not an outlier */
				if (!check_outlier(&od, ns_elapsed))
					add_stat(&stats, ns_elapsed);
			} else {
				/* During calibration: always add to stats */
				add_stat(&stats, ns_elapsed);
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

	print_stats(&stats, stats.count);
	print_histogram(histogram);
	free(histogram);
	return 0;
}
