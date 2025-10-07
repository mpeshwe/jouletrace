#!/usr/bin/env python3
"""
CLI tool to calibrate Socket 0 idle power and save calibration profile.

Usage:
    sudo python3 scripts/calibrate_socket0.py [--duration SECONDS]
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure JouleTrace package is reachable when run via sudo
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jouletrace.core.socket_calibration import SocketCalibrator


def main():
    parser = argparse.ArgumentParser(
        description='Calibrate Socket 0 idle power baseline'
    )
    parser.add_argument(
        '--duration',
        type=int,
        default=30,
        help='Calibration duration in seconds (default: 30)'
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=Path('config/socket0_calibration.json'),
        help='Output JSON file (default: config/socket0_calibration.json)'
    )
    
    args = parser.parse_args()
    
    # Ensure we're running as root (needed for PCM)
    import os
    if os.geteuid() != 0:
        print(" Error: This script must be run as root (use sudo)")
        sys.exit(1)
    
    print("="*60)
    print("Socket 0 Calibration Tool")
    print("="*60)
    print()
    
    # Run calibration
    calibrator = SocketCalibrator(socket_id=0, duration_seconds=args.duration)
    
    try:
        result = calibrator.measure_idle_power()
    except Exception as e:
        print(f"Calibration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # Create output directory
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save calibration profile
    print(f"\nSaving calibration profile to: {output_path}")
    
    calibration_profile = {
        'socket_id': result['socket_id'],
        'idle_power_watts': result['idle_power_watts'],
        'mean_power_watts': result['mean_power_watts'],
        'stddev_watts': result['stddev_watts'],
        'cv_percent': result['cv_percent'],
        'measurements': result['measurements'],
        'duration_seconds': result['duration_seconds'],
        'timestamp': result['timestamp'],
        'valid_until_days': 7,
        'notes': 'Recalibrate if thermal baseline shifts >5°C or after 7 days'
    }
    
    with open(output_path, 'w') as f:
        json.dump(calibration_profile, f, indent=2)
    
    print(f"✓ Calibration profile saved\n")
    
    # Summary
    print("="*60)
    print("Calibration Summary")
    print("="*60)
    print(f"Socket ID:      {result['socket_id']}")
    print(f"Idle Power:     {result['idle_power_watts']:.3f}W")
    print(f"Std Dev:        {result['stddev_watts']:.3f}W")
    print(f"CV:             {result['cv_percent']:.2f}%")
    print(f"Samples:        {result['measurements']}")
    print(f"Profile:        {output_path}")
    print("="*60)
    print()
    print("✓ Calibration complete!")
    print(f"  Use this baseline to subtract {result['idle_power_watts']:.3f}W × duration")
    print(f"  from raw energy measurements.")
    print()
    print("⚠ Recalibrate if:")
    print("  - More than 7 days have passed")
    print("  - Thermal baseline shifts >5°C")
    print("  - Measurements show unexpected drift")
    print()


if __name__ == '__main__':
    main()
