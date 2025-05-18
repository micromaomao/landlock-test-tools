#!/usr/bin/env -S awk -f
# SPDX-License-Identifier: GPL-2.0
#
# Copyright © 2025 Tingmao Wang <m@maowtm.org>.

BEGIN {
	in_output = 0
	show_hist = 1
}

$1 == "[*]" {
	print ""
}

$1 == "[*]" || /^=>/  {
	in_output = 0
	print
}

/^@/ {
	in_output = 1
}

NF == 0 {
	in_output = 0
}

in_output && show_hist {
	print
}
