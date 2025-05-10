#!/usr/bin/env -S awk -f
# SPDX-License-Identifier: GPL-2.0
#
# Copyright © 2025 Tingmao Wang <m@maowtm.org>.

$1 == "[*]" {
	in_output = 0
	print ""
	print
}

BEGIN {
	in_output = 0
}

/^@/ {
	in_output = 1
}

NF == 0 {
	in_output = 0
}

in_output {
	print
}
