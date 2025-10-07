"""
jouletrace/energy/pcm_socket_meter.py

PCM-based energy meter with per-socket energy attribution.
This is the foundation for the single-socket isolation architecture.

PCM provides instantaneous energy counter readings per socket, allowing us to:
1. Read Socket 0 energy before execution
2. Execute code on Socket 0
3. Read Socket 0 energy after execution
4. Calculate net energy consumed

This differs from PERF which wraps execution and can't separate socket contributions.
"""

from __future__ import annotations
import os
import time
import subprocess
import tempfile
import re
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass
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


@dataclass
class SocketTopology:
    """CPU socket topology information."""
    socket_count: int
    socket_to_cpus: Dict[int, List[int]]  # socket_id -> [cpu_ids]
    cpu_to_socket: Dict[int, int]  # cpu_id -> socket_id


@dataclass
class PCMReading:
    """Single PCM energy counter reading."""
    socket_id: int
    package_energy_joules: float
    dram_energy_joules: float
    timestamp: float


class PCMSocketMeter(EnergyMeter):
    """
    Energy meter using Intel PCM for per-socket energy measurement.
    
    This meter provides accurate per-socket energy attribution, essential for
    the single-socket isolation architecture where Socket 0 runs measurements
    and Socket 1 runs infrastructure.
    
    Key features:
    - Read energy counters independently for each socket
    - No execution wrapping required
    - Clean energy attribution
    - Low measurement overhead
    """
    
    def __init__(self, use_sudo: bool = True, pcm_timeout: int = 10):
        """
        Initialize PCM socket meter.
        
        Args:
            use_sudo: Whether to use sudo for PCM (usually required)
            pcm_timeout: Timeout for PCM commands in seconds
        """
        self.use_sudo = use_sudo
        self.pcm_timeout = pcm_timeout
        self.pcm_path: Optional[Path] = None
        self.topology: Optional[SocketTopology] = None
        self._setup_complete = False
        
        # Cache for PCM readings to avoid excessive subprocess calls
        self._last_reading_cache: Dict[int, PCMReading] = {}
        self._cache_timestamp = 0.0
        self._cache_max_age = 0.1  # 100ms cache to avoid reading same counters too fast
    
    @property
    def meter_type(self) -> EnergyMeterType:
        return EnergyMeterType.PCM
    
    @property
    def capabilities(self) -> List[EnergyMeterCapability]:
        return [
            EnergyMeterCapability.PACKAGE_ENERGY,
            EnergyMeterCapability.RAM_ENERGY,
        ]
    
    def _find_pcm_binary(self) -> Optional[Path]:
        """Find PCM binary on the system."""
        # Common PCM installation paths
        common_paths = [
            Path("/usr/sbin/pcm"),
            Path("/usr/local/bin/pcm"),
            Path("/usr/bin/pcm"),
            Path.home() / "pcm/build/bin/pcm",
        ]
        
        for path in common_paths:
            if path.exists() and path.is_file():
                return path
        
        # Try which/whereis
        try:
            result = subprocess.run(
                ["which", "pcm"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                pcm_path = Path(result.stdout.strip())
                if pcm_path.exists():
                    return pcm_path
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass
        
        return None
    
    def _detect_socket_topology(self) -> SocketTopology:
        """
        Detect CPU socket topology from /sys filesystem.
        
        Returns:
            SocketTopology with socket to CPU mapping
        """
        socket_to_cpus: Dict[int, List[int]] = {}
        cpu_to_socket: Dict[int, int] = {}
        
        cpu_base = Path("/sys/devices/system/cpu")
        
        # Scan all CPU directories
        for cpu_dir in sorted(cpu_base.glob("cpu[0-9]*")):
            try:
                cpu_id = int(cpu_dir.name[3:])
                
                # Read socket ID from topology
                socket_file = cpu_dir / "topology" / "physical_package_id"
                if not socket_file.exists():
                    continue
                
                socket_id = int(socket_file.read_text().strip())
                
                # Build mappings
                if socket_id not in socket_to_cpus:
                    socket_to_cpus[socket_id] = []
                socket_to_cpus[socket_id].append(cpu_id)
                cpu_to_socket[cpu_id] = socket_id
                
            except (ValueError, OSError) as e:
                logger.debug(f"Failed to read topology for {cpu_dir}: {e}")
                continue
        
        socket_count = len(socket_to_cpus)
        
        logger.info(f"Detected {socket_count} CPU sockets")
        for socket_id, cpus in socket_to_cpus.items():
            logger.info(f"  Socket {socket_id}: {len(cpus)} CPUs - {cpus[:5]}...")
        
        return SocketTopology(
            socket_count=socket_count,
            socket_to_cpus=socket_to_cpus,
            cpu_to_socket=cpu_to_socket
        )
    
    def _check_pcm_permissions(self) -> bool:
        """Check if we have permissions to run PCM."""
        if not self.pcm_path:
            return False
        
        try:
            cmd = ["sudo", str(self.pcm_path)] if self.use_sudo else [str(self.pcm_path)]
            cmd.extend(["--version"])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
            
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return False
    
    def is_available(self) -> bool:
        """Check if per-socket RAPL is available via sysfs.

        We do not require the external `pcm` binary. Availability is determined by
        presence and readability of `/sys/class/powercap/intel-rapl/.../energy_uj`
        and a detectable CPU socket topology.
        """
        # Detect topology first (from /sys)
        try:
            topology = self._detect_socket_topology()
            if topology.socket_count == 0:
                logger.debug("No CPU sockets detected")
                return False
            self.topology = topology
        except Exception as e:
            logger.debug(f"Failed to detect socket topology: {e}")
            return False

        # Ensure RAPL sysfs is present and readable for at least socket 0
        rapl_base = Path("/sys/class/powercap/intel-rapl")
        rapl_socket0 = rapl_base / "intel-rapl:0" / "energy_uj"
        if not rapl_socket0.exists():
            logger.debug("RAPL sysfs energy counter not found for socket 0")
            return False
        try:
            _ = rapl_socket0.read_text().strip()
        except PermissionError:
            logger.debug("No permission to read RAPL energy counter")
            return False
        except OSError as e:
            logger.debug(f"Failed to read RAPL energy counter: {e}")
            return False

        # Optional: record pcm path if present, but do not require it
        self.pcm_path = self._find_pcm_binary()
        return True
    
    def setup(self) -> None:
        """Set up PCM socket meter."""
        if self._setup_complete:
            return
        
        # Verify availability
        if not self.is_available():
            raise EnergyMeterNotAvailableError("PCM (per-socket RAPL) not available via sysfs")
        
        # Verify we have required topology info
        if not self.topology or self.topology.socket_count == 0:
            raise EnergyMeterNotAvailableError("Could not detect CPU socket topology")
        
        # Test reading energy counters
        try:
            for socket_id in range(self.topology.socket_count):
                reading = self._read_socket_energy(socket_id)
                logger.debug(f"Socket {socket_id} test reading: {reading.package_energy_joules:.3f}J")
        except Exception as e:
            raise EnergyMeterNotAvailableError(f"Failed to read PCM energy counters: {e}")
        
        logger.info(f"PCM socket meter setup complete. {self.topology.socket_count} sockets detected.")
        self._setup_complete = True
    
    def cleanup(self) -> None:
        """Clean up PCM resources."""
        self._setup_complete = False
        self._last_reading_cache.clear()
    
    def _read_socket_energy(self, socket_id: int) -> PCMReading:
        """
        Read current energy counter for a specific socket.
        
        Args:
            socket_id: Socket to read (0 or 1)
            
        Returns:
            PCMReading with current counter values
        """
        # Check cache
        now = time.time()
        if socket_id in self._last_reading_cache:
            cached = self._last_reading_cache[socket_id]
            if now - self._cache_timestamp < self._cache_max_age:
                return cached
        
        # Read RAPL directly from sysfs (faster than running pcm)
        # PCM actually reads from /sys/class/powercap/intel-rapl/intel-rapl:N/energy_uj
        rapl_base = Path("/sys/class/powercap/intel-rapl")
        rapl_socket = rapl_base / f"intel-rapl:{socket_id}"
        
        if not rapl_socket.exists():
            raise EnergyMeterError(f"RAPL interface not found for socket {socket_id}")
        
        try:
            # Read package energy (in microjoules)
            energy_file = rapl_socket / "energy_uj"
            if not energy_file.exists():
                raise EnergyMeterError(f"Energy counter not found: {energy_file}")
            
            energy_uj = int(energy_file.read_text().strip())
            package_joules = energy_uj / 1_000_000.0
            
            # Read DRAM energy if available
            dram_energy_file = rapl_socket / "intel-rapl:0:0" / "energy_uj"
            if dram_energy_file.exists():
                dram_uj = int(dram_energy_file.read_text().strip())
                dram_joules = dram_uj / 1_000_000.0
            else:
                dram_joules = 0.0
            
            reading = PCMReading(
                socket_id=socket_id,
                package_energy_joules=package_joules,
                dram_energy_joules=dram_joules,
                timestamp=now
            )
            
            # Update cache
            self._last_reading_cache[socket_id] = reading
            self._cache_timestamp = now
            
            return reading
            
        except (OSError, ValueError) as e:
            raise EnergyMeterError(f"Failed to read RAPL counters: {e}")
    
    def get_socket_energy(self, socket_id: int) -> float:
        """
        Get current cumulative energy for a socket.
        
        This is the main API for external callers.
        Returns the current counter value - you must take differences yourself.
        
        Args:
            socket_id: Socket to read (0 or 1)
            
        Returns:
            Cumulative package energy in joules
        """
        reading = self._read_socket_energy(socket_id)
        return reading.package_energy_joules
    
    def get_cpu_socket(self, cpu_id: int) -> int:
        """
        Get socket ID for a given CPU core.
        
        Args:
            cpu_id: CPU core number
            
        Returns:
            Socket ID (0 or 1)
            
        Raises:
            ValueError: If CPU ID is invalid
        """
        if not self.topology:
            raise EnergyMeterError("Topology not initialized")
        
        if cpu_id not in self.topology.cpu_to_socket:
            raise ValueError(f"CPU {cpu_id} not found in topology")
        
        return self.topology.cpu_to_socket[cpu_id]
    
    def _create_measurement_script(self,
                                  code: str,
                                  function_name: str,
                                  test_inputs: List[Any]) -> str:
        """Create Python script for measurement execution."""
        script_template = """
import time
import sys

# Safe imports for user code
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
        """
        Perform single energy measurement trial.
        
        This is the core measurement logic:
        1. Read Socket 0 energy before
        2. Execute code on Socket 0
        3. Read Socket 0 energy after
        4. Calculate delta
        """
        # Determine which socket this CPU belongs to
        socket_id = self.get_cpu_socket(cpu_core)
        
        # Create measurement script
        script_content = self._create_measurement_script(code, function_name, test_inputs)
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script_content)
            script_path = f.name
        
        try:
            # Clear cache to force fresh read
            if socket_id in self._last_reading_cache:
                del self._last_reading_cache[socket_id]
            
            # Read energy BEFORE execution
            energy_before = self._read_socket_energy(socket_id)
            
            # Small delay to ensure counter has time to update
            time.sleep(0.01)
            
            # Execute code with CPU pinning
            # Use taskset to pin to specific core
            cmd = ["taskset", "-c", str(cpu_core), "python3", script_path]
            
            start_time = time.perf_counter()
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.pcm_timeout,
                text=True
            )
            
            wall_time = time.perf_counter() - start_time
            
            # Read energy AFTER execution
            # Clear cache first to force fresh read
            if socket_id in self._last_reading_cache:
                del self._last_reading_cache[socket_id]
            
            time.sleep(0.01)  # Small delay
            energy_after = self._read_socket_energy(socket_id)
            
            if result.returncode != 0:
                raise EnergyMeterError(f"Execution failed: {result.stderr}")
            
            # Extract execution time from script
            execution_time = wall_time
            for line in result.stderr.split('\n'):
                if line.startswith('EXECUTION_TIME:'):
                    try:
                        execution_time = float(line.split(':')[1].strip())
                    except (ValueError, IndexError):
                        pass
            
            # Calculate energy delta
            package_energy = energy_after.package_energy_joules - energy_before.package_energy_joules
            dram_energy = energy_after.dram_energy_joules - energy_before.dram_energy_joules
            
            # Handle counter rollover (RAPL counters are 32-bit and can wrap)
            # Max counter value is ~4,294 joules, rollover adds 2^32 microjoules
            if package_energy < 0:
                package_energy += (2**32 / 1_000_000.0)
            if dram_energy < 0:
                dram_energy += (2**32 / 1_000_000.0)
            
            return EnergyMeasurement(
                package_energy_joules=package_energy,
                ram_energy_joules=dram_energy,
                execution_time_seconds=execution_time,
                trial_number=trial_number,
                cpu_core=cpu_core,
                thermal_state="measured"
            )
            
        except subprocess.TimeoutExpired:
            raise EnergyMeterTimeoutError(f"Execution timed out after {self.pcm_timeout}s")
        except subprocess.SubprocessError as e:
            raise EnergyMeterError(f"Subprocess error: {e}")
        finally:
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
        
        logger.info(f"Starting PCM energy measurement: {trials} trials on CPU {cpu_core}")
        
        measurements = []
        
        for trial in range(trials):
            try:
                measurement = self._measure_single_trial(
                    code, function_name, test_inputs, cpu_core, trial
                )
                measurements.append(measurement)
                
                logger.debug(
                    f"Trial {trial + 1}/{trials}: "
                    f"Package={measurement.package_energy_joules:.6f}J, "
                    f"DRAM={measurement.ram_energy_joules:.6f}J, "
                    f"Time={measurement.execution_time_seconds:.3f}s"
                )
                
                # Brief cooldown between trials
                time.sleep(0.3)
                
            except Exception as e:
                logger.error(f"Trial {trial + 1} failed: {e}")
                continue
        
        if not measurements:
            raise EnergyMeterError("All measurement trials failed")
        
        logger.info(f"PCM measurement complete: {len(measurements)}/{trials} successful")
        return measurements
    
    def get_environment_info(self) -> Dict[str, Any]:
        """Get environment information for measurement metadata."""
        info = {
            'meter_type': self.meter_type.value,
            'pcm_path': str(self.pcm_path) if self.pcm_path else None,
            'socket_count': self.topology.socket_count if self.topology else 0,
            'capabilities': [cap.value for cap in self.capabilities]
        }
        
        if self.topology:
            info['socket_topology'] = {
                f'socket_{sid}': cpus
                for sid, cpus in self.topology.socket_to_cpus.items()
            }
        
        # Add CPU model
        try:
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
            model_match = re.search(r'model name\s*:\s*(.+)', cpuinfo)
            if model_match:
                info['cpu_model'] = model_match.group(1).strip()
        except (OSError, IOError):
            pass
        
        return info


# Register PCM meter
from .interfaces import energy_meter_registry, EnergyMeterType as EMT
energy_meter_registry.register(EMT.PCM, PCMSocketMeter)
