#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
Unit tests for statistical calculations in parse-microbench.py

This verifies correctness of:
- Welch's t-test implementation
- Variance pooling in merge_results (parallel axis theorem)
- Effect size (Cohen's d) calculation
"""

import math
import sys
import unittest
import importlib.util

# We need to avoid running main() when importing
# Patch sys.argv to prevent main() from failing
original_argv = sys.argv
sys.argv = ['parse-microbench.py', '/dev/null']  # dummy argument

# Import the module
spec = importlib.util.spec_from_file_location("parse_microbench", "parse-microbench.py")
module = importlib.util.module_from_spec(spec)

try:
    spec.loader.exec_module(module)
except SystemExit:
    pass  # Expected when running with no valid input
except Exception:
    pass  # May fail due to no valid input, that's ok for import
finally:
    sys.argv = original_argv

Stats = module.Stats
welch_t_test = module.welch_t_test
t_critical_value = module.t_critical_value
compare_stats = module.compare_stats
merge_results = module.merge_results


class TestMergeResults(unittest.TestCase):
    """Test the merge_results function with parallel axis theorem."""
    
    def test_merge_identical_distributions(self):
        """Merging two identical distributions should give same mean and stddev."""
        s1 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        s2 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        
        merged = merge_results(s1, s2)
        
        self.assertEqual(merged.count, 2000)
        self.assertAlmostEqual(merged.avg, 100.0, places=5)
        self.assertAlmostEqual(merged.stddev, 10.0, places=5)
    
    def test_merge_different_means_same_variance(self):
        """
        Merging distributions with different means should increase variance.
        
        This is the key test for the parallel axis theorem.
        If we have two groups with the same variance but different means,
        the combined variance should be larger than the individual variances.
        """
        # Two groups with same variance (stddev=10) but different means
        s1 = Stats(count=1000, avg=90.0, min=70.0, max=110.0, median=90.0, stddev=10.0)
        s2 = Stats(count=1000, avg=110.0, min=90.0, max=130.0, median=110.0, stddev=10.0)
        
        merged = merge_results(s1, s2)
        
        self.assertEqual(merged.count, 2000)
        self.assertAlmostEqual(merged.avg, 100.0, places=5)
        # The variance should be larger than 100 (= 10^2) because the means differ.
        # Using parallel axis theorem:
        #   combined_mean = 100
        #   delta1 = 90 - 100 = -10, delta2 = 110 - 100 = 10
        #   var1 = var2 = 100 (stddev^2)
        #   pooled_var = (1000*(100 + (-10)^2) + 1000*(100 + 10^2)) / 2000
        #              = (1000*(100 + 100) + 1000*(100 + 100)) / 2000
        #              = (200000 + 200000) / 2000 = 200
        # So stddev should be sqrt(200) ≈ 14.14
        self.assertGreater(merged.stddev, 10.0)  # Must be larger than individual stddev
        self.assertAlmostEqual(merged.stddev, math.sqrt(200), places=4)
    
    def test_merge_with_empty(self):
        """Merging with an empty distribution should return the other."""
        s1 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        s2 = Stats(count=0, avg=0.0, min=0.0, max=0.0, median=0.0, stddev=0.0)
        
        merged = merge_results(s1, s2)
        self.assertEqual(merged, s1)
        
        merged2 = merge_results(s2, s1)
        self.assertEqual(merged2, s1)
    
    def test_median_unavailable_after_merge(self):
        """Merged median should be -1 since it can't be computed from summaries."""
        s1 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        s2 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        
        merged = merge_results(s1, s2)
        self.assertEqual(merged.median, -1)


class TestWelchTTest(unittest.TestCase):
    """Test the Welch's t-test implementation."""
    
    def test_identical_distributions(self):
        """Identical distributions should give t-statistic of 0."""
        s1 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        s2 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        
        t_stat, df = welch_t_test(s1, s2)
        
        self.assertAlmostEqual(t_stat, 0.0, places=5)
    
    def test_clearly_different_distributions(self):
        """Distributions with very different means should give large t-statistic."""
        s1 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        s2 = Stats(count=1000, avg=200.0, min=180.0, max=220.0, median=200.0, stddev=10.0)
        
        t_stat, df = welch_t_test(s1, s2)
        
        # Should be negative (s1.avg < s2.avg) and large in magnitude
        self.assertLess(t_stat, -10)  # Very significant difference
    
    def test_degrees_of_freedom(self):
        """Test degrees of freedom calculation."""
        s1 = Stats(count=100, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        s2 = Stats(count=100, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        
        _, df = welch_t_test(s1, s2)
        
        # For equal variances and equal sample sizes, df ≈ n1 + n2 - 2 = 198
        # Welch's formula may give slightly different result but should be close
        self.assertGreater(df, 100)
        self.assertLess(df, 250)


class TestCompareStats(unittest.TestCase):
    """Test the compare_stats function."""
    
    def test_no_difference_identical(self):
        """Same distributions should show no significant difference."""
        s1 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        s2 = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        
        result = compare_stats(s1, s2)
        
        self.assertFalse(result.is_different)
        self.assertIsNone(result.direction)
    
    def test_no_difference_small_effect(self):
        """Tiny differences should not be flagged even if statistically significant."""
        # With 10000 samples, small differences become statistically significant
        # but we require practical significance (min_effect_size)
        base = Stats(count=10000, avg=1000.0, min=800.0, max=1200.0, median=1000.0, stddev=50.0)
        test = Stats(count=10000, avg=1002.0, min=802.0, max=1202.0, median=1002.0, stddev=50.0)
        
        result = compare_stats(base, test)
        
        # Effect size is (1000 - 1002) / 50 = -0.04, which is below threshold
        self.assertFalse(result.is_different)
        self.assertLess(abs(result.effect_size), 0.1)
    
    def test_significant_improvement(self):
        """Large improvement should show as improved."""
        base = Stats(count=10000, avg=1000.0, min=800.0, max=1200.0, median=1000.0, stddev=50.0)
        test = Stats(count=10000, avg=900.0, min=700.0, max=1100.0, median=900.0, stddev=50.0)
        
        result = compare_stats(base, test)
        
        self.assertTrue(result.is_different)
        self.assertEqual(result.direction, "improved")
        self.assertLess(result.diff_percent, 0)  # test is lower, so negative %
        # Effect size is (1000 - 900) / 50 = 2.0 (large)
        self.assertGreater(result.effect_size, 0.5)
    
    def test_significant_regression(self):
        """Large regression should show as worse."""
        base = Stats(count=10000, avg=1000.0, min=800.0, max=1200.0, median=1000.0, stddev=50.0)
        test = Stats(count=10000, avg=1100.0, min=900.0, max=1300.0, median=1100.0, stddev=50.0)
        
        result = compare_stats(base, test)
        
        self.assertTrue(result.is_different)
        self.assertEqual(result.direction, "worse")
        self.assertGreater(result.diff_percent, 0)  # test is higher, so positive %
    
    def test_effect_size_calculation(self):
        """Effect size should be calculated correctly."""
        base = Stats(count=1000, avg=100.0, min=80.0, max=120.0, median=100.0, stddev=10.0)
        test = Stats(count=1000, avg=105.0, min=85.0, max=125.0, median=105.0, stddev=10.0)
        
        result = compare_stats(base, test)
        
        # Cohen's d = (100 - 105) / 10 = -0.5 (medium effect)
        self.assertAlmostEqual(abs(result.effect_size), 0.5, places=2)


class TestTCriticalValue(unittest.TestCase):
    """Test the t critical value approximation."""
    
    def test_large_df(self):
        """For large df, should approach z=1.96."""
        t_crit = t_critical_value(1000)
        self.assertAlmostEqual(t_crit, 1.96, places=2)
    
    def test_small_df(self):
        """For small df, should be larger than 1.96."""
        t_crit = t_critical_value(10)
        self.assertGreater(t_crit, 1.96)
        # For df=10, actual value is about 2.228
        self.assertLess(t_crit, 2.5)


if __name__ == "__main__":
    # Don't exit with an error - we want to run these tests
    unittest.main(verbosity=2)
