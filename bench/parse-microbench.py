#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
#
# Parse JSON outputs from microbench.sh and bpftrace, computing statistics
# (mean, median, standard deviation, confidence intervals) for each benchmark
# configuration. When multiple input files are provided, compares the results
# across different variants to identify performance differences.
#
# Usage: ./parse-microbench.py <log1.json> [log2.json ...]
#
# Copyright © 2025 Tingmao Wang <m@maowtm.org>

import math
import os
import re
import json

from argparse import ArgumentParser
from dataclasses import dataclass

from typing import List, Tuple, Optional

BPF_METRICS = [
    ("landlock_hook_ns", "@latency_landlock_hook"),
    ("open_syscall_ns", "@latency_open_syscall"),
    ("overhead", "@landlock_hook_overhead"),
]


@dataclass(eq=True, frozen=True)
class Stats:
    """Statistics for a set of measurements."""
    count: int
    avg: float
    min: float
    max: float
    median: float
    stddev: float

    def p95_confidence(self) -> Tuple[float, float]:
        """
        Calculate 95% confidence interval for the mean.
        
        Uses z=1.96 (normal distribution), which is appropriate for large sample
        sizes. For the typical ~10,000+ samples in benchmarks, this is accurate.
        """
        if self.count == 0:
            return (0.0, 0.0)
        diff = 1.96 * (self.stddev / math.sqrt(self.count))
        return (self.avg - diff, self.avg + diff)


def welch_t_test(stats1: Stats, stats2: Stats) -> Tuple[float, float]:
    """
    Compute Welch's t-statistic for comparing two sample means.
    
    Returns (t_statistic, degrees_of_freedom).
    
    Welch's t-test is more robust than the standard t-test when samples 
    have unequal variances and/or unequal sample sizes.
    
    Edge cases:
    - Returns (0.0, 0.0) if either sample has count <= 1
    - Returns (inf, inf) if both variances are zero but means differ
      (indicating a degenerate case with no variance)
    - Returns (0.0, inf) if both variances are zero and means are equal
    """
    n1, n2 = stats1.count, stats2.count
    if n1 <= 1 or n2 <= 1:
        return (0.0, 0.0)
    
    var1 = stats1.stddev ** 2
    var2 = stats2.stddev ** 2
    
    # Standard error of the difference
    se_sq = var1 / n1 + var2 / n2
    if se_sq == 0:
        # Degenerate case: both samples have zero variance
        # If means differ, t-stat is infinite; otherwise 0
        return (float('inf') if stats1.avg != stats2.avg else 0.0, float('inf'))
    
    se = math.sqrt(se_sq)
    
    # t-statistic
    t_stat = (stats1.avg - stats2.avg) / se
    
    # Welch-Satterthwaite degrees of freedom
    numerator = se_sq ** 2
    denominator = (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
    if denominator == 0:
        # With zero denominator (both variances very small), df approaches infinity
        df = float('inf')
    else:
        df = numerator / denominator
    
    return (t_stat, df)


def t_critical_value(df: float, alpha: float = 0.05) -> float:
    """
    Approximate two-tailed t critical value for given degrees of freedom.
    
    For large df (> 100), this approaches the z-value of ~1.96 for alpha=0.05.
    Uses an approximation since we don't have scipy available.
    
    The approximation formula is a Taylor series expansion of the inverse
    t-distribution: t ≈ z * (1 + 1/(4*df) + z²/(16*df))
    This is accurate to within 1% for df > 5 and approaches z for large df.
    Reference: Abramowitz & Stegun, Handbook of Mathematical Functions, 26.7.5
    """
    z = 1.96  # z value for two-tailed test at alpha=0.05
    if df > 100:
        return z
    if df <= 0:
        return float('inf')
    # Taylor series approximation of inverse t-distribution
    return z * (1 + 1 / (4 * df) + z ** 2 / (16 * df))


@dataclass
class ComparisonResult:
    """Result of comparing two sets of statistics."""
    is_different: bool
    direction: Optional[str]  # "improved", "worse", or None
    t_statistic: float
    degrees_of_freedom: float
    effect_size: float  # Cohen's d
    diff_percent: float


def compare_stats(base: Stats, test: Stats, alpha: float = 0.05,
                  min_effect_size: float = 0.1) -> ComparisonResult:
    """
    Compare two samples using Welch's t-test with practical significance filter.
    
    This is more robust than checking if confidence intervals overlap,
    because it directly tests the null hypothesis that the means are equal.
    
    To avoid false positives from large sample sizes, we require both:
    1. Statistical significance (p < alpha via t-test)
    2. Practical significance (|effect_size| >= min_effect_size)
    
    Args:
        base: Statistics for the baseline measurement
        test: Statistics for the test measurement
        alpha: Significance level (default 0.05 for 95% confidence)
        min_effect_size: Minimum Cohen's d to consider practically significant
                         (default 0.1, which is a "small" effect)
    
    Returns:
        ComparisonResult with statistical comparison details
    """
    if base.count <= 1 or test.count <= 1:
        return ComparisonResult(
            is_different=False, direction=None,
            t_statistic=0.0, degrees_of_freedom=0.0,
            effect_size=0.0, diff_percent=0.0
        )
    
    t_stat, df = welch_t_test(base, test)
    t_crit = t_critical_value(df, alpha)
    
    # Effect size: Cohen's d using pooled standard deviation
    # Interpretation: negligible<0.2, small=0.2, medium=0.5, large=0.8
    pooled_var = ((base.count - 1) * base.stddev ** 2 + 
                  (test.count - 1) * test.stddev ** 2) / (base.count + test.count - 2)
    pooled_sd = math.sqrt(pooled_var) if pooled_var > 0 else 1.0
    effect_size = (base.avg - test.avg) / pooled_sd if pooled_sd > 0 else 0.0
    
    # Require both statistical AND practical significance
    statistically_significant = abs(t_stat) > t_crit
    practically_significant = abs(effect_size) >= min_effect_size
    is_different = statistically_significant and practically_significant
    
    # Direction: positive t_stat means base > test (i.e., test improved/faster)
    direction = None
    if is_different:
        if t_stat > 0:
            direction = "improved"  # test is lower (faster)
        else:
            direction = "worse"  # test is higher (slower)
    
    # Percentage difference
    diff_percent = 100 * (test.avg - base.avg) / base.avg if base.avg != 0 else 0.0
    
    return ComparisonResult(
        is_different=is_different,
        direction=direction,
        t_statistic=t_stat,
        degrees_of_freedom=df,
        effect_size=effect_size,
        diff_percent=diff_percent
    )


def merge_results(stats1: Stats, stats2: Stats) -> Stats:
    """
    Merge two Stats objects into one combined Stats object.
    
    Uses the parallel axis theorem (correct pooled variance formula) that 
    accounts for differences in means between the two samples:
    
    var_combined = [n1*(var1 + delta1²) + n2*(var2 + delta2²)] / (n1 + n2)
    
    where delta1 = mean1 - combined_mean, delta2 = mean2 - combined_mean
    
    Note: Median cannot be correctly computed from summary statistics alone,
    so we mark it as unavailable (-1) when merging.
    """
    if stats1.count == 0:
        return stats2
    if stats2.count == 0:
        return stats1
    
    n1, n2 = stats1.count, stats2.count
    total_count = n1 + n2
    
    # Combined mean
    avg = (stats1.avg * n1 + stats2.avg * n2) / total_count
    
    # Min/max
    min_val = min(stats1.min, stats2.min)
    max_val = max(stats1.max, stats2.max)
    
    # Median cannot be correctly computed from summary statistics
    median = -1
    
    # Correct pooled variance using parallel axis theorem
    var1 = stats1.stddev ** 2
    var2 = stats2.stddev ** 2
    delta1 = stats1.avg - avg
    delta2 = stats2.avg - avg
    pooled_var = (n1 * (var1 + delta1 ** 2) + n2 * (var2 + delta2 ** 2)) / total_count
    stddev = math.sqrt(pooled_var)
    
    return Stats(total_count, avg, min_val, max_val, median, stddev)


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
    stddev = math.sqrt(var)
    return Stats(
        count=total,
        avg=avg,
        min=min_val,
        max=max_val,
        median=median,
        stddev=stddev,
    )


@dataclass(eq=True, frozen=True)
class TestDescription:
    landlock: bool
    dir_depth: int
    nb_extra_rules: int


def parse_test_jsons(test_jsons: List[dict], metrics_ordered: List[str]) -> dict:
    cstats_lines = [l for l in test_jsons if l["type"] == "cstats"]
    if len(cstats_lines) != 1:
        return None
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


def main():
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
    test_descs_ordered = []
    metrics_ordered = ["c_measured_syscall_time_ns"]

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
                parsed = parse_test_jsons(test[1:], metrics_ordered)
                if not parsed:
                    continue
                for metric_name, metric_stats in parsed.items():
                    if metric_name not in tests[variant_name][testd]:
                        tests[variant_name][testd][metric_name] = metric_stats
                    else:
                        tests[variant_name][testd][metric_name] = merge_results(
                            tests[variant_name][testd][metric_name], metric_stats
                        )

    base_variant = variant_names[0]
    test_variants = variant_names[1:]

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
                base_stats = tests[base_variant][test_desc][metric_name]
                print(f"    {metric_name}: {stats.count} samples, "
                      f"avg={stats.avg:.2f}, min={stats.min:.2f}, "
                      f"max={stats.max:.2f}, median={stats.median:.2f}, "
                      f"stddev={stats.stddev:.2f}")
                test_low, test_high = stats.p95_confidence()
                print(f"    95% confidence interval: [{test_low:.2f} .. {test_high:.2f}]")
                
                # Use Welch's t-test for more robust comparison
                comparison = compare_stats(base_stats, stats)
                diff_str = f"{comparison.diff_percent:+.2f}%"
                effect_str = f"effect={comparison.effect_size:.3f}"
                
                if comparison.is_different:
                    if comparison.direction == "improved":
                        print(f"    ** Improved ** ({diff_str}, {effect_str})")
                    else:
                        print(f"    ** Worse ** ({diff_str}, {effect_str})")
                else:
                    print(f"    (No significant difference, {diff_str}, {effect_str})")


if __name__ == "__main__":
    main()
