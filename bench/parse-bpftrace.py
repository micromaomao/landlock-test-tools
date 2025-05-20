#!/usr/bin/env python3

from argparse import ArgumentParser
import os
import re

argparser = ArgumentParser(description="Parse landlock_overhead.bt output")
argparser.add_argument(
    "input_file",
    help="Input files to parse. This will typically be a set of outputs that are interesting to compare against.",
    nargs="+",
)

args = argparser.parse_args()
inputs = args.input_file

tests = {}
scenarios = []

# [*] with landlock: d = / nb_extra_rules = 0
TEST_NAME_RE = re.compile(r"^\[\*\] (.+)$")
# => landlock hook took average 145 ns (131 ns min, 28172 ns max)
LANDLOCK_HOOK_RE = re.compile(
    r"^=> landlock hook took average ([0-9]+) ns \(([0-9]+) ns min, ([0-9]+) ns max\)$"
)
# => open syscall took average 3724 ns (3279 ns min, 61125 ns max)
OPEN_SYSCALL_RE = re.compile(
    r"^=> open syscall took average ([0-9]+) ns \(([0-9]+) ns min, ([0-9]+) ns max\)$"
)
# => landlock hook overhead (percent) average is 3 (0 min, 86 max)
LANDLOCK_OVERHEAD_RE = re.compile(
    r"^=> landlock hook overhead \(percent\) average is ([0-9]+) \(([0-9]+) min, ([0-9]+) max\)$"
)
# @histogram_name:
HISTOGRAM_START_RE = re.compile(r"^@([a-zA-Z0-9_]+):$")


def try_get_avg_min_max(regex, line):
    match = re.match(regex, line)
    if match:
        return {
            "avg": int(match.group(1)),
            "min": int(match.group(2)),
            "max": int(match.group(3)),
        }
    return None


def process_histogram(test_name, scenario_name, histogram_name, lines):
    if histogram_name == "landlock_hook_overhead":
        histogram_name = "landlock_overhead"
    elif histogram_name == "latency_landlock_hook":
        histogram_name = "landlock_hook"
    elif histogram_name == "latency_open_syscall":
        histogram_name = "open_syscall"
    else:
        raise ValueError(f"Unknown histogram @{histogram_name}")

    values = tests[test_name][scenario_name][histogram_name]
    if not values:
        raise ValueError(f"No values for {test_name} {scenario_name} {histogram_name}")

    # Examples:
    # (..., 3000)      6860638 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|
    # [3000, 3250)       39296 |                                                    |
    # [8000, ...)        16870 |                                                    |
    HISTOGRAM_LINE_RE = re.compile(
        r"^[\(\[](\d+|\.\.\.), (\d+|\.\.\.)[\)\]]\s+(\d+)\s+\|([@ ]+)\|$"
    )
    buckets = []
    total = 0
    for line in lines:
        match = re.match(HISTOGRAM_LINE_RE, line)
        if match:
            start_s = match.group(1)
            if start_s == "...":
                start = int(values["min"])
            else:
                start = int(start_s)
            end_s = match.group(2)
            if end_s == "...":
                end = int(values["max"])
            else:
                end = int(end_s)
            count = int(match.group(3))
            buckets.append((start, end, count))
            total += count
        else:
            raise ValueError(f"Unable to parse histogram line: {line}")
    median_pos = total // 2
    curr_seen = 0
    median = None
    for start, end, count in buckets:
        if curr_seen <= median_pos < curr_seen + count:
            median = start + (median_pos - curr_seen) / count * (end - start)
            break
        curr_seen += count
    values["median"] = round(median)


def process_input(input_file, scenario_name):
    curr_test_name = "Test"
    curr_histogram_name = None
    curr_histogram_buf = []

    def ensure_test_name():
        if curr_test_name not in tests:
            tests[curr_test_name] = {}

        if scenario_name not in tests[curr_test_name]:
            tests[curr_test_name][scenario_name] = {
                "landlock_hook": None,
                "open_syscall": None,
                "landlock_overhead": None,
            }

    for line in input_file:
        line = re.sub(r"\n$", "", line)

        if curr_histogram_name is not None:
            if not line:
                process_histogram(
                    curr_test_name,
                    scenario_name,
                    curr_histogram_name,
                    curr_histogram_buf,
                )
                curr_histogram_name = None
                curr_histogram_buf = []
                continue
            else:
                curr_histogram_buf.append(line)
                continue

        test_name_match = re.match(TEST_NAME_RE, line)
        if test_name_match:
            curr_test_name = test_name_match.group(1)
            ensure_test_name()
            continue
        landlock_hook_match = re.match(LANDLOCK_HOOK_RE, line)
        if landlock_hook_match:
            ensure_test_name()
            tests[curr_test_name][scenario_name]["landlock_hook"] = try_get_avg_min_max(
                LANDLOCK_HOOK_RE, line
            )
            continue
        open_syscall_match = re.match(OPEN_SYSCALL_RE, line)
        if open_syscall_match:
            ensure_test_name()
            tests[curr_test_name][scenario_name]["open_syscall"] = try_get_avg_min_max(
                OPEN_SYSCALL_RE, line
            )
            continue
        landlock_overhead_match = re.match(LANDLOCK_OVERHEAD_RE, line)
        if landlock_overhead_match:
            ensure_test_name()
            tests[curr_test_name][scenario_name]["landlock_overhead"] = (
                try_get_avg_min_max(LANDLOCK_OVERHEAD_RE, line)
            )
            continue
        histogram_start_match = re.match(HISTOGRAM_START_RE, line)
        if histogram_start_match:
            ensure_test_name()
            curr_histogram_name = histogram_start_match.group(1)
            continue

    if curr_histogram_name is not None:
        process_histogram(
            curr_test_name,
            scenario_name,
            curr_histogram_name,
            curr_histogram_buf,
        )
        curr_histogram_name = None
        curr_histogram_buf = []


for input_file in inputs:
    with open(input_file, "rt") as f:
        scenario_name = re.sub(r"(-bpf)?\.log$", "", os.path.basename(input_file))
        if scenario_name in scenarios:
            raise ValueError(f"Duplicate scenario name: {scenario_name}")
        scenarios.append(scenario_name)
        process_input(f, scenario_name)

left_pad = max(len(test_name) for test_name in tests.keys()) + 2
if left_pad < 20:
    left_pad = 20
print("Comparing:".ljust(left_pad) + "\t".join(scenarios))
for test_name in tests.keys():
    if test_name != "Test":
        print(f"{test_name}:")
    for metric in ["landlock_overhead", "landlock_hook", "open_syscall"]:

        def compare(stat_name):
            stat_for_scenario = [
                tests[test_name][scenario_name][metric][stat_name]
                for scenario_name in scenarios
            ]
            res = "\t".join(str(stat) for stat in stat_for_scenario)
            if len(stat_for_scenario) == 2:
                diff = stat_for_scenario[1] - stat_for_scenario[0]
                pct_change = round(diff / stat_for_scenario[0] * 100, 1)
                if pct_change < 0:
                    res += f"\t(-{abs(pct_change)}%)"
                elif pct_change > 0:
                    res += f"\t(+{abs(pct_change)}%)"
                else:
                    res += "\t(0%)"
            return res

        print(f"  {metric}:".ljust(left_pad) + f"    avg = {compare('avg')}")
        print(f" ".ljust(left_pad) + f" median = {compare('median')}")
