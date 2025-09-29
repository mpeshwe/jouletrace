# jouletrace/core/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict, Union
from enum import Enum
import time
from uuid import UUID, uuid4

class MeasurementStatus(str, Enum):
    SUCCESS = "success"
    INCORRECT_SOLUTION = "incorrect_solution"
    EXECUTION_ERROR = "execution_error" 
    TIMEOUT = "timeout"
    ENERGY_MEASUREMENT_FAILED = "energy_measurement_failed"
    VALIDATION_ERROR = "validation_error"

@dataclass
class JouleTraceTestCase:
    """
    Standard test case format for JouleTrace.
    Flexible input format to support different problem types.
    """
    inputs: Any                              # Function arguments (any format)
    expected_output: Any                     # Expected result
    test_id: str                            # Unique identifier for this test
    metadata: Dict[str, Any] = field(default_factory=dict)  # Optional test-specific data
    
    def __str__(self) -> str:
        return f"TestCase[{self.test_id}]({self.inputs} → {self.expected_output})"

@dataclass
class ValidationResult:
    """Result of validating candidate solution against test cases."""
    is_correct: bool
    passed_tests: int
    total_tests: int
    failed_test_details: List[Dict[str, Any]] = field(default_factory=list)
    execution_errors: List[str] = field(default_factory=list)
    actual_outputs: List[Any] = field(default_factory=list)
    expected_outputs: List[Any] = field(default_factory=list)
    
    @property
    def pass_rate(self) -> float:
        """Percentage of tests passed."""
        if self.total_tests == 0:
            return 0.0
        return (self.passed_tests / self.total_tests) * 100.0
    
    @property
    def summary(self) -> str:
        """Human-readable validation summary."""
        if self.is_correct:
            return f"All {self.total_tests} tests passed"
        
        return f"Failed {self.total_tests - self.passed_tests}/{self.total_tests} tests"

@dataclass
class EnergyMeasurement:
    """Raw energy measurement from a single trial."""
    package_energy_joules: float
    ram_energy_joules: float
    execution_time_seconds: float
    trial_number: int
    
    # Measurement environment details
    cpu_core: int
    cpu_frequency_mhz: Optional[float] = None
    thermal_state: str = "unknown"           # "baseline", "elevated", etc.
    
    @property
    def total_energy_joules(self) -> float:
        return self.package_energy_joules + self.ram_energy_joules

@dataclass
class CPUIsolationConfig:
    """Configuration for fair multi-core energy measurement."""
    
    # Core isolation
    measurement_core: int = 0                       # Dedicated core for measurement
    isolate_other_processes: bool = True            # Move other processes away
    
    # Thermal management
    thermal_baseline_wait_seconds: float = 5.0     # Wait between measurements
    thermal_monitoring: bool = True                 # Monitor temperature during measurement
    
    # CPU control
    disable_frequency_scaling: bool = True          # Lock CPU frequency
    disable_turbo_boost: bool = True               # Disable dynamic frequency changes
    disable_hyperthreading: bool = False           # Disable SMT if supported
    
    # Advanced options
    memory_binding: Optional[str] = None           # NUMA memory policy
    process_priority: int = -10                    # Real-time priority for measurement

@dataclass
class JouleTraceMeasurementRequest:
    """
    Standard request format for JouleTrace energy measurement.
    Dataset-agnostic interface.
    """
    
    # Core request
    candidate_code: str                            # Code to measure energy consumption
    test_cases: List[JouleTraceTestCase]          # Standard format test cases
    function_name: str = "solve"                   # Function to call in candidate_code
    
    # Execution settings
    timeout_seconds: int = 30                      # Per-trial timeout
    memory_limit_mb: int = 512                    # Memory limit per execution
    
    # Measurement settings  
    energy_measurement_trials: int = 5             # Number of energy measurement trials
    warmup_trials: int = 2                        # Warmup runs before measurement
    
    # CPU isolation for fair comparison
    cpu_config: CPUIsolationConfig = field(default_factory=CPUIsolationConfig)
    
    # Request metadata
    request_id: UUID = field(default_factory=uuid4)
    candidate_id: Optional[str] = None             # External tracking ID
    problem_name: Optional[str] = None             # Problem identifier
    
    def validate(self) -> tuple[bool, str]:
        """Validate request parameters."""
        if not self.candidate_code.strip():
            return False, "Empty candidate code"
        
        if not self.test_cases:
            return False, "No test cases provided"
        
        if self.energy_measurement_trials < 1:
            return False, "Energy measurement trials must be >= 1"
        
        if self.timeout_seconds < 1:
            return False, "Timeout must be >= 1 second"
        
        return True, "Request validation passed"

@dataclass
class JouleTraceMeasurementResult:
    """
    Complete result of JouleTrace energy measurement.
    Contains validation results and energy measurements (if solution was correct).
    """
    
    # Request tracking
    request_id: UUID

    # Validation results (always present)
    validation: ValidationResult

    # Optional metadata
    candidate_id: Optional[str] = None
    problem_name: Optional[str] = None
    measurement_timestamp: float = field(default_factory=time.time)
    
    # Energy measurements (only if validation.is_correct)
    energy_measurements: List[EnergyMeasurement] = field(default_factory=list)
    
    # Aggregated energy metrics (only if energy measurements exist)
    median_package_energy_joules: float = 0.0
    median_ram_energy_joules: float = 0.0
    median_execution_time_seconds: float = 0.0
    median_total_energy_joules: float = 0.0
    
    # Energy efficiency metrics
    energy_per_test_case_joules: float = 0.0      # Total energy / number of test cases
    power_consumption_watts: float = 0.0          # Average power during execution
    
    # Measurement status and diagnostics
    status: MeasurementStatus = MeasurementStatus.SUCCESS
    measurement_environment: Dict[str, Any] = field(default_factory=dict)
    error_details: Optional[str] = None
    
    @property
    def has_energy_data(self) -> bool:
        """Whether energy measurements were successfully captured."""
        return (len(self.energy_measurements) > 0 and 
                self.validation.is_correct)
    
    @property
    def success(self) -> bool:
        """Whether the complete measurement was successful."""
        return (self.status == MeasurementStatus.SUCCESS and
                self.validation.is_correct and
                self.has_energy_data)
    
    @property 
    def failure_reason(self) -> str:
        """Human-readable reason for measurement failure."""
        if self.success:
            return "Measurement completed successfully"
        
        if not self.validation.is_correct:
            return f"Solution validation failed: {self.validation.summary}"
        
        if self.status != MeasurementStatus.SUCCESS:
            error = self.error_details or "Unknown error"
            return f"Measurement failed ({self.status.value}): {error}"
        
        return "Energy measurement data unavailable"
    
    def summary_stats(self) -> Dict[str, float]:
        """Key energy efficiency metrics for comparison."""
        if not self.has_energy_data:
            return {}
        
        return {
            "total_energy_joules": self.median_total_energy_joules,
            "package_energy_joules": self.median_package_energy_joules,
            "ram_energy_joules": self.median_ram_energy_joules,
            "execution_time_seconds": self.median_execution_time_seconds,
            "energy_per_test_case_joules": self.energy_per_test_case_joules,
            "power_consumption_watts": self.power_consumption_watts,
            "energy_efficiency_score": self.energy_per_test_case_joules,  # Lower is better
        }

# Utility types for input handling
InputArgs = Union[Any, List[Any], Dict[str, Any]]  # Flexible function argument format

@dataclass
class ComparisonConfig:
    """Configuration for comparing expected vs actual outputs."""
    float_tolerance: float = 1e-9                 # Absolute tolerance for floating point
    relative_tolerance: float = 1e-9              # Relative tolerance for floating point  
    string_case_sensitive: bool = True            # Case sensitivity for string comparison
    ignore_whitespace: bool = False               # Whether to ignore whitespace differences
    list_order_matters: bool = True               # Whether order matters in list comparison
