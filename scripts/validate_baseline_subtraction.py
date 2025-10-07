#!/usr/bin/env python3
"""
Validate baseline subtraction logic.

Runs simple workloads and verifies:
  Net Energy = Raw Energy - (Idle Power × Duration)
"""
import sys
import json
import time
from pathlib import Path
# Ensure JouleTrace package is reachable when run via sudo
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from jouletrace.energy.pcm_socket_meter import PCMSocketMeter


def load_calibration(path: Path = Path('config/socket0_calibration.json')) -> dict:
    """Load calibration profile."""
    if not path.exists():
        raise FileNotFoundError(
            f"Calibration profile not found: {path}\n"
            f"Run: sudo python3 scripts/calibrate_socket0.py"
        )
    
    with open(path) as f:
        return json.load(f)


def test_baseline_subtraction(meter: PCMSocketMeter, idle_power_watts: float, duration: float = 0.5):
    """
    Run a simple test and verify baseline subtraction.
    
    Args:
        meter: Initialized PCMSocketMeter
        idle_power_watts: Calibrated idle power
        duration: Test duration in seconds
    """
    socket_id = 0
    
    print(f"\nTest: {duration}s sleep on Socket 0")
    print(f"Idle baseline: {idle_power_watts:.3f}W")
    print("-" * 50)
    
    # Measure raw energy
    start_energy = meter.get_socket_energy(socket_id)
    start_time = time.time()
    
    time.sleep(duration)
    
    end_energy = meter.get_socket_energy(socket_id)
    end_time = time.time()
    
    actual_duration = end_time - start_time
    raw_energy = end_energy - start_energy
    
    # Calculate baseline energy
    baseline_energy = idle_power_watts * actual_duration
    
    # Calculate net energy
    net_energy = raw_energy - baseline_energy
    
    # Display results
    print(f"Duration:        {actual_duration:.3f}s")
    print(f"Raw Energy:      {raw_energy:.3f}J")
    print(f"Baseline Energy: {baseline_energy:.3f}J  ({idle_power_watts:.3f}W × {actual_duration:.3f}s)")
    print(f"Net Energy:      {net_energy:.3f}J")
    print(f"Net Power:       {net_energy/actual_duration:.3f}W")
    
    # Validation
    print()
    if abs(net_energy) < 1.0:
        print("✓ PASS: Net energy near zero (pure idle test)")
        print("  This confirms baseline subtraction is working correctly.")
    else:
        print(f" WARNING: Net energy = {net_energy:.3f}J")
        print("  Expected ~0J for pure sleep. Possible causes:")
        print("  - Background process on Socket 0")
        print("  - Thermal drift")
        print("  - Calibration needs update")
    
    return {
        'duration': actual_duration,
        'raw_energy': raw_energy,
        'baseline_energy': baseline_energy,
        'net_energy': net_energy
    }


def main():
    print("=" * 60)
    print("Baseline Subtraction Validation")
    print("=" * 60)
    
    # Load calibration
    try:
        calibration = load_calibration()
        idle_power = calibration['idle_power_watts']
        print(f"\nLoaded calibration:")
        print(f"  Idle Power: {idle_power:.3f}W")
        print(f"  Timestamp:  {calibration['timestamp']}")
        print(f"  CV:         {calibration['cv_percent']:.2f}%")
    except Exception as e:
        print(f"\n Error loading calibration: {e}")
        return
    
    # Run multiple tests
    test_durations = [0.2, 0.5, 1.0]
    results = []
    
    # Initialize meter once
    meter = PCMSocketMeter()
    meter.setup()
    
    try:
        for duration in test_durations:
            result = test_baseline_subtraction(meter, idle_power, duration)
            results.append(result)
            time.sleep(0.5)  # Brief pause between tests
    finally:
        meter.cleanup()  # Cleanup PCM
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    
    avg_net = sum(r['net_energy'] for r in results) / len(results)
    avg_net_power = sum(r['net_energy']/r['duration'] for r in results) / len(results)
    
    print(f"Tests run:       {len(results)}")
    print(f"Avg net energy:  {avg_net:.3f}J")
    print(f"Avg net power:   {avg_net_power:.3f}W")
    print()
    
    if abs(avg_net_power) < 2.0:
        print("✓ Validation PASSED")
        print("  Baseline subtraction is working correctly.")
        print(f"  Net power ~0W confirms idle measurements match calibration.")
    else:
        print(" Validation WARNING")
        print(f"  Net power = {avg_net_power:.3f}W (expected ~0W)")
        print("  Consider recalibrating if drift persists.")
    
    print()
    print("Next: Use this formula for all measurements:")
    print(f"  Net Energy = Raw Energy - ({idle_power:.3f}W × Duration)")
    print()


if __name__ == '__main__':
    main()