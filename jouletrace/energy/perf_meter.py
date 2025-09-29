# jouletrace/energy/perf_meter.py
from __future__ import annotations
import os
import time
import subprocess
import tempfile
import re
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
import logging

from .interfaces import (
    EnergyMeter, 
    EnergyMeterType, 
    EnergyMeterCapability,
    EnergyMeterError,
    EnergyMeterNotAvailableError,
    EnergyMeterPermissionError,
    EnergyMeterTimeoutError
)
from ..core.models import EnergyMeasurement
from ..core.executor import SafeCodeExecutor

logger = logging.getLogger(__name__)

class PerfEnergyMeter(EnergyMeter):
    """
    Energy measurement using Linux perf with RAPL (Running Average Power Limit) events.
    
    This is the primary energy measurement backend for x86 systems with Intel/AMD processors
    that support RAPL energy monitoring through perf events.
    """
    
    # RAPL events for energy measurement
    RAPL_EVENTS = [
        "power/energy-pkg/",     # Package energy (CPU + integrated components)
        "power/energy-ram/",     # DRAM energy
        "power/energy-cores/",   # CPU cores only (optional)
        "power/energy-gpu/",     # Integrated GPU (optional)
    ]
    
    # Required events (must be available)
    REQUIRED_EVENTS = ["power/energy-pkg/"]
    
    def __init__(self, use_sudo: bool = False, perf_timeout: int = 60):
        """
        Initialize perf energy meter.
        
        Args:
            use_sudo: Whether to use sudo for perf commands (needed for some systems)
            perf_timeout: Timeout for perf commands in seconds
        """
        self.use_sudo = use_sudo
        self.perf_timeout = perf_timeout
        self.available_events: List[str] = []
        self._perf_command_base: Optional[List[str]] = None
        self._setup_complete = False
    
    @property
    def meter_type(self) -> EnergyMeterType:
        return EnergyMeterType.PERF
    
    @property
    def capabilities(self) -> List[EnergyMeterCapability]:
        capabilities = [EnergyMeterCapability.PACKAGE_ENERGY]
        
        if "power/energy-ram/" in self.available_events:
            capabilities.append(EnergyMeterCapability.RAM_ENERGY)
        
        if "power/energy-cores/" in self.available_events:
            capabilities.append(EnergyMeterCapability.CORE_ENERGY)
        
        if "power/energy-gpu/" in self.available_events:
            capabilities.append(EnergyMeterCapability.GPU_ENERGY)
        
        return capabilities
    
    def _check_perf_availability(self) -> bool:
        """Check if perf command is available."""
        try:
            cmd = ["sudo", "perf", "--version"] if self.use_sudo else ["perf", "--version"]
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def _check_rapl_events(self) -> List[str]:
        """Check which RAPL events are available on this system."""
        available = []
        
        for event in self.RAPL_EVENTS:
            try:
                # Test if event is available by running a quick perf command
                cmd = ["sudo", "perf", "list"] if self.use_sudo else ["perf", "list"]
                result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
                
                if result.returncode == 0 and event in result.stdout:
                    available.append(event)
                    
            except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                continue
        
        return available
    
    def _check_permissions(self) -> bool:
        """Check if we have permissions to use perf energy events."""
        try:
            base_cmd = ["sudo", "perf"] if self.use_sudo else ["perf"]
            cmd = base_cmd + [
                "stat",
                "--cpu", "0",
                "-e", self.REQUIRED_EVENTS[0],
                "--",
                "sleep", "0.1"
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=5)
            return result.returncode == 0

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            return False

    def is_available(self) -> bool:
        """Check if perf energy measurement is available."""
        # Check perf command
        if not self._check_perf_availability():
            logger.debug("Perf command not available")
            return False
        
        # Check RAPL events
        available_events = self._check_rapl_events()
        if not any(event in available_events for event in self.REQUIRED_EVENTS):
            logger.debug("Required RAPL events not available")
            return False
        
        # Check permissions
        if not self._check_permissions():
            logger.debug("Insufficient permissions for perf energy events")
            return False
        
        return True
    
    def setup(self) -> None:
        """Set up the perf energy meter."""
        if self._setup_complete:
            return
        
        # Check availability
        if not self.is_available():
            raise EnergyMeterNotAvailableError("Perf energy measurement not available")
        
        # Get available events
        self.available_events = self._check_rapl_events()
        
        # Ensure we have required events
        missing_required = [e for e in self.REQUIRED_EVENTS if e not in self.available_events]
        if missing_required:
            raise EnergyMeterNotAvailableError(f"Missing required RAPL events: {missing_required}")
        
        # Build base perf command
        self._perf_command_base = ["sudo", "perf"] if self.use_sudo else ["perf"]
        
        logger.info(f"Perf energy meter setup complete. Available events: {self.available_events}")
        self._setup_complete = True
    
    def cleanup(self) -> None:
        """Clean up perf energy meter resources."""
        # No persistent resources to clean up for perf
        self._setup_complete = False
    
    def _build_perf_command(self, events: List[str], cpu_core: int) -> List[str]:
        """Build perf command for energy measurement."""
        if not self._perf_command_base:
            raise EnergyMeterError("Perf meter not set up")
        
        cmd = list(self._perf_command_base)
        
        # Add stat command for measurement
        cmd.extend(["stat"])
        
        # Add CPU affinity
        cmd.extend(["--cpu", str(cpu_core)])
        
        # Add events
        if events:
            cmd.extend(["-e", ",".join(events)])
        
        # Output format (CSV for easier parsing)
        cmd.extend(["-x", ","])
        
        # Disable automatic scaling
        cmd.extend(["--"])
        
        return cmd
    
    def _parse_perf_output(self, output: str) -> Dict[str, float]:
        """
        Parse perf stat output to extract energy measurements.

        Expected CSV format:
        <value>,<unit>,<event_name>,<time_running>,<percentage>
        """
        energy_values = {}

        for line in output.strip().split('\n'):
            if not line or line.startswith('#'):
                continue

            parts = line.split(',')
            if len(parts) < 3:
                continue

            try:
                value_str = parts[0].strip()
                unit = parts[1].strip() if len(parts) > 1 else ""
                event_name = parts[2].strip()

                # Skip if no measurement
                if value_str == '<not counted>' or value_str == '<not supported>':
                    continue

                # Parse numeric value and normalize units to joules
                value = self._convert_to_joules(value_str, unit)
                if value is None:
                    continue

                # Map event name to our standard names
                if 'energy-pkg' in event_name:
                    energy_values['package_energy_joules'] = value
                elif 'energy-ram' in event_name:
                    energy_values['ram_energy_joules'] = value
                elif 'energy-cores' in event_name:
                    energy_values['core_energy_joules'] = value
                elif 'energy-gpu' in event_name:
                    energy_values['gpu_energy_joules'] = value

            except (ValueError, IndexError) as e:
                logger.debug(f"Failed to parse perf output line: {line}, error: {e}")
                continue

        return energy_values

    def _convert_to_joules(self, value_str: str, unit: str) -> Optional[float]:
        """Convert perf stat values to joules based on the reported unit."""
        try:
            value = float(value_str)
        except ValueError as exc:
            logger.debug("Invalid perf value '%s': %s", value_str, exc)
            return None

        unit_normalized = (unit or "").strip().lower()
        # Normalize common unicode symbols (e.g. micro sign) to ASCII for matching
        unit_normalized = unit_normalized.replace('\u03bc', 'u')
        if not unit_normalized:
            return value

        unit_multipliers = {
            'j': 1.0,
            'joule': 1.0,
            'joules': 1.0,
            'kj': 1e3,
            'kilojoule': 1e3,
            'kilojoules': 1e3,
            'mj': 1e-3,
            'millijoule': 1e-3,
            'millijoules': 1e-3,
            'uj': 1e-6,
            'microjoule': 1e-6,
            'microjoules': 1e-6,
            'nj': 1e-9,
            'nanojoule': 1e-9,
            'nanojoules': 1e-9,
        }

        multiplier = unit_multipliers.get(unit_normalized)
        if multiplier is None:
            # Try singular form without trailing 's'
            unit_singular = unit_normalized.rstrip('s')
            multiplier = unit_multipliers.get(unit_singular)

        if multiplier is None:
            logger.debug("Unknown perf energy unit '%s', assuming Joules", unit)
            multiplier = 1.0

        return value * multiplier
    
    def _create_measurement_script(self, 
                                  code: str, 
                                  function_name: str, 
                                  test_inputs: List[Any]) -> str:
        """Create a Python script for measurement execution."""
        script_template = """
import time
import sys

# Add safe imports for user code
from typing import List, Dict, Set, Tuple, Optional, Any, Union
import math, collections, itertools, functools, heapq, bisect, random

# User code
{user_code}

# Test inputs
test_inputs = {test_inputs!r}

# Function to call
function_name = "{function_name}"

# Get the function
if function_name not in globals():
    print(f"ERROR: Function '{{function_name}}' not found", file=sys.stderr)
    sys.exit(1)

target_function = globals()[function_name]

# Execute function on all test inputs
start_time = time.perf_counter()

try:
    for inputs in test_inputs:
        # Treat list/tuple as positional args even if length == 1
        if isinstance(inputs, (list, tuple)):
            result = target_function(*inputs)
        elif isinstance(inputs, dict):
            result = target_function(**inputs)
        else:
            result = target_function(inputs)
except Exception as e:
    print(f"ERROR: Execution failed: {{e}}", file=sys.stderr)
    sys.exit(1)

execution_time = time.perf_counter() - start_time
print(f"EXECUTION_TIME: {{execution_time}}", file=sys.stderr)
"""

        return script_template.format(
            user_code=code,
            test_inputs=test_inputs,
            function_name=function_name
        )
    
    def _measure_single_trial(self,
                             code: str,
                             function_name: str, 
                             test_inputs: List[Any],
                             cpu_core: int,
                             trial_number: int) -> EnergyMeasurement:
        """Perform a single energy measurement trial."""
        
        # Create measurement script
        script_content = self._create_measurement_script(code, function_name, test_inputs)
        
        # Write script to temporary file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as script_file:
            script_file.write(script_content)
            script_path = script_file.name
        
        try:
            # Build perf command
            perf_cmd = self._build_perf_command(self.available_events, cpu_core)
            perf_cmd.extend(["python3", script_path])
            
            # Execute with perf measurement
            start_time = time.perf_counter()
            
            result = subprocess.run(
                perf_cmd,
                capture_output=True,
                timeout=self.perf_timeout,
                text=True
            )
            
            wall_time = time.perf_counter() - start_time
            
            if result.returncode != 0:
                logger.error(
                    "Perf command failed (rc=%s) during trial %s: %s",
                    result.returncode,
                    trial_number,
                    result.stderr.strip()
                )
                raise EnergyMeterError(f"Perf measurement failed: {result.stderr.strip()}")
            
            # Parse perf output for energy values
            energy_values = self._parse_perf_output(result.stderr)
            
            # Extract execution time from script output
            execution_time = wall_time  # Default fallback
            for line in result.stderr.split('\n'):
                if line.startswith('EXECUTION_TIME:'):
                    try:
                        execution_time = float(line.split(':')[1].strip())
                        break
                    except (ValueError, IndexError):
                        pass
            
            # Create energy measurement
            return EnergyMeasurement(
                package_energy_joules=energy_values.get('package_energy_joules', 0.0),
                ram_energy_joules=energy_values.get('ram_energy_joules', 0.0),
                execution_time_seconds=execution_time,
                trial_number=trial_number,
                cpu_core=cpu_core,
                thermal_state="measured"  # Could be enhanced with actual thermal monitoring
            )
            
        except subprocess.TimeoutExpired:
            raise EnergyMeterTimeoutError(f"Perf measurement timed out after {self.perf_timeout}s")
        
        except subprocess.SubprocessError as e:
            raise EnergyMeterError(f"Perf subprocess error: {e}")
        
        finally:
            # Clean up temporary script file
            try:
                os.unlink(script_path)
            except OSError:
                pass
    
    def measure_execution(self,
                         executor: SafeCodeExecutor,
                         code: str,
                         function_name: str,
                         test_inputs: List[Any],
                         trials: int,
                         cpu_core: int) -> List[EnergyMeasurement]:
        """Measure energy consumption during code execution."""
        
        if not self._setup_complete:
            self.setup()
        
        logger.info(f"Starting perf energy measurement: {trials} trials on CPU {cpu_core}")
        
        measurements = []
        
        for trial in range(trials):
            try:
                measurement = self._measure_single_trial(
                    code, function_name, test_inputs, cpu_core, trial
                )
                measurements.append(measurement)
                
                logger.debug(f"Trial {trial + 1}/{trials}: "
                           f"Package={measurement.package_energy_joules:.6f}J, "
                           f"RAM={measurement.ram_energy_joules:.6f}J, "
                           f"Time={measurement.execution_time_seconds:.3f}s")
                
            except Exception as e:
                logger.error("Trial %s failed", trial + 1, exc_info=True)
                # Continue with remaining trials
                continue
        
        if not measurements:
            raise EnergyMeterError("All measurement trials failed")
        
        logger.info(f"Perf energy measurement complete: {len(measurements)}/{trials} successful trials")
        return measurements
    
    def get_environment_info(self) -> Dict[str, Any]:
        """Get environment information for measurement metadata."""
        info = {
            'meter_type': self.meter_type.value,
            'perf_version': self._get_perf_version(),
            'available_events': self.available_events,
            'use_sudo': self.use_sudo,
            'capabilities': [cap.value for cap in self.capabilities]
        }
        
        # Add CPU information
        try:
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
                
            # Extract CPU model
            model_match = re.search(r'model name\s*:\s*(.+)', cpuinfo)
            if model_match:
                info['cpu_model'] = model_match.group(1).strip()
                
        except (OSError, IOError):
            pass
        
        return info
    
    def _get_perf_version(self) -> Optional[str]:
        """Get perf command version."""
        try:
            cmd = ["sudo", "perf", "--version"] if self.use_sudo else ["perf", "--version"]
            result = subprocess.run(cmd, capture_output=True, timeout=5, text=True)
            
            if result.returncode == 0:
                return result.stdout.strip()
                
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass
        
        return None

# Register the perf meter in the global registry
from .interfaces import energy_meter_registry
energy_meter_registry.register(EnergyMeterType.PERF, PerfEnergyMeter)
