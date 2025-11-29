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

NUM_ITERATIONS="500000"

DIRNAME="$(dirname -- "${BASH_SOURCE[0]}")"
BASENAME="$(basename -- "${BASH_SOURCE[0]}")"

RUN_ON_CPU=""
DO_BPFTRACE=0
SSH_HOST=""
VERBOSE=0
NB_TRIALS=5
PERF_TRACE_OPENAT=0

print_usage() {
	echo "Usage: ${BASENAME} [--cpu <cpu_number>] [--bpftrace] [--ssh <host>]"
	echo "  --cpu <cpu_number>   Run the benchmark on the specified CPU core."
	echo "                       (default: 0)"
	echo "  --bpftrace           Use bpftrace to measure Landlock overhead."
	echo "  --ssh <host>         Use SSH to run the benchmark on the specified host."
	echo "  --verbose            Enable verbose output."
	echo "  --trials             <number>"
	echo "      Number of trials for each test size (default: ${NB_TRIALS})."
	echo "      This is probably only necessary for hashtable benchmarks."
	echo "  --perf-trace-openat"
	echo "      Use perf trace to measure openat syscall."
	echo "      Tends to slow things down.  Mutually exclusive with --bpftrace."
	exit 1
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--cpu)
			shift
			if [[ $# -eq 0 ]]; then
				print_usage
			fi
			RUN_ON_CPU="$1"
			shift
			;;
		--bpftrace)
			DO_BPFTRACE=1
			shift
			;;
		--ssh)
			shift
			if [[ $# -eq 0 ]]; then
				print_usage
			fi
			SSH_HOST="$1"
			shift
			;;
		--verbose)
			VERBOSE=1
			shift
			;;
		--trials)
			shift
			if [[ $# -eq 0 ]]; then
				print_usage
			fi
			NB_TRIALS="$1"
			shift
			;;
		--perf-trace-openat)
			PERF_TRACE_OPENAT=1
			shift
			;;
		*)
			print_usage
			;;
	esac
done

print_verbose() {
	if [[ $VERBOSE -eq 1 ]]; then
		echo "[#]" "$@" >&2
	fi
}

if [[ -z "$RUN_ON_CPU" ]]; then
	RUN_ON_CPU=0
fi

if [[ $DO_BPFTRACE -eq 1 && $PERF_TRACE_OPENAT -eq 1 ]]; then
	echo "ERROR: Can only do one of --bpftrace and --perf-trace-openat" >&2
	exit 1
fi

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

print_test_setup_json() {
	local do_landlock="$1"
	local dir_depth="$2"
	local nb_extra_rules="$3"

	jq -nc \
		--arg do_landlock "$do_landlock" \
		--arg dir_depth "$dir_depth" \
		--arg nb_extra_rules "$nb_extra_rules" \
		'{
			landlock: ($do_landlock == "1"),
			dir_depth: ($dir_depth | tonumber),
			nb_extra_rules: ($nb_extra_rules | tonumber)
		}'
}

run_test() {
	local do_landlock="$1"
	local dir_depth="$2"
	local nb_extra_rules="$3"
	local maybe_sandboxer=()
	if [[ $do_landlock -eq 1 ]]; then
		maybe_sandboxer=("./sandboxer")
	fi
	local d="$(echo /1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9/0/1/2/3/4/5/6/7/8/9 | head -c $((dir_depth * 2 + 1)))"
	local LL_FS_RO=/
	local LL_FS_RW=/
	for i in $(seq 1 $nb_extra_rules); do
		LL_FS_RO+=":/extra_rules/_$i"
	done
	local expected_errno=0
	local maybe_perf=()
	if [[ $PERF_TRACE_OPENAT -eq 1 ]]; then
		maybe_perf=(perf trace -C "${RUN_ON_CPU}" -s -e openat --)
	fi
	local maybe_ssh=()
	if [[ -n "${SSH_HOST}" ]]; then
		maybe_ssh=(ssh "$SSH_HOST" --)
	fi
	local cmd=(
		"${maybe_ssh[@]}"
		env LL_FS_RO="$LL_FS_RO" LL_FS_RW="$LL_FS_RW" IN_BENCHMARK_NS=1 VERBOSE=$VERBOSE unshare --mount --
		./run-bench-in-namespace.sh "${maybe_perf[@]}" taskset -c "${RUN_ON_CPU}" "${maybe_sandboxer[@]}"
		./open-ntimes "$NUM_ITERATIONS" "$expected_errno" "$d"
	)

	print_test_setup_json "$do_landlock" "$dir_depth" "$nb_extra_rules"
	print_verbose "Running command: ${cmd[*]}"

	local bpftrace_pid=""
	if [[ $DO_BPFTRACE -ne 0 ]]; then
		local bpftrace_cmd=(
			bpftrace -f json landlock_overhead.bt $RUN_ON_CPU
		)
		print_verbose "Running command: ${bpftrace_cmd[*]}"
		"${maybe_ssh[@]}" "${bpftrace_cmd[@]}" &
		bpftrace_pid=$!
	fi

	"${cmd[@]}"

	if [[ -n "$bpftrace_pid" ]]; then
		kill -INT $bpftrace_pid
		wait $bpftrace_pid
	fi
}

for trial in $(seq 1 $NB_TRIALS); do
	for depth in 0 1 5 10 20 29; do
		print_verbose "Running trial $trial for no Landlock"
		run_test 0 $depth 0
		for nb_extra_rules in 0 1 5 10 30 50 100 150 200; do
			print_verbose "Running trial $trial for depth $depth with $nb_extra_rules extra rules"
			run_test 1 $depth $nb_extra_rules
		done
	done
done
