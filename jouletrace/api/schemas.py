# jouletrace/api/schemas.py
from __future__ import annotations
from typing import List, Optional, Dict, Any, Union, Literal
from uuid import UUID
try:
    from pydantic import BaseModel, Field, field_validator
except ImportError:  # pydantic v1
    from pydantic import BaseModel, Field, validator as field_validator  # type: ignore
from enum import Enum

from ..core.models import MeasurementStatus, CPUIsolationConfig

class APIStatus(str, Enum):
    """API-specific status codes."""
    QUEUED = "queued"
    RUNNING = "running" 
    COMPLETED = "completed"
    FAILED = "failed"

# Request Models
class TestCaseRequest(BaseModel):
    """Single test case in API request format."""
    inputs: Any = Field(description="Function inputs (flexible format)")
    expected_output: Any = Field(description="Expected function output")
    test_id: str = Field(description="Unique identifier for this test case")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Optional test metadata")

class EnergyMeasurementRequest(BaseModel):
    """HTTP request for energy measurement."""
    
    # Core execution parameters
    candidate_code: str = Field(description="Code to measure energy consumption")
    test_cases: List[TestCaseRequest] = Field(description="Test cases for validation and measurement")
    function_name: str = Field(default="solve", description="Function to call in candidate code")
    
    # Execution limits
    timeout_seconds: int = Field(default=30, ge=1, le=300, description="Execution timeout per trial")
    memory_limit_mb: int = Field(default=512, ge=64, le=8192, description="Memory limit per execution")
    
    # Measurement settings
    energy_measurement_trials: int = Field(default=5, ge=1, le=20, description="Number of energy measurement trials")
    warmup_trials: int = Field(default=2, ge=0, le=10, description="Number of warmup trials")
    
    # CPU isolation (optional, will use defaults if not provided)
    cpu_core: Optional[int] = Field(default=None, ge=0, description="Specific CPU core to use")
    thermal_wait_seconds: Optional[float] = Field(default=5.0, ge=0, le=60, description="Thermal baseline wait time")
    
    # Request metadata
    candidate_id: Optional[str] = Field(default=None, description="External candidate identifier")
    problem_name: Optional[str] = Field(default=None, description="Problem identifier")
    
    @field_validator('test_cases')
    @classmethod
    def validate_test_cases_not_empty(cls, v):
        if not v:
            raise ValueError("At least one test case must be provided")
        return v

class QuickValidationRequest(BaseModel):
    """Request for quick validation without energy measurement."""
    candidate_code: str = Field(description="Code to validate")
    test_cases: List[TestCaseRequest] = Field(description="Test cases for validation")
    function_name: str = Field(default="solve", description="Function to call")
    timeout_seconds: int = Field(default=10, ge=1, le=60, description="Validation timeout")

# Response Models
class ValidationSummary(BaseModel):
    """Summary of solution validation results."""
    is_correct: bool = Field(description="Whether solution passed all tests")
    passed_tests: int = Field(description="Number of tests passed")
    total_tests: int = Field(description="Total number of tests")
    pass_rate: float = Field(description="Percentage of tests passed")
    error_summary: Optional[str] = Field(default=None, description="Summary of errors if validation failed")

class EnergyMetricsSummary(BaseModel):
    """Summary of energy measurement results."""
    median_package_energy_joules: float = Field(description="Median package energy consumption")
    median_ram_energy_joules: float = Field(description="Median RAM energy consumption") 
    median_total_energy_joules: float = Field(description="Median total energy consumption")
    median_execution_time_seconds: float = Field(description="Median execution time")
    energy_per_test_case_joules: float = Field(description="Energy consumption per test case")
    power_consumption_watts: float = Field(description="Average power consumption")
    energy_efficiency_score: float = Field(description="Energy efficiency metric (lower is better)")

class MeasurementEnvironmentInfo(BaseModel):
    """Information about measurement environment."""
    meter_type: str = Field(description="Type of energy meter used")
    cpu_model: Optional[str] = Field(default=None, description="CPU model")
    measurement_core: Optional[int] = Field(default=None, description="CPU core used for measurement")
    thermal_controlled: bool = Field(description="Whether thermal control was used")
    timestamp: float = Field(description="Measurement timestamp")

class EnergyMeasurementResponse(BaseModel):
    """Complete energy measurement response."""
    
    # Request tracking
    request_id: UUID = Field(description="Unique request identifier")
    candidate_id: Optional[str] = Field(default=None, description="External candidate identifier")
    problem_name: Optional[str] = Field(default=None, description="Problem identifier")
    status: Literal["completed"] = Field(description="Measurement status")
    
    # Validation results
    validation: ValidationSummary = Field(description="Solution validation summary")
    
    # Energy measurement results (only if validation passed)
    energy_metrics: Optional[EnergyMetricsSummary] = Field(default=None, description="Energy measurement summary")
    measurement_environment: Optional[MeasurementEnvironmentInfo] = Field(default=None, description="Measurement environment")
    
    # Processing metadata
    processing_time_seconds: float = Field(description="Total processing time")
    measurement_timestamp: float = Field(description="When measurement was completed")

class TaskQueuedResponse(BaseModel):
    """Response when measurement task is queued."""
    task_id: str = Field(description="Celery task identifier")
    request_id: UUID = Field(description="Request identifier")
    status: Literal["queued"] = Field(description="Task status")
    estimated_completion_seconds: Optional[int] = Field(default=None, description="Estimated completion time")
    poll_url: str = Field(description="URL to poll for results")

class TaskRunningResponse(BaseModel):
    """Response when measurement task is in progress."""
    task_id: str = Field(description="Celery task identifier") 
    request_id: UUID = Field(description="Request identifier")
    status: Literal["running"] = Field(description="Task status")
    stage: Optional[str] = Field(default=None, description="Current processing stage")
    progress: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Progress percentage")
    elapsed_seconds: float = Field(description="Time elapsed since task started")

class TaskFailedResponse(BaseModel):
    """Response when measurement task failed."""
    task_id: str = Field(description="Celery task identifier")
    request_id: UUID = Field(description="Request identifier") 
    status: Literal["failed"] = Field(description="Task status")
    error_type: str = Field(description="Type of error that occurred")
    error_message: str = Field(description="Detailed error message")
    failure_timestamp: float = Field(description="When the task failed")

# Union type for polling responses
TaskStatusResponse = Union[TaskQueuedResponse, TaskRunningResponse, EnergyMeasurementResponse, TaskFailedResponse]

class QuickValidationResponse(BaseModel):
    """Response for quick validation requests."""
    request_id: UUID = Field(description="Request identifier")
    validation: ValidationSummary = Field(description="Validation results")
    processing_time_seconds: float = Field(description="Validation processing time")
    
# System Status and Health Check Models
class SystemHealthResponse(BaseModel):
    """System health check response."""
    status: Literal["healthy", "degraded", "unhealthy"] = Field(description="Overall system status")
    timestamp: float = Field(description="Health check timestamp")
    version: str = Field(description="JouleTrace version")
    
    # Component status
    energy_meter_available: bool = Field(description="Whether energy measurement is available")
    energy_meter_type: Optional[str] = Field(default=None, description="Type of energy meter")
    celery_worker_count: int = Field(description="Number of active Celery workers")
    redis_connected: bool = Field(description="Redis connection status")
    
    # Performance metrics
    active_tasks: int = Field(description="Number of active measurement tasks")
    completed_tasks_24h: int = Field(description="Tasks completed in last 24 hours")
    average_measurement_time_seconds: Optional[float] = Field(default=None, description="Average measurement time")

class SystemCapabilitiesResponse(BaseModel):
    """System capabilities and configuration response."""
    
    # Energy measurement capabilities
    energy_measurement_available: bool = Field(description="Whether energy measurement is supported")
    supported_meter_types: List[str] = Field(description="Available energy meter types")
    cpu_isolation_supported: bool = Field(description="Whether CPU isolation is supported")
    thermal_monitoring_supported: bool = Field(description="Whether thermal monitoring is supported")
    
    # System information
    platform: str = Field(description="Operating system platform")
    cpu_vendor: Optional[str] = Field(default=None, description="CPU vendor")
    cpu_cores: int = Field(description="Number of CPU cores")
    available_memory_gb: float = Field(description="Available system memory")
    
    # Limits and constraints
    max_concurrent_measurements: int = Field(description="Maximum concurrent measurements")
    max_measurement_timeout_seconds: int = Field(description="Maximum allowed timeout")
    max_memory_limit_mb: int = Field(description="Maximum memory limit per measurement")

# Error Response Models
class APIErrorResponse(BaseModel):
    """Standard error response format."""
    error: str = Field(description="Error type")
    message: str = Field(description="Human-readable error message")
    details: Optional[Dict[str, Any]] = Field(default=None, description="Additional error details")
    timestamp: float = Field(description="Error timestamp")
    request_id: Optional[str] = Field(default=None, description="Request ID if available")

class ValidationErrorResponse(BaseModel):
    """Validation error response with field-specific details."""
    error: Literal["validation_error"] = Field(description="Error type")
    message: str = Field(description="Validation error summary")
    field_errors: List[Dict[str, str]] = Field(description="Field-specific validation errors")
    timestamp: float = Field(description="Error timestamp")

# Utility models for API documentation
class APIExampleTestCase(BaseModel):
    """Example test case for API documentation."""
    inputs: Any = Field(example=[[1, 3], [2, 4]], description="Example function inputs")
    expected_output: Any = Field(example=2.5, description="Example expected output")
    test_id: str = Field(example="test_001", description="Example test identifier")

class APIExampleRequest(BaseModel):
    """Example request for API documentation."""
    candidate_code: str = Field(
        example="""def solve(nums1, nums2):
    # Find median of two sorted arrays
    merged = sorted(nums1 + nums2)
    n = len(merged)
    if n % 2 == 0:
        return (merged[n//2-1] + merged[n//2]) / 2.0
    else:
        return float(merged[n//2])""",
        description="Example candidate solution"
    )
    test_cases: List[APIExampleTestCase] = Field(
        example=[
            {"inputs": [[1, 3], [2, 4]], "expected_output": 2.5, "test_id": "test_001"},
            {"inputs": [[1, 2], [3, 4]], "expected_output": 2.5, "test_id": "test_002"}
        ],
        description="Example test cases"
    )
    function_name: str = Field(example="solve", description="Function to call")
    energy_measurement_trials: int = Field(example=5, description="Number of measurement trials")
