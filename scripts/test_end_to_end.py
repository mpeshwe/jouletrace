#!/usr/bin/env python3
"""
Episode 8: Comprehensive End-to-End Testing

Tests the complete Socket 0 measurement pipeline:
1. System readiness
2. Measurement accuracy and reproducibility
3. Known workload comparisons
4. Stress testing
"""

import time
import statistics
import json
from pathlib import Path
from typing import List, Dict, Any


import sys
from pathlib import Path

# Ensure JouleTrace package is reachable when run via sudo
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jouletrace.core.socket_executor import SocketExecutor
from jouletrace.core.statistical_aggregator import StatisticalAggregator
from jouletrace.core.validator import SolutionValidator
from jouletrace.api.socket_measurement_task import Socket0Lock


class TestResults:
    """Track test results across suite."""
    def __init__(self):
        self.tests_run = 0
        self.tests_passed = 0
        self.tests_failed = 0
        self.failures: List[str] = []
    
    def record_pass(self, test_name: str):
        self.tests_run += 1
        self.tests_passed += 1
        print(f"  ✓ {test_name}")
    
    def record_fail(self, test_name: str, reason: str):
        self.tests_run += 1
        self.tests_failed += 1
        self.failures.append(f"{test_name}: {reason}")
        print(f"  ✗ {test_name}: {reason}")
    
    def summary(self):
        print(f"\n{'='*60}")
        print("Test Suite Summary")
        print(f"{'='*60}")
        print(f"Total:  {self.tests_run}")
        print(f"Passed: {self.tests_passed}")
        print(f"Failed: {self.tests_failed}")
        
        if self.failures:
            print(f"\nFailures:")
            for failure in self.failures:
                print(f"  - {failure}")
        
        return self.tests_failed == 0


def test_system_readiness(results: TestResults):
    """Test 1: Verify system is ready for measurements."""
    print("\n" + "="*60)
    print("Test 1: System Readiness")
    print("="*60)
    
    # Check calibration
    cal_path = Path('config/socket0_calibration.json')
    if not cal_path.exists():
        results.record_fail("Calibration exists", "calibration file not found")
        return
    
    results.record_pass("Calibration exists")
    
    # Validate calibration
    with open(cal_path) as f:
        cal_data = json.load(f)
    
    required_fields = ['idle_power_watts', 'cv_percent', 'timestamp']
    if all(field in cal_data for field in required_fields):
        results.record_pass("Calibration format valid")
    else:
        results.record_fail("Calibration format", "missing required fields")
        return
    
    # Check CV
    if cal_data['cv_percent'] < 1.0:
        results.record_pass(f"Calibration quality (CV={cal_data['cv_percent']:.2f}%)")
    else:
        results.record_fail("Calibration quality", f"CV too high: {cal_data['cv_percent']:.2f}%")
    
    # Check isolation
    isolated_path = Path('/sys/devices/system/cpu/isolated')
    if isolated_path.exists():
        isolated_cpus = isolated_path.read_text().strip()
        if isolated_cpus:
            results.record_pass(f"Socket 0 isolated ({isolated_cpus})")
        else:
            results.record_fail("Socket 0 isolation", "no isolated CPUs")
    else:
        results.record_fail("Socket 0 isolation", "isolation not configured")
    
    # Check Redis
    try:
        from jouletrace.infrastructure.config import get_config
        config = get_config()
        lock = Socket0Lock(config.celery.broker_url, lock_timeout=5)
        
        if lock.acquire(blocking=False):
            lock.release()
            results.record_pass("Redis connectivity")
        else:
            results.record_fail("Redis connectivity", "lock already held")
    except Exception as e:
        results.record_fail("Redis connectivity", str(e))


def test_measurement_accuracy(results: TestResults):
    """Test 2: Measure accuracy with known workload."""
    print("\n" + "="*60)
    print("Test 2: Measurement Accuracy")
    print("="*60)
    
    # Simple workload with predictable characteristics
    test_code = '''
def compute_sum(n):
    """Simple addition loop."""
    total = 0
    for i in range(n):
        total += i
    return total
'''
    
    # Batch to reach 100ms minimum
    test_inputs = [1000000] * 100  # Should take ~100-200ms
    
    try:
        executor = SocketExecutor(cpu_core=4)
        executor.setup()
        
        aggregator = StatisticalAggregator(
            min_trials=5,
            max_trials=10,
            target_cv_percent=5.0,
            early_stop_enabled=True
        )
        aggregator.setup(executor)
        
        result = aggregator.aggregate_measurements(
            code=test_code,
            function_name='compute_sum',
            test_inputs=test_inputs,
            verbose=False
        )
        
        executor.cleanup()
        
        # Validate results
        if result.successful_trials >= 3:
            results.record_pass(f"Completed {result.successful_trials} trials")
        else:
            results.record_fail("Trial completion", f"only {result.successful_trials} trials")
            return
        
        # Check CV
        if result.cv_percent < 5.0:
            results.record_pass(f"Measurement precision (CV={result.cv_percent:.2f}%)")
        else:
            results.record_fail("Measurement precision", f"CV={result.cv_percent:.2f}% > 5%")
        
        # Check confidence
        if result.confidence_level == "high":
            results.record_pass(f"Confidence level: {result.confidence_level}")
        else:
            results.record_fail("Confidence level", f"not high: {result.confidence_level}")
        
        # Check energy is reasonable
        if 5.0 < result.median_energy_joules < 50.0:
            results.record_pass(f"Energy range reasonable ({result.median_energy_joules:.2f}J)")
        else:
            results.record_fail("Energy range", f"unexpected: {result.median_energy_joules:.2f}J")
        
        # Check execution time (100ms to 30s is reasonable range)
        if 0.1 < result.median_time_seconds < 30.0:
            results.record_pass(f"Execution time reasonable ({result.median_time_seconds:.3f}s)")
        else:
            results.record_fail("Execution time", f"unexpected: {result.median_time_seconds:.3f}s")
        
    except Exception as e:
        results.record_fail("Measurement execution", str(e))


def test_reproducibility(results: TestResults):
    """Test 3: Verify measurements are reproducible."""
    print("\n" + "="*60)
    print("Test 3: Reproducibility")
    print("="*60)
    
    test_code = '''
def fibonacci(n):
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b
'''
    
    # Run same workload 3 times
    test_inputs = [35] * 50000
    
    energies = []
    
    try:
        executor = SocketExecutor(cpu_core=4)
        executor.setup()
        
        for run in range(3):
            print(f"  Run {run + 1}/3...", end=" ", flush=True)
            
            result = executor.execute_single_trial(
                code=test_code,
                function_name='fibonacci',
                test_inputs=test_inputs,
                trial_number=run,
                verify_idle=True
            )
            
            if result.success:
                energies.append(result.net_energy_joules)
                print(f"{result.net_energy_joules:.3f}J")
            else:
                print(f"FAILED: {result.error_message}")
                results.record_fail(f"Reproducibility run {run+1}", result.error_message)
                executor.cleanup()
                return
            
            time.sleep(1.0)  # Cooldown
        
        executor.cleanup()
        
        # Calculate reproducibility metrics
        mean_energy = statistics.mean(energies)
        stddev = statistics.stdev(energies)
        cv_percent = (stddev / mean_energy * 100) if mean_energy > 0 else 0
        
        if cv_percent < 5.0:
            results.record_pass(f"Reproducibility excellent (CV={cv_percent:.2f}%)")
        elif cv_percent < 10.0:
            results.record_pass(f"Reproducibility acceptable (CV={cv_percent:.2f}%)")
        else:
            results.record_fail("Reproducibility", f"CV={cv_percent:.2f}% > 10%")
        
        # Check individual variations
        max_deviation = max(abs(e - mean_energy) / mean_energy * 100 for e in energies)
        if max_deviation < 10.0:
            results.record_pass(f"Max deviation acceptable ({max_deviation:.2f}%)")
        else:
            results.record_fail("Max deviation", f"{max_deviation:.2f}% > 10%")
        
    except Exception as e:
        results.record_fail("Reproducibility test", str(e))


def test_workload_comparison(results: TestResults):
    """Test 4: Compare different workload complexities."""
    print("\n" + "="*60)
    print("Test 4: Workload Comparison")
    print("="*60)
    
    # Light workload
    light_code = '''
def light_work(n):
    return sum(range(n))
'''
    
    # Heavy workload
    heavy_code = '''
def heavy_work(n):
    result = 0
    for i in range(n):
        for j in range(100):
            result += i * j
    return result
'''
    
    try:
        executor = SocketExecutor(cpu_core=4)
        executor.setup()
        
        # Measure light workload
        print("  Measuring light workload...")
        light_result = executor.execute_single_trial(
            code=light_code,
            function_name='light_work',
            test_inputs=[10000] * 5000,
            trial_number=0,
            verify_idle=False
        )
        
        time.sleep(1.0)
        
        # Measure heavy workload
        print("  Measuring heavy workload...")
        heavy_result = executor.execute_single_trial(
            code=heavy_code,
            function_name='heavy_work',
            test_inputs=[1000] * 500,
            trial_number=0,
            verify_idle=False
        )
        
        executor.cleanup()
        
        if not (light_result.success and heavy_result.success):
            results.record_fail("Workload execution", "one or more workloads failed")
            return
        
        # Heavy should use more energy
        if heavy_result.net_energy_joules > light_result.net_energy_joules:
            ratio = heavy_result.net_energy_joules / light_result.net_energy_joules
            results.record_pass(f"Energy scales with complexity ({ratio:.2f}x)")
        else:
            results.record_fail("Energy scaling", "heavy workload used less energy")
        
        # Both should have reasonable net power (after baseline subtraction)
        # Net power = code execution above idle baseline
        # Expect 5-150W net power above ~54W baseline
        light_power = light_result.net_energy_joules / light_result.execution_time_seconds
        heavy_power = heavy_result.net_energy_joules / heavy_result.execution_time_seconds
        
        if 5 < light_power < 150 and 5 < heavy_power < 200:
            results.record_pass(f"Net power consumption reasonable (L:{light_power:.1f}W, H:{heavy_power:.1f}W)")
        else:
            results.record_fail("Net power consumption", f"unexpected: L:{light_power:.1f}W, H:{heavy_power:.1f}W")
        
    except Exception as e:
        results.record_fail("Workload comparison", str(e))


def test_validation_gate(results: TestResults):
    """Test 5: Verify validation blocks incorrect solutions."""
    print("\n" + "="*60)
    print("Test 5: Validation Gate")
    print("="*60)
    
    from jouletrace.core.models import JouleTraceMeasurementRequest, JouleTraceTestCase
    from uuid import uuid4
    
    # Correct solution
    correct_code = "def solve(x): return x * 2"
    
    # Incorrect solution
    incorrect_code = "def solve(x): return x * 3"
    
    test_cases = [
        JouleTraceTestCase(inputs=5, expected_output=10, test_id="t1"),
        JouleTraceTestCase(inputs=10, expected_output=20, test_id="t2")
    ]
    
    validator = SolutionValidator()
    
    # Test correct solution
    print("  Testing correct solution...")
    correct_result = validator.validate_solution(
        candidate_code=correct_code,
        function_name='solve',
        test_cases=test_cases
    )
    
    if correct_result.is_correct:
        results.record_pass("Correct solution passes validation")
    else:
        results.record_fail("Correct solution validation", "marked as incorrect")
    
    # Test incorrect solution
    print("  Testing incorrect solution...")
    incorrect_result = validator.validate_solution(
        candidate_code=incorrect_code,
        function_name='solve',
        test_cases=test_cases
    )
    
    if not incorrect_result.is_correct:
        results.record_pass("Incorrect solution fails validation")
    else:
        results.record_fail("Incorrect solution validation", "marked as correct")


def test_lock_serialization(results: TestResults):
    """Test 6: Verify Redis lock prevents concurrent Socket 0 access."""
    print("\n" + "="*60)
    print("Test 6: Lock Serialization")
    print("="*60)
    
    try:
        from jouletrace.infrastructure.config import get_config
        config = get_config()
        
        # Acquire first lock
        lock1 = Socket0Lock(config.celery.broker_url, lock_timeout=10)
        acquired1 = lock1.acquire(blocking=False)
        
        if not acquired1:
            results.record_fail("First lock acquire", "could not acquire")
            return
        
        results.record_pass("First lock acquired")
        
        # Try to acquire second lock (should fail)
        lock2 = Socket0Lock(config.celery.broker_url, lock_timeout=10)
        acquired2 = lock2.acquire(blocking=False)
        
        if not acquired2:
            results.record_pass("Second lock blocked (serialization enforced)")
        else:
            lock2.release()
            results.record_fail("Lock serialization", "second lock succeeded")
        
        # Release first lock
        lock1.release()
        results.record_pass("First lock released")
        
        # Now second lock should work
        acquired3 = lock2.acquire(blocking=False)
        if acquired3:
            results.record_pass("Lock available after release")
            lock2.release()
        else:
            results.record_fail("Lock release", "still blocked after release")
        
    except Exception as e:
        results.record_fail("Lock serialization test", str(e))


def test_stress_multiple_measurements(results: TestResults):
    """Test 7: Run multiple measurements back-to-back."""
    print("\n" + "="*60)
    print("Test 7: Stress Test (10 Measurements)")
    print("="*60)
    
    test_code = '''
def simple_loop(n):
    total = 0
    for i in range(n):
        total += i
    return total
'''
    
    test_inputs = [100000] * 1000
    
    energies = []
    times = []
    
    try:
        executor = SocketExecutor(cpu_core=4)
        executor.setup()
        
        for i in range(10):
            print(f"  Measurement {i+1}/10...", end=" ", flush=True)
            
            result = executor.execute_single_trial(
                code=test_code,
                function_name='simple_loop',
                test_inputs=test_inputs,
                trial_number=i,
                verify_idle=False  # Skip verification for speed
            )
            
            if result.success:
                energies.append(result.net_energy_joules)
                times.append(result.execution_time_seconds)
                print(f"{result.net_energy_joules:.3f}J")
            else:
                print(f"FAILED")
                results.record_fail(f"Stress measurement {i+1}", result.error_message)
            
            time.sleep(0.3)  # Brief cooldown
        
        executor.cleanup()
        
        # Analyze stability
        if len(energies) == 10:
            results.record_pass(f"All 10 measurements completed")
            
            mean_energy = statistics.mean(energies)
            stddev_energy = statistics.stdev(energies)
            cv_energy = (stddev_energy / mean_energy * 100) if mean_energy > 0 else 0
            
            if cv_energy < 10.0:
                results.record_pass(f"Stress test stability (CV={cv_energy:.2f}%)")
            else:
                results.record_fail("Stress test stability", f"CV={cv_energy:.2f}% > 10%")
        else:
            results.record_fail("Stress test completion", f"only {len(energies)}/10 succeeded")
        
    except Exception as e:
        results.record_fail("Stress test", str(e))


def main():
    print("\n")
    print("="*60)
    print("JouleTrace Socket 0 Architecture")
    print("End-to-End Testing Suite")
    print("="*60)
    print()
    print("This comprehensive test validates:")
    print("  1. System readiness and configuration")
    print("  2. Measurement accuracy and precision")
    print("  3. Reproducibility across runs")
    print("  4. Workload comparison and scaling")
    print("  5. Validation gate correctness")
    print("  6. Redis lock serialization")
    print("  7. Stress testing under load")
    print()
    input("Press Enter to begin testing...")
    
    results = TestResults()
    
    # Run test suite
    test_system_readiness(results)
    test_validation_gate(results)
    test_lock_serialization(results)
    test_measurement_accuracy(results)
    test_reproducibility(results)
    test_workload_comparison(results)
    test_stress_multiple_measurements(results)
    
    # Final summary
    success = results.summary()
    
    if success:
        print("\n✓ ALL TESTS PASSED - Socket 0 system ready for production!")
        print("\nNext steps:")
        print("  - Deploy Celery workers with docker-compose")
        print("  - Integrate with training pipeline")
        print("  - Monitor calibration age (recalibrate weekly)")
        return 0
    else:
        print("\n✗ SOME TESTS FAILED - Review failures above")
        return 1


if __name__ == '__main__':
    import sys
    sys.exit(main())