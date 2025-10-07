"""
Socket 0 idle power calibration for baseline subtraction.

Measures Socket 0 idle power over 30 seconds to establish baseline
for accurate net energy calculations.
"""

import time
import statistics
from typing import Dict, List
from datetime import datetime
from pathlib import Path

from jouletrace.energy.pcm_socket_meter import PCMSocketMeter


class SocketCalibrator:
    """Calibrates Socket 0 idle power baseline."""
    
    def __init__(self, socket_id: int = 0, duration_seconds: int = 30):
        """
        Args:
            socket_id: Socket to calibrate (default: 0)
            duration_seconds: Calibration duration (default: 30)
        """
        self.socket_id = socket_id
        self.duration_seconds = duration_seconds
        self.meter = PCMSocketMeter()
        
    def verify_socket_idle(self) -> tuple[bool, str]:
        """
        Verify Socket 0 has no processes running.
        
        Returns:
            (is_idle, message)
        """
        import subprocess
        
        # Get Socket 0 CPU list
        isolated_cpus = Path('/sys/devices/system/cpu/isolated').read_text().strip()
        if not isolated_cpus:
            return False, "No isolated CPUs found. Run Part 1 setup first."
        
        # Count processes on Socket 0 CPUs
        cpu_pattern = isolated_cpus.replace(',', '|').replace('-', '|')
        try:
            result = subprocess.run(
                f"ps -eLo psr,comm | grep -E '^({cpu_pattern}) ' | wc -l",
                shell=True,
                capture_output=True,
                text=True
            )
            process_count = int(result.stdout.strip())
            
            if process_count > 0:
                return False, f"Socket {self.socket_id} has {process_count} processes running. Must be idle."
            
            return True, f"Socket {self.socket_id} is idle (0 processes)"
            
        except Exception as e:
            return False, f"Failed to verify socket state: {e}"
    
    def measure_idle_power(self) -> Dict:
        """
        Measure Socket 0 idle power over calibration duration.
        
        Returns:
            {
                'idle_power_watts': float,
                'stddev_watts': float,
                'measurements': int,
                'duration_seconds': float,
                'timestamp': str,
                'socket_id': int
            }
        """
        print(f"Calibrating Socket {self.socket_id} idle power...")
        print(f"Duration: {self.duration_seconds} seconds")
        print(f"Sampling every 1 second...\n")
        
        # Check if PCM is available
        if not self.meter.is_available():
            raise RuntimeError("PCM not available. Check RAPL support.")
        
        # Verify socket is idle
        is_idle, message = self.verify_socket_idle()
        if not is_idle:
            raise RuntimeError(f"Calibration failed: {message}")
        print(f"✓ {message}\n")
        
        # Collect power measurements
        power_samples: List[float] = []
        start_time = time.time()
        
        # Initial energy reading
        prev_energy = self.meter.get_socket_energy(self.socket_id)
        prev_time = time.time()
        
        # Warmup (discard first sample)
        time.sleep(1.0)
        curr_energy = self.meter.get_socket_energy(self.socket_id)
        curr_time = time.time()
        warmup_watts = (curr_energy - prev_energy) / (curr_time - prev_time)
        print(f"Warmup sample: {warmup_watts:.2f}W (discarded)")
        
        prev_energy = curr_energy
        prev_time = curr_time
        
        # Collect samples
        sample_num = 1
        while (time.time() - start_time) < self.duration_seconds:
            time.sleep(1.0)
            
            curr_energy = self.meter.get_socket_energy(self.socket_id)
            curr_time = time.time()
            
            elapsed = curr_time - prev_time
            energy_delta = curr_energy - prev_energy
            power_watts = energy_delta / elapsed
            
            power_samples.append(power_watts)
            print(f"Sample {sample_num:2d}: {power_watts:.2f}W")
            
            prev_energy = curr_energy
            prev_time = curr_time
            sample_num += 1
        
        actual_duration = time.time() - start_time
        
        # Calculate statistics
        median_watts = statistics.median(power_samples)
        mean_watts = statistics.mean(power_samples)
        stddev_watts = statistics.stdev(power_samples) if len(power_samples) > 1 else 0.0
        cv_percent = (stddev_watts / mean_watts * 100) if mean_watts > 0 else 0.0
        
        print(f"\n{'='*50}")
        print(f"Calibration Results:")
        print(f"{'='*50}")
        print(f"Samples:        {len(power_samples)}")
        print(f"Duration:       {actual_duration:.1f}s")
        print(f"Median Power:   {median_watts:.2f}W")
        print(f"Mean Power:     {mean_watts:.2f}W")
        print(f"Std Dev:        {stddev_watts:.3f}W")
        print(f"CV:             {cv_percent:.2f}%")
        print(f"{'='*50}\n")
        
        # Validate stability
        if cv_percent > 5.0:
            print(f"⚠ Warning: High variability (CV={cv_percent:.2f}%). Check thermal stability.")
        else:
            print(f"✓ Good stability (CV={cv_percent:.2f}%)")
        
        return {
            'idle_power_watts': median_watts,
            'mean_power_watts': mean_watts,
            'stddev_watts': stddev_watts,
            'cv_percent': cv_percent,
            'measurements': len(power_samples),
            'duration_seconds': actual_duration,
            'timestamp': datetime.now().isoformat(),
            'socket_id': self.socket_id
        }


# Standalone test
if __name__ == '__main__':
    print("Socket 0 Calibration Test\n")
    
    calibrator = SocketCalibrator(socket_id=0, duration_seconds=30)
    
    try:
        result = calibrator.measure_idle_power()
        print("\nCalibration data:")
        for key, value in result.items():
            print(f"  {key}: {value}")
            
    except Exception as e:
        print(f"\n Calibration failed: {e}")
        import traceback
        traceback.print_exc()