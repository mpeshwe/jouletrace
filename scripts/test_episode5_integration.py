#!/usr/bin/env python3
"""
Test Episode 5 integration: Socket measurement task with lock.

Tests the full stack without requiring Celery workers.
"""

import time
import uuid
import sys
from pathlib import Path

# Ensure JouleTrace package is reachable when run via sudo
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jouletrace.api.socket_measurement_task import (
    Socket0Lock,
    _parse_request,
    _validate_solution,
    _extract_test_inputs
)
from jouletrace.core.socket_executor import SocketExecutor
from jouletrace.core.statistical_aggregator import StatisticalAggregator
from jouletrace.core.validator import SolutionValidator


def test_lock_mechanism():
    """Test Redis lock acquire/release."""
    print("=" * 60)
    print("Test 1: Lock Mechanism")
    print("=" * 60)
    
    lock = Socket0Lock('redis://localhost:6379/0', lock_timeout=10)
    
    # Acquire lock
    print("Acquiring lock...")
    acquired = lock.acquire(blocking=False)
    
    if not acquired:
        print("✗ Failed to acquire lock")
        return False
    
    print("✓ Lock acquired")
    
    # Try to acquire again (should fail)
    lock2 = Socket0Lock('redis://localhost:6379/0', lock_timeout=10)
    print("Attempting second acquire (should fail)...")
    acquired2 = lock2.acquire(blocking=False)
    
    if acquired2:
        print("✗ Second acquire succeeded (should have failed)")
        return False
    
    print("✓ Second acquire correctly blocked")
    
    # Release lock
    lock.release()
    print("✓ Lock released")
    
    # Now second acquire should work
    print("Attempting acquire after release...")
    acquired3 = lock2.acquire(blocking=False)
    
    if not acquired3:
        print("✗ Acquire after release failed")
        return False
    
    print("✓ Acquire after release succeeded")
    lock2.release()
    
    print("\n✓ Test 1 PASSED\n")
    return True


def test_request_parsing():
    """Test request parsing."""
    print("=" * 60)
    print("Test 2: Request Parsing")
    print("=" * 60)
    
    request_data = {
        "candidate_code": "def solve(n): return n * 2",
        "function_name": "solve",
        "test_cases": [
            {"inputs": 5, "expected_output": 10, "test_id": "tc1"},
            {"inputs": 10, "expected_output": 20, "test_id": "tc2"}
        ],
        "timeout_seconds": 30,
        "memory_limit_mb": 512,
        "energy_measurement_trials": 5
    }
    
    request_id = str(uuid.uuid4())
    
    try:
        internal_request = _parse_request(request_data, request_id)
        print(f"✓ Request parsed successfully")
        print(f"  Function: {internal_request.function_name}")
        print(f"  Test cases: {len(internal_request.test_cases)}")
        print(f"  Trials: {internal_request.energy_measurement_trials}")
        
        # Extract inputs
        inputs = _extract_test_inputs(internal_request)
        print(f"✓ Inputs extracted: {inputs}")
        
        print("\n✓ Test 2 PASSED\n")
        return True
        
    except Exception as e:
        print(f"✗ Request parsing failed: {e}")
        return False


def test_validation():
    """Test solution validation."""
    print("=" * 60)
    print("Test 3: Solution Validation")
    print("=" * 60)
    
    # Correct solution
    request_data_correct = {
        "candidate_code": "def solve(n): return n * 2",
        "function_name": "solve",
        "test_cases": [
            {"inputs": 5, "expected_output": 10, "test_id": "tc1"},
            {"inputs": 10, "expected_output": 20, "test_id": "tc2"}
        ],
        "timeout_seconds": 30,
        "memory_limit_mb": 512
    }
    
    request_correct = _parse_request(request_data_correct, str(uuid.uuid4()))
    validator = SolutionValidator()
    
    print("Testing correct solution...")
    result_correct = _validate_solution(validator, request_correct)
    
    if not result_correct['is_correct']:
        print("✗ Correct solution marked as incorrect")
        return False
    
    print(f"✓ Correct solution validated: {result_correct['passed_tests']}/{result_correct['total_tests']}")
    
    # Incorrect solution
    request_data_incorrect = {
        "candidate_code": "def solve(n): return n * 3",  # Wrong!
        "function_name": "solve",
        "test_cases": [
            {"inputs": 5, "expected_output": 10, "test_id": "tc1"},
            {"inputs": 10, "expected_output": 20, "test_id": "tc2"}
        ],
        "timeout_seconds": 30,
        "memory_limit_mb": 512
    }
    
    request_incorrect = _parse_request(request_data_incorrect, str(uuid.uuid4()))
    
    print("Testing incorrect solution...")
    result_incorrect = _validate_solution(validator, request_incorrect)
    
    if result_incorrect['is_correct']:
        print("✗ Incorrect solution marked as correct")
        return False
    
    print(f"✓ Incorrect solution rejected: {result_incorrect['passed_tests']}/{result_incorrect['total_tests']}")
    
    print("\n✓ Test 3 PASSED\n")
    return True


def test_full_measurement_flow():
    """Test full measurement flow with lock."""
    print("=" * 60)
    print("Test 4: Full Measurement Flow")
    print("=" * 60)
    
    # Simple fibonacci test
    request_data = {
        "candidate_code": """
def fibonacci(n):
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b
""",
        "function_name": "fibonacci",
        "test_cases": [
            {"inputs": 10, "expected_output": 55, "test_id": "tc1"},
            {"inputs": 15, "expected_output": 610, "test_id": "tc2"}
        ],
        "timeout_seconds": 30,
        "memory_limit_mb": 512,
        "energy_measurement_trials": 3  # Quick test
    }
    
    request_id = str(uuid.uuid4())
    
    try:
        # Parse request
        print("1. Parsing request...")
        internal_request = _parse_request(request_data, request_id)
        print("   ✓ Request parsed")
        
        # Validate solution
        print("2. Validating solution...")
        validator = SolutionValidator()
        validation_result = _validate_solution(validator, internal_request)
        
        if not validation_result['is_correct']:
            print(f"   ✗ Validation failed: {validation_result}")
            return False
        
        print(f"   ✓ Validation passed: {validation_result['passed_tests']}/{validation_result['total_tests']}")
        
        # Acquire lock
        print("3. Acquiring Socket 0 lock...")
        lock = Socket0Lock('redis://localhost:6379/0')
        
        with lock:
            print("   ✓ Lock acquired")
            
            # Setup executor and aggregator
            print("4. Setting up measurement system...")
            executor = SocketExecutor(cpu_core=4)
            executor.setup()
            
            aggregator = StatisticalAggregator(
                min_trials=3,
                max_trials=5,
                target_cv_percent=5.0,
                early_stop_enabled=True
            )
            aggregator.setup(executor)
            print("   ✓ System ready")
            
            # Run measurements
            print("5. Running measurements...")
            
            # Batch inputs to reach 100ms minimum
            inputs = _extract_test_inputs(internal_request)
            batched_inputs = inputs * 25000  # Should give ~100-200ms
            
            result = aggregator.aggregate_measurements(
                code=internal_request.candidate_code,
                function_name=internal_request.function_name,
                test_inputs=batched_inputs,
                verbose=False
            )
            
            print(f"   ✓ Measurement complete:")
            print(f"     Energy: {result.median_energy_joules:.3f}J")
            print(f"     Time: {result.median_time_seconds:.3f}s")
            print(f"     Power: {result.median_power_watts:.3f}W")
            print(f"     CV: {result.cv_percent:.2f}%")
            print(f"     Confidence: {result.confidence_level}")
            print(f"     Trials: {result.successful_trials}/{result.total_trials}")
            
            # Cleanup
            executor.cleanup()
            print("   ✓ Cleanup complete")
        
        print("   ✓ Lock released")
        
        print("\n✓ Test 4 PASSED\n")
        return True
        
    except Exception as e:
        print(f"✗ Full measurement flow failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    print("\n")
    print("=" * 60)
    print("Episode 5 Integration Test Suite")
    print("=" * 60)
    print()
    
    tests = [
        ("Lock Mechanism", test_lock_mechanism),
        ("Request Parsing", test_request_parsing),
        ("Solution Validation", test_validation),
        ("Full Measurement Flow", test_full_measurement_flow)
    ]
    
    results = []
    
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n✗ Test '{name}' crashed: {e}\n")
            import traceback
            traceback.print_exc()
            results.append((name, False))
        
        time.sleep(1)
    
    # Summary
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = sum(1 for _, p in results if p)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")
    
    print()
    print(f"Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n✓ All tests PASSED - Episode 5 integration complete!\n")
    else:
        print(f"\n✗ {total - passed} test(s) FAILED\n")
    
    return passed == total


if __name__ == '__main__':
    import sys
    success = main()
    sys.exit(0 if success else 1)
