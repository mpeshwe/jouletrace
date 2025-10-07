# jouletrace/energy/meter_factory.py
from __future__ import annotations
import logging
import platform
import subprocess
from typing import Optional, Dict, Any, List
from pathlib import Path

from .interfaces import (
    EnergyMeter,
    EnergyMeterType, 
    EnergyMeterError,
    EnergyMeterNotAvailableError,
    energy_meter_registry
)
from .pcm_socket_meter import PCMSocketMeter

logger = logging.getLogger(__name__)

class EnergyMeterFactory:
    """
    Factory for creating and configuring energy meters.
    Handles system detection and optimal meter selection for production use.
    """
    
    def __init__(self):
        self._system_info: Optional[Dict[str, Any]] = None
        self._detection_cache: Dict[str, Any] = {}
    
    def _detect_system_capabilities(self) -> Dict[str, Any]:
        """Detect system energy measurement capabilities."""
        if self._system_info is not None:
            return self._system_info
        
        info = {
            'platform': platform.system(),
            'architecture': platform.machine(),
            'cpu_vendor': self._detect_cpu_vendor(),
            'rapl_available': self._check_rapl_support(),
            # Perf is deprecated in favor of per-socket RAPL path for Socket 0
            'perf_available': False,
            'permissions': self._check_energy_permissions(),
            'kernel_version': platform.release()
        }
        
        logger.info(f"System capabilities detected: {info}")
        self._system_info = info
        return info
    
    def _detect_cpu_vendor(self) -> Optional[str]:
        """Detect CPU vendor for RAPL support determination."""
        try:
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
            
            if 'GenuineIntel' in cpuinfo:
                return 'Intel'
            elif 'AuthenticAMD' in cpuinfo:
                return 'AMD'
            
        except (OSError, IOError):
            pass
        
        return None
    
    def _check_rapl_support(self) -> bool:
        """Check if RAPL energy monitoring is supported."""
        # Check for RAPL interface in sysfs
        rapl_path = Path('/sys/class/powercap/intel-rapl')
        if rapl_path.exists():
            return True
        
        # Check for RAPL events in perf
        try:
            result = subprocess.run(['perf', 'list'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and 'power/energy-pkg/' in result.stdout:
                return True
        except (subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass
        
        return False
    
    def _check_perf_availability(self) -> bool:
        """Check if perf command is available and functional."""
        try:
            result = subprocess.run(['perf', '--version'], capture_output=True, timeout=5)
            return result.returncode == 0
        except (subprocess.SubprocessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def _check_energy_permissions(self) -> Dict[str, bool]:
        """Check permissions for energy measurement."""
        permissions = {
            'perf_events': False,
            'rapl_sysfs': False,
            'sudo_available': False
        }
        
        # Check perf events permission
        try:
            result = subprocess.run(['perf', 'stat', 'sleep', '0.1'], 
                                  capture_output=True, timeout=5)
            permissions['perf_events'] = result.returncode == 0
        except (subprocess.SubprocessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        # Check RAPL sysfs access
        rapl_energy_file = Path('/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj')
        if rapl_energy_file.exists():
            try:
                rapl_energy_file.read_text()
                permissions['rapl_sysfs'] = True
            except PermissionError:
                pass
        
        # Check sudo availability
        try:
            result = subprocess.run(
                ['sudo', '-n', 'perf', '--version'],
                capture_output=True,
                timeout=5
            )
            permissions['sudo_available'] = result.returncode == 0
        except (subprocess.SubprocessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        return permissions
    
    def _validate_energy_measurement_environment(self) -> tuple[bool, List[str]]:
        """Validate that energy measurement can work on this system."""
        issues = []
        system_info = self._detect_system_capabilities()
        
        # Platform check
        if system_info['platform'] != 'Linux':
            issues.append(f"Unsupported platform: {system_info['platform']} (Linux required)")
        
        # Architecture check  
        if system_info['architecture'] not in ['x86_64', 'amd64']:
            issues.append(f"Unsupported architecture: {system_info['architecture']} (x86_64 required)")
        
        # CPU vendor check
        if system_info['cpu_vendor'] not in ['Intel', 'AMD']:
            issues.append(f"Unsupported CPU vendor: {system_info['cpu_vendor']} (Intel/AMD required for RAPL)")
        
        # RAPL support check
        if not system_info['rapl_available']:
            issues.append("RAPL energy monitoring not available (requires Intel/AMD processor with RAPL support)")

        # Permission checks: require readable RAPL sysfs, ignore perf
        permissions = system_info['permissions']
        if not permissions['rapl_sysfs']:
            issues.append("Insufficient permissions to read RAPL sysfs energy counters")
        
        return len(issues) == 0, issues
    
    def create_energy_meter(self, 
                           meter_type: Optional[EnergyMeterType] = None,
                           **kwargs) -> EnergyMeter:
        """
        Create an energy meter instance.
        
        Args:
            meter_type: Specific meter type to create, or None for auto-selection
            **kwargs: Additional configuration for the meter
            
        Returns:
            Configured and validated energy meter instance
            
        Raises:
            EnergyMeterNotAvailableError: If no suitable meter can be created
        """
        logger.info("Creating energy meter for production use")
        
        # Validate environment first
        environment_ok, issues = self._validate_energy_measurement_environment()
        if not environment_ok:
            error_msg = "Energy measurement environment validation failed:\n" + "\n".join(f"- {issue}" for issue in issues)
            logger.error(error_msg)
            raise EnergyMeterNotAvailableError(error_msg)
        
        # If specific meter type requested, create it
        if meter_type:
            return self._create_specific_meter(meter_type, **kwargs)
        
        # Auto-select best available meter
        return self._auto_select_meter(**kwargs)
    
    def _create_specific_meter(self, meter_type: EnergyMeterType, **kwargs) -> EnergyMeter:
        """Create a specific type of energy meter."""
        logger.info(f"Creating specific energy meter: {meter_type.value}")
        
        if meter_type == EnergyMeterType.PCM:
            return self._create_pcm_meter(**kwargs)
        else:
            raise EnergyMeterNotAvailableError(f"Unsupported meter type: {meter_type.value}")
    
    def _create_pcm_meter(self, **kwargs) -> PCMSocketMeter:
        """Create and configure a per-socket RAPL meter (PCM-style)."""
        meter = PCMSocketMeter(**kwargs)
        setup_ok, setup_error = meter.validate_setup()
        if not setup_ok:
            raise EnergyMeterNotAvailableError(setup_error)
        logger.info("PCM socket meter created and validated successfully")
        return meter
    
    def _auto_select_meter(self, **kwargs) -> EnergyMeter:
        """Automatically select the best available energy meter."""
        logger.info("Auto-selecting energy meter")
        
        system_info = self._detect_system_capabilities()
        
        # Prefer per-socket RAPL meter for Socket 0 architecture
        if system_info['rapl_available'] and system_info['permissions'].get('rapl_sysfs', False):
            logger.info("RAPL sysfs available, selecting PCM socket meter")
            return self._create_pcm_meter(**kwargs)
        
        # Future: Could add other meter types here (turbostat, Intel Power Gadget, etc.)
        
        raise EnergyMeterNotAvailableError("No suitable energy meter available on this system")
    
    def get_system_energy_info(self) -> Dict[str, Any]:
        """Get comprehensive information about system energy measurement capabilities."""
        system_info = self._detect_system_capabilities()
        
        # Add validation results
        environment_ok, issues = self._validate_energy_measurement_environment()
        
        return {
            'system_capabilities': system_info,
            'energy_measurement_ready': environment_ok,
            'validation_issues': issues,
            'recommended_meter': EnergyMeterType.PCM.value if environment_ok else None,
            'available_meter_types': [EnergyMeterType.PCM.value] if environment_ok else []
        }
    
    def diagnose_energy_measurement_issues(self) -> Dict[str, Any]:
        """Diagnose and provide solutions for energy measurement issues."""
        system_info = self._detect_system_capabilities()
        environment_ok, issues = self._validate_energy_measurement_environment()
        
        solutions = []
        
        if not system_info['perf_available']:
            solutions.append({
                'issue': 'Perf command not available',
                'solution': 'Install perf tools: sudo apt-get install linux-tools-$(uname -r) linux-tools-generic'
            })
        
        if not system_info['rapl_available']:
            solutions.append({
                'issue': 'RAPL energy monitoring not available',
                'solution': 'Ensure you have Intel/AMD processor with RAPL support and recent kernel (3.14+)'
            })
        
        permissions = system_info['permissions']
        if not permissions['perf_events'] and not permissions['sudo_available']:
            solutions.append({
                'issue': 'Insufficient permissions for perf events',
                'solution': 'Either: (1) sudo sysctl kernel.perf_event_paranoid=0, or (2) set up passwordless sudo for perf'
            })
        
        return {
            'system_info': system_info,
            'environment_ready': environment_ok,
            'issues': issues,
            'solutions': solutions,
            'next_steps': self._get_setup_next_steps(system_info, issues)
        }
    
    def _get_setup_next_steps(self, system_info: Dict[str, Any], issues: List[str]) -> List[str]:
        """Get specific next steps for setting up energy measurement."""
        if not issues:
            return ["Energy measurement ready - no action needed"]
        
        steps = []
        
        if 'linux-tools' in str(issues).lower():
            steps.append("1. Install perf tools: sudo apt-get install linux-tools-$(uname -r)")
        
        if 'permission' in str(issues).lower():
            if not system_info['permissions']['sudo_available']:
                steps.append("2. Set up sudo access or adjust perf_event_paranoid: sudo sysctl kernel.perf_event_paranoid=0")
            else:
                steps.append("2. Permissions look OK - will use sudo for perf commands")
        
        if 'rapl' in str(issues).lower():
            steps.append("3. Verify RAPL support: cat /sys/class/powercap/intel-rapl/intel-rapl:0/name")
        
        steps.append("4. Test setup: python -c \"from jouletrace.energy import EnergyMeterFactory; EnergyMeterFactory().create_energy_meter()\"")
        
        return steps

# Global factory instance
energy_meter_factory = EnergyMeterFactory()

# Convenience functions
def create_energy_meter(meter_type: Optional[EnergyMeterType] = None, **kwargs) -> EnergyMeter:
    """Create an energy meter using the global factory."""
    return energy_meter_factory.create_energy_meter(meter_type, **kwargs)

def get_system_energy_info() -> Dict[str, Any]:
    """Get system energy measurement information."""
    return energy_meter_factory.get_system_energy_info()

def diagnose_energy_setup() -> Dict[str, Any]:
    """Diagnose energy measurement setup issues."""
    return energy_meter_factory.diagnose_energy_measurement_issues()
