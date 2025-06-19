#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# Copyright © 2015-2025 Mickaël Salaün <mic@digikod.net>
#
# Launch a minimal User-Mode Linux system to run all Landlock tests.
#
# Examples:
# ./x86-run.sh linux-6.1 HISTFILE=/dev/null -- bash -i
# ./x86-run.sh .../linux -- .../tools/testing/selftests/kselftest_install/run_kselftest.sh

set -e -u -o pipefail

if [[ $# -lt 2 ]]; then
	echo "usage: ${BASH_SOURCE[0]} <linux-x86-kernel> [VAR=value]... -- <exec-path> [exec-arg]..." >&2
	exit 1
fi

BASE_DIR="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"

KERNEL="$1"
shift

if [[ "${1:-}" != "--" ]] then
	echo "ERROR: Missing '--' argument" >&2
	exit 1
fi
shift

# Handles relative file without "./" prefix.
KERNEL="$(readlink -f -- "${KERNEL}")"

if [[ ! -f "${KERNEL}" ]]; then
	echo "ERROR: Could not find this kernel: ${KERNEL}" >&2
	exit 1
fi

KERNEL_DIR="$(dirname -- "${KERNEL}")/"
if [[ "${KERNEL_DIR}" =~ ^/(tmp|run)/ ]]; then
	echo "ERROR: The kernel must not be in /tmp nor /run: ${KERNEL_DIR}" >&2
	exit 1
fi

if ! command -v vng &>/dev/null; then
	echo "ERROR: Unable to find the \"vng\" command (provided by virtme-ng)" >&2
	exit 1
fi

# FIXME:
OUT_RET="$(mktemp "--tmpdir=${KERNEL_DIR}" .x86-run-ret.XXXXXXXXXX)"

cleanup() {
	rm -- "${OUT_RET}"
}

trap cleanup QUIT INT TERM EXIT

echo "[*] Booting kernel ${KERNEL}"

# # virtme-ng requires a ~/.ssh/id_*.pub file
# if [[ ! -e ~/.ssh/id_virtme-ng-landlock-test ]]; then
# 	ssh-keygen -f ~/.ssh/id_virtme-ng-landlock-test -N ''
# fi
# vng --ssh
# ssh -F ~/.cache/virtme-ng/.ssh/virtme-ng-ssh.conf -o IdentityFile=~/.ssh/id_virtme-ng-landlock-test -l root ssh://virtme-ng:2222

vng --run "${KERNEL}" \
	--verbose \
	--user root \
	--append "loglevel=4" \
	--append "TEST_PATH=${BASE_DIR}/guest:${PATH:-/usr/bin}" \
	--append "TERM=${TERM:-linux}" \
	--append "TEST_UID=$(id -u)" \
	--append "TEST_CWD=$(pwd)" \
	--append "TEST_RET=${OUT_RET}" \
	--append "TEST_EXEC=$(printf "%s" "$*" | base64 --wrap=0)" \
	"${BASE_DIR}/guest/init.sh"

#exit "$(< "${OUT_RET}")"
