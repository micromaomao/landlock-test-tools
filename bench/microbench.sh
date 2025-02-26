#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# cd linux
# ARCH=x86_64 .../check-linux.sh build_light
# # Run a VM with this new kernel
# .../microbench.sh vm0 | .../filter-microbench.awk
#
# Copyright © 2025 Mickaël Salaün <mic@digikod.net>.

set -e -u -o pipefail

# In sync with to filter-microbench.awk
NUM_ITERATIONS="1000000"

DIRNAME="$(dirname -- "${BASH_SOURCE[0]}")"
BASENAME="$(basename -- "${BASH_SOURCE[0]}")"

if [[ $# -gt 1 ]]; then
	echo "usage: ${BASENAME} [ssh-host] | .../filter-microbench.awk" >&2
	exit 1
fi

SSH_HOST="${1:-}"

BUILD_DIR=".out-landlock_local-x86_64-gcc"

get_file() {
	local path="$1"
	local basename="./$(basename -- "${path}")"
	shift
	local ret

	if [[ -e "${basename}" ]]; then
		path="${basename}"
	elif [[ ! -e "${path}" ]]; then
		"$@"
		if [[ ! -e "${path}" ]]; then
			echo "ERROR: Missing file: ${path}" >&2
			return 1
		fi
	fi

	if [[ -n "${SSH_HOST}" ]]; then
		scp "${path}" "${SSH_HOST}:"
	elif [[ "${path}" != "${basename}" ]]; then
		cp -v "${path}" .
	fi
}

if [[ -n "${SSH_HOST}" ]]; then
	echo "[*] Installing required dependencies"
	ssh "${SSH_HOST}" "pacman --noconfirm -Sy llvm-libs capstone perl python"
fi

get_file "${DIRNAME}/open-ntimes" make -C "${DIRNAME}"
get_file "${DIRNAME}/run-bench-in-namespace.sh"
get_file "${BUILD_DIR}/samples/landlock/sandboxer"
get_file "tools/perf/perf" make -C "tools/perf"

run_test() {
	local d="$1"
	local sandboxer="${2:-}"
	local cmd=(env LL_FS_RO=/ LL_FS_RW=/ IN_BENCHMARK_NS=1 unshare --mount -- ./run-bench-in-namespace.sh ./perf trace -s -e openat -- ${sandboxer} ./open-ntimes "${NUM_ITERATIONS}" 0 "$d")

	if [[ -n "${sandboxer}" ]]; then
		echo -n "[*] with sandbox"
	else
		echo -n "[*] without sandbox"
	fi
	echo " d=$d"

	if [[ -n "${SSH_HOST}" ]]; then
		echo "[+] ssh ${SSH_HOST} ${cmd[*]}"
		ssh "${SSH_HOST}" -- "${cmd[@]}"
	else
		echo "[+] ${cmd[*]}"
		"${cmd[@]}"
	fi
}

for d in / /1/2/3/4/5/6/7/8/9/ /1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9 /1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9; do
	run_test "$d" 2>&1
	run_test "$d" ./sandboxer 2>&1
done
