"""
Socket 0 execution engine with energy measurement.

Executes Python code on isolated Socket 0 and measures energy consumption
with baseline subtraction for accurate net energy attribution.
"""

import json
import time
import tempfile
import subprocess
import os
from pathlib import Path
from typing import List, Any, Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

from jouletrace.energy.pcm_socket_meter import PCMSocketMeter


@dataclass
class ExecutionResult:
    """Result of a single execution measurement."""
    net_energy_joules: float
    raw_energy_joules: float
    baseline_energy_joules: float
    execution_time_seconds: float
    trial_number: int
    cpu_core: int
    timestamp: str
    success: bool
    error_message: Optional[str] = None
    # Optional breakdowns
    package_net_energy_joules: float = 0.0
    dram_energy_joules: float = 0.0


class CalibrationProfile:
    """Manages Socket 0 calibration profile."""
    
    def __init__(self, config_path: Path = Path('config/socket0_calibration.json')):
        self.config_path = config_path
        self.idle_power_watts: Optional[float] = None
        self.timestamp: Optional[str] = None
        self.cv_percent: Optional[float] = None
        self.valid_until_days: int = 7
        
    def load(self) -> None:
        """Load calibration profile from disk."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Calibration profile not found: {self.config_path}\n"
                f"Run: sudo python3 scripts/calibrate_socket0.py"
            )
        
        with open(self.config_path) as f:
            data = json.load(f)
        
        self.idle_power_watts = data['idle_power_watts']
        self.timestamp = data['timestamp']
        self.cv_percent = data.get('cv_percent', 0.0)
        self.valid_until_days = data.get('valid_until_days', 7)
        
    def is_valid(self) -> tuple[bool, str]:
        """
        Check if calibration is still valid.
        
        Returns:
            (is_valid, reason)
        """
        if not self.idle_power_watts:
            return False, "Calibration not loaded"
        
        if not self.timestamp:
            return False, "Calibration timestamp missing"
        
        # Check age
        try:
            cal_time = datetime.fromisoformat(self.timestamp)
            age = datetime.now() - cal_time
            
            if age > timedelta(days=self.valid_until_days):
                return False, f"Calibration expired ({age.days} days old, max {self.valid_until_days})"
        except ValueError:
            return False, "Invalid timestamp format"
        
        return True, "Valid"
    
    def get_baseline_energy(self, duration_seconds: float) -> float:
        """Calculate baseline energy for given duration."""
        if not self.idle_power_watts:
            raise RuntimeError("Calibration not loaded")
        return self.idle_power_watts * duration_seconds


class SocketExecutor:
    """
    Executes code on Socket 0 with energy measurement.
    
    Features:
    - CPU pinning to Socket 0
    - PCM energy measurement
    - Baseline subtraction
    - Execution time tracking
    """
    
    def __init__(self, 
                 socket_id: int = 0,
                 cpu_core: int = 4,
                 timeout_seconds: int = 30,
                 calibration_path: Path = Path('config/socket0_calibration.json')):
        """
        Args:
            socket_id: Target socket (default: 0)
            cpu_core: CPU core to pin execution (default: 4 on Socket 0)
            timeout_seconds: Execution timeout (default: 30)
            calibration_path: Path to calibration profile
        """
        self.socket_id = socket_id
        self.cpu_core = cpu_core
        self.timeout_seconds = timeout_seconds
        
        # Initialize components
        self.meter = PCMSocketMeter()
        self.calibration = CalibrationProfile(calibration_path)
        
        self._initialized = False
    
    def setup(self) -> None:
        """Initialize executor (setup meter, load calibration)."""
        if self._initialized:
            return
        
        # Setup PCM meter
        self.meter.setup()
        
        # Load and validate calibration
        self.calibration.load()
        is_valid, reason = self.calibration.is_valid()
        
        if not is_valid:
            raise RuntimeError(
                f"Calibration invalid: {reason}\n"
                f"Run: sudo python3 scripts/calibrate_socket0.py"
            )
        
        print(f"✓ Calibration loaded: {self.calibration.idle_power_watts:.3f}W idle")
        print(f"  Timestamp: {self.calibration.timestamp}")
        print(f"  CV: {self.calibration.cv_percent:.2f}%\n")
        
        self._initialized = True
    
    def cleanup(self) -> None:
        """Cleanup resources."""
        if self._initialized:
            self.meter.cleanup()
            self._initialized = False
    
    def _create_execution_script(self,
                                 code: str,
                                 function_name: str,
                                 test_inputs: List[Any],
                                 min_wall_time_seconds: float) -> str:
        """
        Create Python script for isolated execution.
        
        Returns script content as string.
        """
        # Template for execution script
        script_template = '''
import sys
import time
from typing import List, Dict, Any, Tuple, Optional, Set, Union

# User code
{user_code}

# Test configuration
function_name = "{function_name}"
test_inputs = {test_inputs!r}
min_wall_time_seconds = {min_wall_time_seconds}

# Verify function exists
if function_name not in globals():
    print(f"ERROR: Function '{{function_name}}' not found", file=sys.stderr)
    sys.exit(1)

target_function = globals()[function_name]

# Execute on all test inputs
start_time = time.perf_counter()

loops = 0
try:
    while True:
        for test_input in test_inputs:
            # Handle different input formats
            if isinstance(test_input, dict):
                result = target_function(**test_input)
            elif isinstance(test_input, (list, tuple)):
                result = target_function(*test_input)
            else:
                result = target_function(test_input)
        loops += 1
        if time.perf_counter() - start_time >= min_wall_time_seconds:
            break
except Exception as e:
    print(f"ERROR: {{type(e).__name__}}: {{e}}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)

end_time = time.perf_counter()
execution_time = end_time - start_time

# Report execution time
print(f"EXECUTION_TIME: {{execution_time:.6f}}", file=sys.stderr)
print(f"SUCCESS", file=sys.stderr)
'''
        
        return script_template.format(
            user_code=code,
            function_name=function_name,
            test_inputs=test_inputs,
            min_wall_time_seconds=min_wall_time_seconds
        )
    
    def _verify_socket_idle(self) -> tuple[bool, str]:
        """Verify Socket 0 has no other processes."""
        try:
            result = subprocess.run(
                f"ps -eLo psr,comm | grep -E '^({self.cpu_core}) ' | wc -l",
                shell=True,
                capture_output=True,
                text=True,
                timeout=5
            )
            process_count = int(result.stdout.strip())
            
            if process_count > 0:
                return False, f"Socket {self.socket_id} has {process_count} processes"
            
            return True, "Socket idle"
            
        except Exception as e:
            return False, f"Failed to verify: {e}"
    
    def execute_single_trial(self,
                            code: str,
                            function_name: str,
                            test_inputs: List[Any],
                            trial_number: int = 0,
                            verify_idle: bool = False,
                            min_wall_time_seconds: float = 0.1) -> ExecutionResult:
        """
        Execute single measurement trial.
        
        Args:
            code: Python code containing function to measure
            function_name: Name of function to call
            test_inputs: List of inputs to pass to function
            trial_number: Trial identifier
            verify_idle: Check if socket is idle before execution
            
        Returns:
            ExecutionResult with energy and timing data
        """
        if not self._initialized:
            self.setup()
        
        # Verify socket is idle (optional but recommended)
        if verify_idle:
            is_idle, message = self._verify_socket_idle()
            if not is_idle:
                return ExecutionResult(
                    net_energy_joules=0.0,
                    raw_energy_joules=0.0,
                    baseline_energy_joules=0.0,
                    execution_time_seconds=0.0,
                    trial_number=trial_number,
                    cpu_core=self.cpu_core,
                    timestamp=datetime.now().isoformat(),
                    success=False,
                    error_message=f"Socket not idle: {message}"
                )
        
        # Create execution script
        script_content = self._create_execution_script(
            code, function_name, test_inputs, min_wall_time_seconds
        )
        
        # Write to temporary file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script_content)
            script_path = f.name
        
        try:
            # Clear PCM cache to force fresh readings
            if self.socket_id in self.meter._last_reading_cache:
                del self.meter._last_reading_cache[self.socket_id]
            
            # Small delay to ensure counter stability
            time.sleep(0.002)
            
            # Read energy BEFORE execution (package + DRAM)
            reading_before = self.meter.get_socket_reading(self.socket_id)
            time_before = time.perf_counter()
            
            # Execute code with CPU pinning (taskset)
            # This ensures execution stays on Socket 0
            cmd = [
                'taskset',
                '-c', str(self.cpu_core),
                'python3', script_path
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds
            )
            
            time_after = time.perf_counter()
            wall_time = time_after - time_before
            
            # Small delay before reading energy
            time.sleep(0.002)
            
            # Clear cache again
            if self.socket_id in self.meter._last_reading_cache:
                del self.meter._last_reading_cache[self.socket_id]
            
            # Read energy AFTER execution (package + DRAM)
            reading_after = self.meter.get_socket_reading(self.socket_id)
            
            # Check for execution errors
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else "Unknown error"
                return ExecutionResult(
                    net_energy_joules=0.0,
                    raw_energy_joules=0.0,
                    baseline_energy_joules=0.0,
                    execution_time_seconds=0.0,
                    trial_number=trial_number,
                    cpu_core=self.cpu_core,
                    timestamp=datetime.now().isoformat(),
                    success=False,
                    error_message=f"Execution failed: {error_msg}"
                )
            
            # Use wall-clock duration for baseline/power; ignore in-script time
            execution_time = wall_time
            
            # Calculate raw energy deltas
            pkg_raw = reading_after.package_energy_joules - reading_before.package_energy_joules
            dram_raw = reading_after.dram_energy_joules - reading_before.dram_energy_joules
            
            # Handle counter rollover (RAPL is 32-bit)
            if pkg_raw < 0:
                pkg_raw += (2**32 / 1_000_000.0)
            if dram_raw < 0:
                dram_raw += (2**32 / 1_000_000.0)
            
            # Calculate baseline energy
            baseline_energy = self.calibration.get_baseline_energy(execution_time)
            
            # Calculate net energies
            package_net = max(0.0, pkg_raw - baseline_energy)
            total_net = package_net + max(0.0, dram_raw)
            
            return ExecutionResult(
                net_energy_joules=total_net,
                raw_energy_joules=pkg_raw,
                baseline_energy_joules=baseline_energy,
                execution_time_seconds=execution_time,
                trial_number=trial_number,
                cpu_core=self.cpu_core,
                timestamp=datetime.now().isoformat(),
                success=True,
                package_net_energy_joules=package_net,
                dram_energy_joules=dram_raw
            )
            
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                net_energy_joules=0.0,
                raw_energy_joules=0.0,
                baseline_energy_joules=0.0,
                execution_time_seconds=0.0,
                trial_number=trial_number,
                cpu_core=self.cpu_core,
                timestamp=datetime.now().isoformat(),
                success=False,
                error_message=f"Execution timeout ({self.timeout_seconds}s)"
            )
        
        except Exception as e:
            return ExecutionResult(
                net_energy_joules=0.0,
                raw_energy_joules=0.0,
                baseline_energy_joules=0.0,
                execution_time_seconds=0.0,
                trial_number=trial_number,
                cpu_core=self.cpu_core,
                timestamp=datetime.now().isoformat(),
                success=False,
                error_message=f"Unexpected error: {str(e)}"
            )
        
        finally:
            # Cleanup temporary script
            try:
                os.unlink(script_path)
            except OSError:
                pass


# Standalone test
if __name__ == '__main__':
    print("Socket Executor Test\n")
    
    # Test code: simple fibonacci
    test_code = '''
def fibonacci(n):
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b
'''
    
    # Test inputs - batch to reach 100ms minimum
    # Run fibonacci(35) 50 times to get measurable duration (~100-250ms)
    test_inputs = [35] * 50000
    
    # Create executor
    executor = SocketExecutor(cpu_core=4)
    
    try:
        print("Setting up executor...")
        executor.setup()
        
        print(f"Executing fibonacci on Socket 0, CPU {executor.cpu_core}")
        print(f"Test: fibonacci(35) × {len(test_inputs)} iterations\n")
        
        # Run 3 trials
        for trial in range(3):
            print(f"Trial {trial + 1}:")
            result = executor.execute_single_trial(
                code=test_code,
                function_name='fibonacci',
                test_inputs=test_inputs,
                trial_number=trial
            )
            
            if result.success:
                print(f"  ✓ Success")
                print(f"  Raw Energy:      {result.raw_energy_joules:.3f}J")
                print(f"  Baseline:        {result.baseline_energy_joules:.3f}J")
                print(f"  Net Energy:      {result.net_energy_joules:.3f}J")
                print(f"  Execution Time:  {result.execution_time_seconds:.3f}s")
                print(f"  Net Power:       {result.net_energy_joules/result.execution_time_seconds:.3f}W")
            else:
                print(f"  ✗ Failed: {result.error_message}")
            
            print()
            time.sleep(0.5)
        
    finally:
        executor.cleanup()
        print("Cleanup complete")
