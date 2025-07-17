#!/usr/bin/env python3

import os
import re
import json

from argparse import ArgumentParser
from dataclasses import dataclass

from typing import List, Tuple

BPF_METRICS = [
    ("landlock_hook_ns", "@latency_landlock_hook"),
    ("open_syscall_ns", "@latency_open_syscall"),
    ("overhead", "@landlock_hook_overhead"),
]

argparser = ArgumentParser(description="Parse JSON outputs from microbench.sh")
argparser.add_argument(
    "input_file",
    help="Input files to parse. This will typically be a set of outputs that are interesting to compare against.",
    nargs="+",
)

args = argparser.parse_args()
inputs = args.input_file

variant_names = []
tests = {}


def collect_json_lines(input_file) -> List[List[dict]]:
    tests = []
    for line in input_file:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        if not isinstance(obj, dict):
            print(line)
            continue
        if "landlock" in obj:
            print(line)
        elif obj["type"] == "printf":
            print(obj["data"].strip())

        if "landlock" in obj:
            # new test
            tests.append([obj])
        else:
            if not tests:
                raise ValueError(
                    "input file does not start with a valid test description json line"
                )
            tests[-1].append(obj)
    return tests


@dataclass(eq=True, frozen=True)
class Stats:
    count: int
    avg: float
    min: float
    max: float
    median: float
    stddev: float

    def p95_confidence(self) -> Tuple[float, float]:
        # Assuming a normal distribution, 95% confidence interval is approximately
        # mean ± 1.96 * (stddev / sqrt(count))
        # (1.96 is standard normal distribution CDF(0.975))
        if self.count == 0:
            return 0.0
        diff = 1.96 * (self.stddev / (self.count ** 0.5))
        return (self.avg - diff, self.avg + diff)

def process_bpf_metric(name, bpf_data, bpf_data_type) -> Stats:
    if bpf_data_type[name] != "hist":
        raise ValueError(f"Expected {name} to be a histogram")
    hist = bpf_data[name]
    total = sum(bucket["count"] for bucket in hist)
    avg = bpf_data[name + "_avg"]
    if not isinstance(avg, int):
        raise ValueError(f"Expected {name}_avg to be an int")
    min_val = bpf_data[name + "_min"]
    if not isinstance(min_val, int):
        raise ValueError(f"Expected {name}_min to be an int")
    max_val = bpf_data[name + "_max"]
    if not isinstance(max_val, int):
        raise ValueError(f"Expected {name}_max to be an int")

    for bucket in hist:
        if "min" not in bucket:
            bucket["min"] = min_val
        if "max" not in bucket:
            bucket["max"] = max_val

    # Find median from the histogram
    median_pos = total // 2
    curr_seen = 0
    median = None
    for bucket in hist:
        start = bucket["min"]
        end = bucket["max"]
        count = bucket["count"]
        if curr_seen <= median_pos < curr_seen + count:
            median = start + (median_pos - curr_seen) / count * (end - start)
            break
        curr_seen += count

    # Find standard deviation from the histogram
    var = 0.0
    for bucket in hist:
        start = bucket["min"]
        end = bucket["max"]
        count = bucket["count"]
        midpoint = (start + end) / 2
        if end == max_val:
            # For the purpose of standard deviation, we avoid outliers
            # massively skewing the result by pretending that most of the
            # values in this bucket is at the front.
            midpoint = start
        var += count * ((midpoint - avg) ** 2)

    var /= total
    stddev = var ** 0.5
    return Stats(
        count=total,
        avg=avg,
        min=min_val,
        max=max_val,
        median=median,
        stddev=stddev,
    )

metrics_ordered = ["c_measured_syscall_time_ns"]

def parse_test_jsons(test_jsons: List[dict]) -> dict:
    cstats_lines = [l for l in test_jsons if l["type"] == "cstats"]
    if len(cstats_lines) != 1:
        raise ValueError("Expected exactly one cstats line per test")
    cstats = cstats_lines[0]
    bpf_data = {}
    bpf_data_type = {}

    result_metrics = {}

    for obj in test_jsons:
        if obj["type"] in ["hist", "map", "stats"]:
            d = obj["data"]
            for k, v in d.items():
                bpf_data[k] = v
                bpf_data_type[k] = obj["type"]

    for metric_name, bpf_map_name in BPF_METRICS:
        if bpf_map_name in bpf_data:
            result_metrics[metric_name] = process_bpf_metric(
                bpf_map_name, bpf_data, bpf_data_type
            )
            if metric_name not in metrics_ordered:
                metrics_ordered.append(metric_name)

    result_metrics["c_measured_syscall_time_ns"] = Stats(
        count=cstats["ntimes"],
        avg=cstats["mean"],
        min=cstats["min"],
        max=cstats["max"],
        median=-1,
        stddev=cstats["stddev"],
    )

    return result_metrics


@dataclass(eq=True, frozen=True)
class TestDescription:
    landlock: bool
    dir_depth: int
    nb_extra_rules: int

test_descs_ordered = []

def merge_results(stats1: Stats, stats2: Stats) -> Stats:
    if stats1.count == 0:
        return stats2
    if stats2.count == 0:
        return stats1
    total_count = stats1.count + stats2.count
    avg = (stats1.avg * stats1.count + stats2.avg * stats2.count) / total_count
    min_val = min(stats1.min, stats2.min)
    max_val = max(stats1.max, stats2.max)
    if stats1.median >= 0 and stats2.median >= 0:
        median = (stats1.median * stats1.count + stats2.median * stats2.count) / total_count
    else:
        median = -1
    stddev = ((stats1.stddev ** 2 * stats1.count + stats2.stddev ** 2 * stats2.count) / total_count) ** 0.5
    return Stats(total_count, avg, min_val, max_val, median, stddev)

for input_file in inputs:
    with open(input_file, "rt") as f:
        variant_name = re.sub(r"\.\w+$", "", os.path.basename(input_file))
        if variant_name in variant_names:
            raise ValueError(f"Duplicate variant name: {variant_name}")
        variant_names.append(variant_name)
        for test in collect_json_lines(f):
            testd = TestDescription(**test[0])
            if variant_name not in tests:
                tests[variant_name] = {}
            if testd not in test_descs_ordered:
                test_descs_ordered.append(testd)
            if testd not in tests[variant_name]:
                tests[variant_name][testd] = {}
            for metric_name, metric_stats in parse_test_jsons(test[1:]).items():
                if metric_name not in tests[variant_name][testd]:
                    tests[variant_name][testd][metric_name] = metric_stats
                else:
                    tests[variant_name][testd][metric_name] = merge_results(
                        tests[variant_name][testd][metric_name], metric_stats
                    )

base_variant = variant_names[0]
test_variants = variant_names[1:]

def confidence_interval_is_different(
    base_low: float, base_high: float,
    test_low: float, test_high: float
) -> bool:
    return base_low > test_high or test_low > base_high

for test_desc in test_descs_ordered:
    print(f"{test_desc}")
    for metric_name in metrics_ordered:
        base_stats = tests[base_variant][test_desc][metric_name]
        print(f"  {base_variant}:")
        print(f"    {metric_name}: {base_stats.count} samples, "
              f"avg={base_stats.avg:.2f}, min={base_stats.min:.2f}, "
              f"max={base_stats.max:.2f}, median={base_stats.median:.2f}, "
              f"stddev={base_stats.stddev:.2f}")
        base_low, base_high = base_stats.p95_confidence()
        print(f"    95% confidence interval: [{base_low:.2f} .. {base_high:.2f}]")

    for variant in test_variants:
        if test_desc not in tests[variant]:
            print(f"  {variant}: no data")
            continue
        variant_stats = tests[variant][test_desc]
        print(f"  {variant}:")
        for metric_name in metrics_ordered:
            if metric_name not in variant_stats:
                print(f"    {metric_name}: no data")
                continue
            stats = variant_stats[metric_name]
            print(f"    {metric_name}: {stats.count} samples, "
                  f"avg={stats.avg:.2f}, min={stats.min:.2f}, "
                  f"max={stats.max:.2f}, median={stats.median:.2f}, "
                  f"stddev={stats.stddev:.2f}")
            test_low, test_high = stats.p95_confidence()
            print(f"    95% confidence interval: [{test_low:.2f} .. {test_high:.2f}]")
            if test_high < base_low:
                print("    ** Improved **")
            elif test_low > base_high:
                print("    ** Worse **")
            else:
                print("    (No significant difference)")
