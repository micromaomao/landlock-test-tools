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
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
	ssize_t ntimes;
	int err;
	const char *path;

	if (argc != 4)
		return 1;

	ntimes = atoi(argv[1]);
	printf("ntimes: %ld\n", ntimes);
	if (ntimes <= 0)
		return 1;

	err = atoi(argv[2]);
	printf("expected errno: %ld\n", err);

	path = argv[3];
	printf("path: %s\n", path);

	for (size_t i = 0; i < ntimes; i++) {
		int fd = open(path, O_RDONLY);
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
		if (i % (ntimes / 10) == 0) {
			printf("i: %ld\n", i);
		}
	}
	return 0;
}
