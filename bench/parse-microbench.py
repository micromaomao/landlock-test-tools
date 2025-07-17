#!/usr/bin/env python3

import os
import re
import json

from argparse import ArgumentParser
from dataclasses import dataclass

from typing import List, Optional

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
    median: Optional[float]
    stddev: float

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

    result_metrics["c_measured_syscall_time_ns"] = Stats(
        count=cstats["ntimes"],
        avg=cstats["mean"],
        min=cstats["min"],
        max=cstats["max"],
        median=None,
        stddev=cstats["stddev"],
    )

    return result_metrics


@dataclass(eq=True, frozen=True)
class TestDescription:
    landlock: bool
    dir_depth: int
    nb_extra_rules: int


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
            tests[variant_name][testd] = parse_test_jsons(test[1:])

print("Parsed tests:")
for variant_name, test_data in tests.items():
    print(f"  {variant_name}:")
    for test_desc, metrics in test_data.items():
        print(f"    {test_desc}:")
        for metric_name, stats in metrics.items():
            print(f"      {metric_name}: avg={stats.avg}, min={stats.min}, max={stats.max}, stddev={stats.stddev}")
