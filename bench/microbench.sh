#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# cd linux
# ARCH=x86_64 .../check-linux.sh build_light
# # Run a VM with this new kernel
# .../microbench.sh vm0 | .../filter-microbench.awk
#
# Copyright © 2025 Mickaël Salaün <mic@digikod.net>.
# Copyright © 2025 Tingmao Wang <m@maowtm.org>.

set -e -u -o pipefail

# In sync with to filter-microbench.awk
NUM_ITERATIONS="1000000"

DIRNAME="$(dirname -- "${BASH_SOURCE[0]}")"
BASENAME="$(basename -- "${BASH_SOURCE[0]}")"

TRACE_OVERHEAD_LOG_FILE="${TRACE_OVERHEAD_LOG_FILE-}"
RUN_ON_CPU="${RUN_ON_CPU-}"

if [[ $# -gt 1 ]]; then
	echo "usage: [[TRACE_OVERHEAD_LOG_FILE=<file_name>] RUN_ON_CPU=<nb>] ${BASENAME} [ssh-host] | .../filter-microbench.awk" >&2
	exit 1
fi

if [[ $TRACE_OVERHEAD_LOG_FILE != "" && $RUN_ON_CPU == "" ]]; then
	echo "Must set RUN_ON_CPU as well to trace overhead." >&2
	exit 1
fi

if [ -e "$TRACE_OVERHEAD_LOG_FILE" ]; then
	echo "$TRACE_OVERHEAD_LOG_FILE already exists, you might want to provide a new name." >&2
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
	local nb_extra_rules="$2"
	local sandboxer="${3:-}"
	local maybe_taskset=()
	if [[ -n "${RUN_ON_CPU}" ]]; then
		maybe_taskset=(taskset -c "${RUN_ON_CPU}")
	fi
	local cmd=(
		env LL_FS_RO=/ LL_FS_RW=/ IN_BENCHMARK_NS=1 NB_EXTRA_RULES=$nb_extra_rules unshare --mount --
		./run-bench-in-namespace.sh ./perf trace -s -e openat --
		${sandboxer} ${maybe_taskset[@]} ./open-ntimes "${NUM_ITERATIONS}" 0 "$d"
	)

	if [[ -n "${sandboxer}" ]]; then
		echo -n "[*] with sandbox"
	else
		echo -n "[*] without sandbox"
	fi
	echo -n " d=$d"
	echo " nb_extra_rules=$nb_extra_rules"

	if [[ -n "${SSH_HOST}" ]]; then
		echo "[+] ssh ${SSH_HOST} ${cmd[*]}"
		ssh "${SSH_HOST}" -- "${cmd[@]}"
	else
		echo "[+] ${cmd[*]}"
		"${cmd[@]}"
	fi
}

for d in / /1/2/3/4/5/6/7/8/9/ /1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9 /1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9; do
	for nb_extra_rules in 0 100 1000 10000; do
		if [[ $TRACE_OVERHEAD_LOG_FILE != "" ]]; then
			echo "[*] d = $d nb_extra_rules = $nb_extra_rules" >> "$TRACE_OVERHEAD_LOG_FILE"
			bpftrace landlock_overhead.bt $RUN_ON_CPU >> "$TRACE_OVERHEAD_LOG_FILE" &
			bpftrace_pid=$!
		fi
		run_test "$d" $nb_extra_rules 2>&1
		run_test "$d" $nb_extra_rules ./sandboxer 2>&1
		if [[ $TRACE_OVERHEAD_LOG_FILE != "" ]]; then
			kill -INT $bpftrace_pid
			wait $bpftrace_pid
		fi
	done
done
