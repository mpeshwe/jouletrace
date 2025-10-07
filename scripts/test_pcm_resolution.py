#!/usr/bin/env python3
"""
scripts/test_pcm_resolution.py

Test script to validate PCM energy measurement resolution for short executions.

This script answers critical questions:
1. What is the minimum reliably measurable duration?
2. What is the coefficient of variation for different durations?
3. Does energy scale linearly with execution time?
4. Is PCM suitable for measuring 50-500ms Python code executions?

Run: sudo python3 scripts/test_pcm_resolution.py
"""

import sys
import time
import statistics
from pathlib import Path
from typing import List, Dict, Tuple

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from jouletrace.energy.pcm_socket_meter import PCMSocketMeter


class Colors:
    """ANSI colors for output."""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    END = '\033[0m'


def busy_wait(duration_ms: int) -> None:
    """
    Busy-wait for specified duration to generate consistent CPU load.
    
    Args:
        duration_ms: Duration to wait in milliseconds
    """
    start = time.perf_counter()
    target = start + (duration_ms / 1000.0)
    
    # Busy loop
    iterations = 0
    while time.perf_counter() < target:
        # Simple arithmetic to keep CPU busy
        _ = sum(range(100))
        iterations += 1


def measure_workload_energy(
    meter: PCMSocketMeter,
    duration_ms: int,
    cpu_core: int,
    trials: int = 20
) -> Dict[str, float]:
    """
    Measure energy for a specific workload duration.
    
    Args:
        meter: PCM meter instance
        duration_ms: Target execution duration in milliseconds
        cpu_core: CPU core to run on
        trials: Number of measurement trials
        
    Returns:
        Dictionary with statistics about the measurements
    """
    socket_id = meter.get_cpu_socket(cpu_core)
    
    print(f"\n{Colors.BLUE}Testing {duration_ms}ms workload (Socket {socket_id}, CPU {cpu_core})...{Colors.END}")
    
    energies = []
    durations = []
    
    for trial in range(trials):
        # Clear cache
        if socket_id in meter._last_reading_cache:
            del meter._last_reading_cache[socket_id]
        
        # Read energy before
        time.sleep(0.01)  # Let counter update
        energy_before = meter.get_socket_energy(socket_id)
        
        # Execute workload
        start = time.perf_counter()
        busy_wait(duration_ms)
        duration = time.perf_counter() - start
        durations.append(duration * 1000)  # Convert to ms
        
        # Read energy after
        time.sleep(0.01)
        energy_after = meter.get_socket_energy(socket_id)
        
        # Calculate energy consumed
        energy_joules = energy_after - energy_before
        
        # Handle counter rollover
        if energy_joules < 0:
            energy_joules += (2**32 / 1_000_000.0)
        
        energies.append(energy_joules)
        
        # Progress indicator
        if (trial + 1) % 5 == 0:
            print(f"  Trial {trial + 1}/{trials}: {energy_joules:.6f}J")
        
        # Brief cooldown
        time.sleep(0.2)
    
    # Calculate statistics
    mean_energy = statistics.mean(energies)
    median_energy = statistics.median(energies)
    stddev_energy = statistics.stdev(energies) if len(energies) > 1 else 0.0
    cv_energy = (stddev_energy / mean_energy * 100) if mean_energy > 0 else 0.0
    
    mean_duration = statistics.mean(durations)
    
    return {
        'target_ms': duration_ms,
        'actual_duration_ms': mean_duration,
        'mean_energy_j': mean_energy,
        'median_energy_j': median_energy,
        'stddev_energy_j': stddev_energy,
        'cv_percent': cv_energy,
        'min_energy_j': min(energies),
        'max_energy_j': max(energies),
        'trials': len(energies),
        'all_energies': energies
    }


def test_energy_scaling(results: List[Dict[str, float]]) -> bool:
    """
    Test if energy scales approximately linearly with execution time.
    
    Longer executions should consume proportionally more energy.
    
    Args:
        results: List of measurement results
        
    Returns:
        True if scaling looks reasonable
    """
    print(f"\n{Colors.BOLD}Energy Scaling Analysis{Colors.END}")
    print("-" * 70)
    
    # Sort by duration
    sorted_results = sorted(results, key=lambda x: x['target_ms'])
    
    # Check ratios
    baseline = sorted_results[0]
    
    all_ratios_ok = True
    
    for result in sorted_results[1:]:
        duration_ratio = result['target_ms'] / baseline['target_ms']
        #energy_ratio = result['median_energy_j'] / baseline['median_energy_j']
        if baseline['median_energy_j'] == 0:
            print(f"Skipping {result['target_ms']}ms - baseline is zero")
            continue
        energy_ratio = result['median_energy_j'] / baseline['median_energy_j']
        # Energy should scale roughly with duration (allow 20% deviation)
        expected_min = duration_ratio * 0.8
        expected_max = duration_ratio * 1.2
        
        ratio_ok = expected_min <= energy_ratio <= expected_max
        
        status = f"{Colors.GREEN}✓{Colors.END}" if ratio_ok else f"{Colors.RED}✗{Colors.END}"
        
        print(f"{status} {baseline['target_ms']}ms → {result['target_ms']}ms: "
              f"Duration ratio={duration_ratio:.2f}x, "
              f"Energy ratio={energy_ratio:.2f}x "
              f"(expected {expected_min:.2f}x - {expected_max:.2f}x)")
        
        all_ratios_ok &= ratio_ok
    
    return all_ratios_ok


def evaluate_measurement_quality(results: List[Dict[str, float]]) -> Tuple[bool, str]:
    """
    Evaluate if PCM measurements are suitable for JouleTrace.
    
    Criteria:
    - CV < 15% for executions >= 200ms
    - CV < 25% for executions >= 50ms
    - Energy scaling looks reasonable
    
    Returns:
        (is_suitable, recommendation)
    """
    print(f"\n{Colors.BOLD}Measurement Quality Evaluation{Colors.END}")
    print("=" * 70)
    
    issues = []
    recommendations = []
    
    # Check each duration
    for result in results:
        duration_ms = result['target_ms']
        cv = result['cv_percent']
        
        if duration_ms >= 200:
            threshold = 15.0
            requirement = "≥200ms should have CV < 15%"
        elif duration_ms >= 50:
            threshold = 25.0
            requirement = "≥50ms should have CV < 25%"
        else:
            threshold = 35.0
            requirement = "Short durations may have higher CV"
        
        passed = cv < threshold
        status = f"{Colors.GREEN}✓{Colors.END}" if passed else f"{Colors.RED}✗{Colors.END}"
        
        print(f"{status} {duration_ms}ms: CV = {cv:.1f}% (threshold: {threshold:.1f}%)")
        
        if not passed:
            issues.append(f"{duration_ms}ms has CV {cv:.1f}% > {threshold:.1f}%")
    
    # Overall recommendation
    if not issues:
        recommendation = (
            f"{Colors.GREEN}✓ PCM is suitable for JouleTrace{Colors.END}\n"
            f"  Measurements are stable enough for 50-500ms Python executions.\n"
            f"  Proceed with Episode 2: Socket 0 Calibration."
        )
        is_suitable = True
    elif len(issues) == 1 and '50ms' in issues[0]:
        recommendation = (
            f"{Colors.YELLOW}⚠ PCM is marginally suitable{Colors.END}\n"
            f"  50ms executions have high variance, but 100ms+ are acceptable.\n"
            f"  Recommendation: Set minimum execution time to 100ms.\n"
            f"  Proceed with caution to Episode 2."
        )
        is_suitable = True
    else:
        recommendation = (
            f"{Colors.RED}✗ PCM may not be suitable{Colors.END}\n"
            f"  Multiple durations have unacceptable variance.\n"
            f"  Options:\n"
            f"    1. Batch multiple test cases (run 10 inputs instead of 1)\n"
            f"    2. Increase minimum execution time to 500ms\n"
            f"    3. Consider alternative measurement approach\n"
            f"  Review results before proceeding to Episode 2."
        )
        is_suitable = False
    
    return is_suitable, recommendation


def main():
    """Main test execution."""
    print(f"{Colors.BOLD}{'='*70}{Colors.END}")
    print(f"{Colors.BOLD}PCM Energy Measurement Resolution Test{Colors.END}")
    print(f"{Colors.BOLD}{'='*70}{Colors.END}\n")
    
    # Initialize PCM meter
    print(f"{Colors.BLUE}Initializing PCM meter...{Colors.END}")
    meter = PCMSocketMeter(use_sudo=True)
    
    if not meter.is_available():
        print(f"{Colors.RED}✗ PCM is not available on this system{Colors.END}")
        print("Ensure:")
        print("  1. PCM is installed")
        print("  2. You have root/sudo access")
        print("  3. RAPL interfaces are available")
        return 1
    
    meter.setup()
    print(f"{Colors.GREEN}✓ PCM meter initialized{Colors.END}")
    print(f"  Sockets detected: {meter.topology.socket_count}")
    
    # Use core 4 (Socket 0, as specified in isolation config)
    test_core = 4
    
    try:
        socket_id = meter.get_cpu_socket(test_core)
        print(f"  Test core: CPU {test_core} (Socket {socket_id})")
    except ValueError as e:
        print(f"{Colors.RED}✗ Error: {e}{Colors.END}")
        return 1
    
    # Test different execution durations
    durations_to_test = [50, 100, 200, 500]  # milliseconds
    trials_per_duration = 20
    
    print(f"\n{Colors.BOLD}Test Configuration:{Colors.END}")
    print(f"  Durations: {durations_to_test} ms")
    print(f"  Trials per duration: {trials_per_duration}")
    print(f"  Total measurements: {len(durations_to_test) * trials_per_duration}")
    print(f"  Estimated time: ~{len(durations_to_test) * trials_per_duration * 0.3 / 60:.1f} minutes")
    
    input(f"\n{Colors.YELLOW}Press Enter to start tests...{Colors.END}")
    
    # Run tests
    results = []
    for duration_ms in durations_to_test:
        result = measure_workload_energy(meter, duration_ms, test_core, trials_per_duration)
        results.append(result)
    
    # Print summary
    print(f"\n{Colors.BOLD}{'='*70}{Colors.END}")
    print(f"{Colors.BOLD}Measurement Summary{Colors.END}")
    print(f"{Colors.BOLD}{'='*70}{Colors.END}\n")
    
    print(f"{'Duration':<12} {'Mean Energy':<15} {'Median':<12} {'StdDev':<12} {'CV':<10}")
    print("-" * 70)
    
    for result in results:
        cv_color = Colors.GREEN if result['cv_percent'] < 15 else Colors.YELLOW if result['cv_percent'] < 25 else Colors.RED
        
        print(
            f"{result['target_ms']:>6}ms     "
            f"{result['mean_energy_j']:>8.6f}J     "
            f"{result['median_energy_j']:>8.6f}J  "
            f"{result['stddev_energy_j']:>8.6f}J  "
            f"{cv_color}{result['cv_percent']:>6.2f}%{Colors.END}"
        )
    
    # Test energy scaling
    scaling_ok = test_energy_scaling(results)
    
    # Evaluate quality
    is_suitable, recommendation = evaluate_measurement_quality(results)
    
    # Final verdict
    print(f"\n{Colors.BOLD}{'='*70}{Colors.END}")
    print(f"{Colors.BOLD}Final Recommendation{Colors.END}")
    print(f"{Colors.BOLD}{'='*70}{Colors.END}\n")
    print(recommendation)
    
    # Cleanup
    meter.cleanup()
    
    return 0 if is_suitable else 1


if __name__ == "__main__":
    sys.exit(main())