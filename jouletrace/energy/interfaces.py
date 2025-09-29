# jouletrace/energy/interfaces.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from enum import Enum
import logging

from ..core.models import EnergyMeasurement
from ..core.executor import SafeCodeExecutor

logger = logging.getLogger(__name__)

class EnergyMeterType(str, Enum):
    """Types of energy measurement backends."""
    PERF = "perf"                    # Linux perf with RAPL events
    TURBOSTAT = "turbostat"          # Intel turbostat utility
    MOCK = "mock"                    # Mock implementation for testing
    RUNTIME_ONLY = "runtime_only"    # Fallback with no energy measurement

class EnergyMeterCapability(str, Enum):
    """Capabilities that energy meters may support."""
    PACKAGE_ENERGY = "package_energy"        # CPU package energy measurement
    RAM_ENERGY = "ram_energy"                # RAM energy measurement  
    CORE_ENERGY = "core_energy"              # Per-core energy measurement
    GPU_ENERGY = "gpu_energy"                # GPU energy measurement
    FREQUENCY_SCALING = "frequency_scaling"  # CPU frequency control
    THERMAL_MONITORING = "thermal_monitoring" # Temperature monitoring

class EnergyMeterError(Exception):
    """Base exception for energy measurement errors."""
    pass

class EnergyMeterNotAvailableError(EnergyMeterError):
    """Raised when energy meter hardware/software is not available."""
    pass

class EnergyMeterPermissionError(EnergyMeterError):
    """Raised when energy meter lacks required permissions."""
    pass

class EnergyMeterTimeoutError(EnergyMeterError):
    """Raised when energy measurement times out."""
    pass

class EnergyMeter(ABC):
    """
    Abstract base class for energy measurement backends.
    
    Defines the contract that all energy meters must implement.
    Used by the JouleTrace pipeline for pluggable energy measurement.
    """
    
    @property
    @abstractmethod
    def meter_type(self) -> EnergyMeterType:
        """Get the type of this energy meter."""
        pass
    
    @property
    @abstractmethod
    def capabilities(self) -> List[EnergyMeterCapability]:
        """Get the capabilities supported by this meter."""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if this energy meter is available on the current system.
        
        Returns:
            True if the meter can be used, False otherwise.
        """
        pass
    
    @abstractmethod
    def setup(self) -> None:
        """
        Set up the energy meter for measurement.
        
        Raises:
            EnergyMeterNotAvailableError: If meter cannot be set up
            EnergyMeterPermissionError: If lacking required permissions
        """
        pass
    
    @abstractmethod
    def cleanup(self) -> None:
        """Clean up energy meter resources."""
        pass
    
    @abstractmethod
    def measure_execution(self, 
                         executor: SafeCodeExecutor,
                         code: str,
                         function_name: str,
                         test_inputs: List[Any],
                         trials: int,
                         cpu_core: int) -> List[EnergyMeasurement]:
        """
        Measure energy consumption during code execution.
        
        Args:
            executor: Safe code executor to use
            code: Code to execute
            function_name: Function to call in the code
            test_inputs: List of inputs to feed to the function
            trials: Number of measurement trials to perform
            cpu_core: CPU core to execute on
            
        Returns:
            List of energy measurements, one per trial
            
        Raises:
            EnergyMeterError: If measurement fails
            EnergyMeterTimeoutError: If measurement times out
        """
        pass
    
    @abstractmethod
    def get_environment_info(self) -> Dict[str, Any]:
        """
        Get information about the measurement environment.
        
        Returns:
            Dictionary containing environment details for result metadata.
        """
        pass
    
    def validate_setup(self) -> tuple[bool, str]:
        """
        Validate that the energy meter is properly set up.
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            if not self.is_available():
                return False, f"{self.meter_type.value} meter not available on this system"
            
            self.setup()
            return True, "Energy meter setup successful"
            
        except EnergyMeterNotAvailableError as e:
            return False, f"Energy meter not available: {e}"
        except EnergyMeterPermissionError as e:
            return False, f"Permission error: {e}"
        except Exception as e:
            return False, f"Setup error: {e}"
    
    def get_meter_info(self) -> Dict[str, Any]:
        """Get comprehensive information about this energy meter."""
        return {
            'type': self.meter_type.value,
            'capabilities': [cap.value for cap in self.capabilities],
            'available': self.is_available(),
            'class_name': self.__class__.__name__,
            'environment_info': self.get_environment_info() if self.is_available() else {}
        }

class EnergyMeterRegistry:
    """
    Registry for discovering and managing available energy meters.
    """
    
    def __init__(self):
        self._meters: Dict[EnergyMeterType, type] = {}
        self._instances: Dict[EnergyMeterType, EnergyMeter] = {}
    
    def register(self, meter_type: EnergyMeterType, meter_class: type) -> None:
        """Register an energy meter implementation."""
        if not issubclass(meter_class, EnergyMeter):
            raise ValueError(f"Meter class must inherit from EnergyMeter")
        
        self._meters[meter_type] = meter_class
        logger.info(f"Registered energy meter: {meter_type.value} -> {meter_class.__name__}")
    
    def get_meter(self, meter_type: EnergyMeterType, **kwargs) -> EnergyMeter:
        """Get an instance of the specified energy meter."""
        if meter_type not in self._meters:
            raise ValueError(f"Unknown energy meter type: {meter_type.value}")
        
        # Return cached instance if available
        if meter_type in self._instances:
            return self._instances[meter_type]
        
        # Create new instance
        meter_class = self._meters[meter_type]
        instance = meter_class(**kwargs)
        self._instances[meter_type] = instance
        
        return instance
    
    def get_available_meters(self) -> List[EnergyMeterType]:
        """Get list of available energy meter types on this system."""
        available = []
        
        for meter_type, meter_class in self._meters.items():
            try:
                # Create temporary instance to check availability
                temp_instance = meter_class()
                if temp_instance.is_available():
                    available.append(meter_type)
            except Exception as e:
                logger.debug(f"Energy meter {meter_type.value} not available: {e}")
        
        return available
    
    def get_best_available_meter(self) -> Optional[EnergyMeter]:
        """
        Get the best available energy meter on this system.
        
        Preference order: PERF > TURBOSTAT > RUNTIME_ONLY > MOCK
        """
        preference_order = [
            EnergyMeterType.PERF,
            EnergyMeterType.TURBOSTAT, 
            EnergyMeterType.RUNTIME_ONLY,
            EnergyMeterType.MOCK
        ]
        
        for meter_type in preference_order:
            if meter_type in self._meters:
                try:
                    meter = self.get_meter(meter_type)
                    if meter.is_available():
                        logger.info(f"Selected energy meter: {meter_type.value}")
                        return meter
                except Exception as e:
                    logger.debug(f"Failed to create {meter_type.value} meter: {e}")
        
        logger.warning("No energy meters available")
        return None
    
    def get_registry_info(self) -> Dict[str, Any]:
        """Get information about the meter registry."""
        return {
            'registered_meters': list(self._meters.keys()),
            'available_meters': self.get_available_meters(),
            'cached_instances': list(self._instances.keys())
        }

# Global registry instance
energy_meter_registry = EnergyMeterRegistry()

# Utility functions for common operations
def get_energy_meter(meter_type: Optional[EnergyMeterType] = None, **kwargs) -> Optional[EnergyMeter]:
    """
    Get an energy meter instance.
    
    Args:
        meter_type: Specific meter type to get, or None for best available
        **kwargs: Additional arguments for meter initialization
        
    Returns:
        EnergyMeter instance or None if not available
    """
    if meter_type:
        try:
            return energy_meter_registry.get_meter(meter_type, **kwargs)
        except Exception as e:
            logger.warning(f"Failed to get {meter_type.value} meter: {e}")
            return None
    else:
        return energy_meter_registry.get_best_available_meter()

def list_available_meters() -> List[str]:
    """Get list of available energy meter type names."""
    return [meter.value for meter in energy_meter_registry.get_available_meters()]

def validate_energy_meter(meter: EnergyMeter) -> tuple[bool, str]:
    """Validate that an energy meter is ready for use."""
    return meter.validate_setup()