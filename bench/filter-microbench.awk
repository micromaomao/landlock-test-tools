#!/usr/bin/env -S awk -f
# SPDX-License-Identifier: GPL-2.0
#
# Copyright © 2025 Mickaël Salaün <mic@digikod.net>.

BEGIN {
	max=1000000
	exit_status = 0
}

$1 == "[*]" {
	print
}

# perf output:
#
# Summary of events:
#
# open-ntimes (3115), 199924 events, 100.0%
#
#   syscall            calls  errors  total       min       avg       max       stddev
#                                     (msec)    (msec)    (msec)    (msec)        (%)
#   --------------- --------  ------ -------- --------- --------- ---------     ------
#   openat             99968      3   765.950     0.005     0.008     0.065      0.09%

$1 == "openat" {
	if ($2 < max/2) { exit 2 } # calls
	if ($3 > 100) { exit 3 } # errors
	if ($8 >= 0.2) { exit 4 } # stddev
	print "=> avg: " $6 * 1000 " microseconds\n"
}
