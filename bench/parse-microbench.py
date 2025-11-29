#!/usr/bin/env python3

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
        # Note: For sample sizes of ~1,000,000, using z instead of t is appropriate.
        if self.count == 0:
            return (0.0, 0.0)
        diff = 1.96 * (self.stddev / (self.count ** 0.5))
        return (self.avg - diff, self.avg + diff)


def welch_t_statistic(stats1: Stats, stats2: Stats) -> Tuple[float, float]:
    """
    Compute Welch's t-statistic for comparing two sample means.
    Returns (t_statistic, degrees_of_freedom).
    
    Welch's t-test is more robust than the standard t-test when samples 
    have unequal variances and/or unequal sample sizes.
    """
    n1, n2 = stats1.count, stats2.count
    if n1 == 0 or n2 == 0:
        return (0.0, 0.0)
    
    var1 = stats1.stddev ** 2
    var2 = stats2.stddev ** 2
    
    # Standard error of the difference
    se_sq = var1 / n1 + var2 / n2
    if se_sq == 0:
        return (float('inf') if stats1.avg != stats2.avg else 0.0, float('inf'))
    
    se = math.sqrt(se_sq)
    
    # t-statistic
    t_stat = (stats1.avg - stats2.avg) / se
    
    # Welch-Satterthwaite degrees of freedom
    numerator = se_sq ** 2
    denominator = (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
    if denominator == 0:
        df = float('inf')
    else:
        df = numerator / denominator
    
    return (t_stat, df)


def t_critical_value(df: float, alpha: float = 0.05) -> float:
    """
    Approximate two-tailed t critical value for given degrees of freedom.
    For large df (> 100), this approaches the z-value of ~1.96 for alpha=0.05.
    
    This is an approximation since we don't have scipy available.
    Uses the formula: t ≈ z + (z + z^3/4) / (4*df) + higher order terms
    For df > 30, z-approximation is generally adequate.
    """
    # z value for two-tailed test at alpha=0.05
    z = 1.96  
    if df > 100:
        return z
    # Simple approximation that works reasonably well
    # Based on inverse t-distribution approximation
    return z * (1 + 1 / (4 * df) + z ** 2 / (16 * df))


@dataclass
class ComparisonResult:
    is_different: bool
    direction: Optional[str]  # "improved", "worse", or None
    t_statistic: float
    degrees_of_freedom: float
    effect_size: float  # Cohen's d
    diff_percent: float


def compare_stats(base: Stats, test: Stats, alpha: float = 0.05) -> ComparisonResult:
    """
    Compare two samples using Welch's t-test.
    Returns a ComparisonResult indicating whether there's a significant difference.
    
    This is more robust than just checking if confidence intervals overlap,
    because it directly tests the null hypothesis that the means are equal.
    """
    if base.count == 0 or test.count == 0:
        return ComparisonResult(
            is_different=False, direction=None,
            t_statistic=0.0, degrees_of_freedom=0.0,
            effect_size=0.0, diff_percent=0.0
        )
    
    t_stat, df = welch_t_statistic(base, test)
    t_crit = t_critical_value(df, alpha)
    
    is_different = abs(t_stat) > t_crit
    
    # Determine direction of change (positive t_stat means base > test, i.e., test improved)
    direction = None
    if is_different:
        if t_stat > 0:
            direction = "improved"  # test is lower (faster)
        else:
            direction = "worse"  # test is higher (slower)
    
    # Effect size: Cohen's d using pooled standard deviation
    # A small effect is d=0.2, medium is d=0.5, large is d=0.8
    pooled_var = ((base.count - 1) * base.stddev ** 2 + (test.count - 1) * test.stddev ** 2) / \
                 (base.count + test.count - 2)
    pooled_sd = math.sqrt(pooled_var) if pooled_var > 0 else 1.0
    effect_size = (base.avg - test.avg) / pooled_sd if pooled_sd > 0 else 0.0
    
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
    
    Uses the correct pooled variance formula that accounts for differences
    in means between the two samples:
    var_combined = [n1*(var1 + (mean1 - combined_mean)^2) + n2*(var2 + (mean2 - combined_mean)^2)] / (n1 + n2)
    
    Note: Median cannot be correctly computed from summary statistics alone,
    so we mark it as unavailable (-1) when merging.
    """
    if stats1.count == 0:
        return stats2
    if stats2.count == 0:
        return stats1
    n1, n2 = stats1.count, stats2.count
    total_count = n1 + n2
    avg = (stats1.avg * n1 + stats2.avg * n2) / total_count
    min_val = min(stats1.min, stats2.min)
    max_val = max(stats1.max, stats2.max)
    # Median cannot be correctly computed from summary statistics alone
    # without access to the original data, so we mark it as unavailable
    median = -1
    # Correct pooled variance formula that accounts for differences in means
    var1 = stats1.stddev ** 2
    var2 = stats2.stddev ** 2
    delta1 = stats1.avg - avg
    delta2 = stats2.avg - avg
    pooled_var = (n1 * (var1 + delta1 ** 2) + n2 * (var2 + delta2 ** 2)) / total_count
    stddev = pooled_var ** 0.5
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
    stddev = var ** 0.5
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
                for metric_name, metric_stats in parse_test_jsons(test[1:], metrics_ordered).items():
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
