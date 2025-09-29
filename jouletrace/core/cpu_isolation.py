# jouletrace/core/cpu_isolation.py
from __future__ import annotations
import os
import time
import psutil
import subprocess
from typing import List, Dict, Optional, Set
from dataclasses import dataclass
import logging
from pathlib import Path

from .models import CPUIsolationConfig

logger = logging.getLogger(__name__)

@dataclass
class CPUTopology:
    """CPU topology information for intelligent core selection."""
    total_cores: int
    physical_cores: int
    logical_cores: int
    numa_nodes: List[int]
    core_siblings: Dict[int, List[int]]  # Hyperthreading pairs
    cache_sharing: Dict[int, List[int]]  # Cores sharing cache levels
    
class ThermalMonitor:
    """Monitor CPU thermal state for fair energy measurements."""
    
    def __init__(self):
        self.thermal_paths = self._find_thermal_sensors()
    
    def _find_thermal_sensors(self) -> List[Path]:
        """Find available CPU thermal sensors."""
        thermal_base = Path("/sys/class/thermal")
        sensors = []
        
        if not thermal_base.exists():
            return sensors
        
        for thermal_zone in thermal_base.glob("thermal_zone*"):
            try:
                thermal_type = (thermal_zone / "type").read_text().strip()
                if any(keyword in thermal_type.lower() for keyword in ["cpu", "core", "pkg", "x86"]):
                    sensors.append(thermal_zone / "temp")
            except (OSError, IOError):
                continue
        
        return sensors
    
    def get_cpu_temperature(self) -> Optional[float]:
        """Get current CPU temperature in Celsius."""
        if not self.thermal_paths:
            return None
        
        temperatures = []
        for sensor_path in self.thermal_paths:
            try:
                temp_millicelsius = int(sensor_path.read_text().strip())
                temperatures.append(temp_millicelsius / 1000.0)
            except (OSError, IOError, ValueError):
                continue
        
        return max(temperatures) if temperatures else None
    
    def wait_for_thermal_baseline(self, 
                                 target_temp: float = 60.0,
                                 max_wait_seconds: float = 30.0,
                                 check_interval: float = 1.0) -> bool:
        """
        Wait for CPU temperature to reach baseline before measurement.
        
        Args:
            target_temp: Target temperature threshold in Celsius
            max_wait_seconds: Maximum time to wait
            check_interval: How often to check temperature
            
        Returns:
            True if baseline reached, False if timeout
        """
        if not self.thermal_paths:
            logger.warning("No thermal sensors available, skipping thermal baseline wait")
            return True
        
        start_time = time.time()
        
        while (time.time() - start_time) < max_wait_seconds:
            current_temp = self.get_cpu_temperature()
            
            if current_temp is None:
                logger.warning("Could not read CPU temperature")
                return False
            
            if current_temp <= target_temp:
                logger.info(f"Thermal baseline reached: {current_temp:.1f}°C <= {target_temp:.1f}°C")
                return True
            
            logger.debug(f"Waiting for thermal baseline: {current_temp:.1f}°C > {target_temp:.1f}°C")
            time.sleep(check_interval)
        
        logger.warning(f"Thermal baseline wait timeout after {max_wait_seconds}s")
        return False

class CPUIsolationManager:
    """
    Manages CPU core isolation for fair energy measurements.
    Addresses the multi-core fairness issue you raised.
    """
    
    def __init__(self, config: CPUIsolationConfig):
        self.config = config
        self.topology = self._detect_cpu_topology()
        self.thermal_monitor = ThermalMonitor()
        self.original_affinity: Optional[Set[int]] = None
        self.isolated_processes: List[int] = []
    
    def _detect_cpu_topology(self) -> CPUTopology:
        """Detect CPU topology for intelligent core selection."""
        
        # Basic CPU information
        cpu_count = psutil.cpu_count(logical=True)
        physical_count = psutil.cpu_count(logical=False)
        
        # NUMA topology
        numa_nodes = []
        numa_path = Path("/sys/devices/system/node")
        if numa_path.exists():
            for node_dir in numa_path.glob("node*"):
                try:
                    node_id = int(node_dir.name[4:])
                    numa_nodes.append(node_id)
                except ValueError:
                    continue
        
        # Core sibling detection (hyperthreading)
        core_siblings = {}
        siblings_path = Path("/sys/devices/system/cpu")
        if siblings_path.exists():
            for cpu_dir in siblings_path.glob("cpu[0-9]*"):
                try:
                    cpu_id = int(cpu_dir.name[3:])
                    siblings_file = cpu_dir / "topology" / "thread_siblings_list"
                    if siblings_file.exists():
                        siblings = [int(x) for x in siblings_file.read_text().strip().split(',')]
                        core_siblings[cpu_id] = siblings
                except (ValueError, OSError):
                    continue
        
        # Cache topology (simplified)
        cache_sharing = {}
        for cpu_id in range(cpu_count):
            cache_sharing[cpu_id] = [cpu_id]  # Default: each core separate
        
        return CPUTopology(
            total_cores=cpu_count,
            physical_cores=physical_count or cpu_count,
            logical_cores=cpu_count,
            numa_nodes=numa_nodes,
            core_siblings=core_siblings,
            cache_sharing=cache_sharing
        )
    
    def _select_optimal_measurement_core(self) -> int:
        """
        Select the best CPU core for energy measurement.
        Considers interference, cache topology, and thermal characteristics.
        """
        # If user specified a core, validate and use it
        if hasattr(self.config, 'measurement_core') and self.config.measurement_core is not None:
            if 0 <= self.config.measurement_core < self.topology.total_cores:
                return self.config.measurement_core
            else:
                logger.warning(f"Invalid measurement core {self.config.measurement_core}, auto-selecting")
        
        # Selection strategy: prefer physical cores over hyperthreads
        # Avoid core 0 (often handles system interrupts)
        candidate_cores = list(range(1, self.topology.total_cores))
        
        # Prefer cores without hyperthreading siblings if available
        isolated_cores = []
        for core in candidate_cores:
            siblings = self.topology.core_siblings.get(core, [core])
            if len(siblings) == 1:  # No hyperthreading sibling
                isolated_cores.append(core)
        
        if isolated_cores:
            selected = isolated_cores[0]
            logger.info(f"Selected isolated core {selected} for measurement")
            return selected
        
        # Fall back to first available core
        selected = candidate_cores[0]
        logger.info(f"Selected core {selected} for measurement (no isolated cores available)")
        return selected
    
    def _set_process_affinity(self, pid: int, cpu_cores: List[int]) -> bool:
        """Set CPU affinity for a specific process."""
        try:
            process = psutil.Process(pid)
            process.cpu_affinity(cpu_cores)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as e:
            logger.warning(f"Failed to set affinity for process {pid}: {e}")
            return False
    
    def _isolate_other_processes(self, measurement_core: int) -> None:
        """Move other processes away from the measurement core."""
        if not self.config.isolate_other_processes:
            return
        
        # Get list of all other cores
        other_cores = [i for i in range(self.topology.total_cores) if i != measurement_core]
        if not other_cores:
            logger.warning("Cannot isolate processes: only one CPU core available")
            return
        
        # Get current process list
        try:
            current_processes = list(psutil.process_iter(['pid', 'cpu_affinity']))
        except psutil.Error as e:
            logger.warning(f"Failed to enumerate processes: {e}")
            return
        
        moved_count = 0
        for proc_info in current_processes:
            try:
                pid = proc_info.info['pid']
                current_affinity = proc_info.info.get('cpu_affinity', [])
                
                # Skip if process is not using the measurement core
                if measurement_core not in current_affinity:
                    continue
                
                # Skip kernel threads (usually can't change affinity)
                if pid <= 2:
                    continue
                
                # Try to move process to other cores
                if self._set_process_affinity(pid, other_cores):
                    self.isolated_processes.append(pid)
                    moved_count += 1
                    
            except (psutil.NoSuchProcess, KeyError):
                continue
        
        logger.info(f"Moved {moved_count} processes away from measurement core {measurement_core}")
    
    def _disable_frequency_scaling(self) -> bool:
        """Disable CPU frequency scaling for consistent measurements."""
        if not self.config.disable_frequency_scaling:
            return True
        
        try:
            # Try to set performance governor
            cpufreq_path = Path("/sys/devices/system/cpu/cpufreq")
            if cpufreq_path.exists():
                for policy_dir in cpufreq_path.glob("policy*"):
                    governor_file = policy_dir / "scaling_governor"
                    if governor_file.exists():
                        try:
                            governor_file.write_text("performance")
                        except PermissionError:
                            logger.warning(f"Permission denied setting governor for {policy_dir}")
                            return False
                
                logger.info("Set CPU governor to 'performance' mode")
                return True
            else:
                logger.warning("CPU frequency scaling control not available")
                return False
                
        except Exception as e:
            logger.warning(f"Failed to disable frequency scaling: {e}")
            return False
    
    def setup_isolation(self) -> int:
        """
        Set up CPU isolation for fair energy measurement.
        
        Returns:
            The CPU core number selected for measurement.
        """
        logger.info("Setting up CPU isolation for energy measurement")
        
        # Store original process affinity
        try:
            self.original_affinity = set(psutil.Process().cpu_affinity())
        except psutil.Error:
            self.original_affinity = None
        
        # Select measurement core
        measurement_core = self._select_optimal_measurement_core()
        
        # Set up CPU frequency control
        self._disable_frequency_scaling()
        
        # Isolate other processes
        self._isolate_other_processes(measurement_core)
        
        # Set our process affinity to measurement core
        if not self._set_process_affinity(os.getpid(), [measurement_core]):
            logger.warning("Failed to set measurement process affinity")
        
        logger.info(f"CPU isolation setup complete, using core {measurement_core}")
        return measurement_core
    
    def wait_thermal_baseline(self) -> bool:
        """Wait for thermal baseline before measurement."""
        if not self.config.thermal_baseline_wait_seconds:
            return True
        
        return self.thermal_monitor.wait_for_thermal_baseline(
            max_wait_seconds=self.config.thermal_baseline_wait_seconds
        )
    
    def cleanup_isolation(self) -> None:
        """Clean up CPU isolation settings."""
        logger.info("Cleaning up CPU isolation")
        
        # Restore original process affinity if possible
        if self.original_affinity:
            try:
                psutil.Process().cpu_affinity(list(self.original_affinity))
            except psutil.Error as e:
                logger.warning(f"Failed to restore original affinity: {e}")
        
        # Note: We don't restore other processes' affinity or CPU governor
        # as this could interfere with other system operations
        
        self.isolated_processes.clear()
    
    def get_isolation_info(self) -> Dict[str, any]:
        """Get information about current isolation setup."""
        return {
            "cpu_topology": {
                "total_cores": self.topology.total_cores,
                "physical_cores": self.topology.physical_cores,
                "numa_nodes": self.topology.numa_nodes,
            },
            "isolation_config": {
                "measurement_core": getattr(self.config, 'measurement_core', None),
                "isolate_processes": self.config.isolate_other_processes,
                "thermal_control": self.config.thermal_baseline_wait_seconds > 0,
                "frequency_scaling_disabled": self.config.disable_frequency_scaling,
            },
            "current_state": {
                "isolated_processes": len(self.isolated_processes),
                "current_temperature": self.thermal_monitor.get_cpu_temperature(),
            }
        }