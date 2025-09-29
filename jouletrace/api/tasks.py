# jouletrace/api/tasks.py
from __future__ import annotations
import time
import logging
import traceback
from typing import Dict, Any, List
from uuid import UUID

from celery import Celery, Task
from celery.signals import worker_ready, worker_shutting_down

from ..core.models import (
    JouleTraceMeasurementRequest, 
    JouleTraceTestCase,
    MeasurementStatus
)
from ..core.pipeline import JouleTracePipeline
from ..energy.meter_factory import create_energy_meter
from ..infrastructure.config import get_config

logger = logging.getLogger(__name__)

# Celery app configuration
config = get_config()
celery_app = Celery(
    "jouletrace",
    broker=config.celery.broker_url,
    backend=config.celery.result_backend
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    
    # Task routing and execution
    task_default_queue="energy_measurement",
    task_default_exchange="energy_measurement",
    task_default_routing_key="energy_measurement",
    
    # Task timeouts and retries (from config)
    task_soft_time_limit=config.celery.task_soft_time_limit,
    task_time_limit=config.celery.task_time_limit,
    task_acks_late=True,       # Acknowledge tasks after completion
    worker_prefetch_multiplier=1,  # Process one task at a time for energy isolation
    
    # Result backend settings
    result_expires=86400,      # Keep results for 24 hours
    result_backend_transport_options={
        "master_name": "mymaster",
        "visibility_timeout": 3600,
    },
    
    # Worker settings for energy measurement
    worker_disable_rate_limits=True,
    worker_max_tasks_per_child=50,  # Restart workers periodically
    
    # Monitoring and debugging
    task_track_started=True,
    task_send_sent_event=True,
    worker_send_task_events=True,
    task_reject_on_worker_lost=True,
)

class EnergyMeasurementTask(Task):
    """
    Custom base task class for energy measurement with proper resource management.
    """
    
    def __init__(self):
        self._pipeline: JouleTracePipeline = None
        self._energy_meter = None
    
    @property
    def pipeline(self) -> JouleTracePipeline:
        """Lazy-loaded pipeline instance."""
        if self._pipeline is None:
            try:
                # Create energy meter
                self._energy_meter = create_energy_meter(
                    use_sudo=config.energy.use_sudo,
                    perf_timeout=config.energy.perf_timeout
                )
                if self._energy_meter is None:
                    logger.warning("No energy meter available, using runtime-only mode")
                
                # Create pipeline
                self._pipeline = JouleTracePipeline(energy_meter=self._energy_meter)
                logger.info(f"Energy measurement pipeline initialized with meter: {type(self._energy_meter).__name__ if self._energy_meter else 'None'}")
                
            except Exception as e:
                logger.error(f"Failed to initialize energy measurement pipeline: {e}", exc_info=True)
                raise
        
        return self._pipeline
    
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Handle task failure with detailed logging."""
        logger.error(f"Energy measurement task {task_id} failed: {exc}", exc_info=True)
        
        # Extract request info for better error reporting
        request_id = kwargs.get("request_id", "unknown")
        logger.error(f"Task failure details - RequestID: {request_id}, TaskID: {task_id}")
    
    def on_success(self, retval, task_id, args, kwargs):
        """Handle successful task completion."""
        request_id = kwargs.get("request_id", "unknown")
        processing_time = retval.get("processing_time_seconds", 0)
        logger.info(f"Energy measurement task {task_id} completed successfully - "
                   f"RequestID: {request_id}, ProcessingTime: {processing_time:.2f}s")

@celery_app.task(bind=True, base=EnergyMeasurementTask, name="measure_energy_task")
def measure_energy_task(self, request_data: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    """
    Celery task for energy measurement.
    
    Args:
        request_data: Serialized EnergyMeasurementRequest data
        request_id: Unique request identifier
        
    Returns:
        Serialized EnergyMeasurementResponse data
    """
    start_time = time.time()
    
    try:
        # Update task state to STARTED
        self.update_state(
            state="STARTED",
            meta={
                "request_id": request_id,
                "stage": "initializing", 
                "progress": 0.0,
                "elapsed_seconds": 0.0
            }
        )
        
        logger.info(f"Starting energy measurement task for request {request_id}")
        
        # Convert request data to internal format
        internal_request = _convert_api_request_to_internal(request_data, request_id)
        
        # Update progress
        self.update_state(
            state="STARTED",
            meta={
                "request_id": request_id,
                "stage": "validation",
                "progress": 0.1,
                "elapsed_seconds": time.time() - start_time
            }
        )
        
        # Perform energy measurement using pipeline
        measurement_result = self.pipeline.measure_energy(internal_request)
        
        # Update progress 
        self.update_state(
            state="STARTED", 
            meta={
                "request_id": request_id,
                "stage": "finalizing",
                "progress": 0.9,
                "elapsed_seconds": time.time() - start_time
            }
        )
        
        # Convert internal result to API response format
        api_response = _convert_internal_result_to_api(measurement_result, start_time)
        
        logger.info(f"Energy measurement completed for request {request_id} - "
                   f"Status: {measurement_result.status.value}, "
                   f"Correct: {measurement_result.validation.is_correct}, "
                   f"HasEnergy: {measurement_result.has_energy_data}")
        
        return api_response
        
    except Exception as e:
        error_message = str(e)
        error_type = type(e).__name__
        
        logger.error(f"Energy measurement task failed for request {request_id}: {error_message}", exc_info=True)
        
        # Return structured error information
        return {
            "request_id": request_id,
            "status": "failed", 
            "error_type": error_type,
            "error_message": error_message,
            "processing_time_seconds": time.time() - start_time,
            "measurement_timestamp": time.time(),
            "traceback": traceback.format_exc()
        }

def _convert_api_request_to_internal(request_data: Dict[str, Any], request_id: str) -> JouleTraceMeasurementRequest:
    """Convert API request format to internal JouleTrace format."""
    
    # Convert test cases
    test_cases = []
    for tc_data in request_data["test_cases"]:
        test_case = JouleTraceTestCase(
            inputs=tc_data["inputs"],
            expected_output=tc_data["expected_output"],
            test_id=tc_data["test_id"],
            metadata=tc_data.get("metadata", {})
        )
        test_cases.append(test_case)
    
    # Build CPU isolation config
    from ..core.models import CPUIsolationConfig
    cpu_config = CPUIsolationConfig(
        measurement_core=request_data.get("cpu_core", 0),
        thermal_baseline_wait_seconds=request_data.get("thermal_wait_seconds", 5.0),
        isolate_other_processes=True,
        disable_frequency_scaling=True
    )
    
    # Create internal request
    # Fallback to service defaults from config if caller didn't provide limits
    cfg = get_config()
    timeout_default = cfg.energy.default_timeout
    mem_default = cfg.energy.default_memory_limit
    internal_request = JouleTraceMeasurementRequest(
        candidate_code=request_data["candidate_code"],
        test_cases=test_cases,
        function_name=request_data.get("function_name", "solve"),
        timeout_seconds=request_data.get("timeout_seconds", timeout_default),
        memory_limit_mb=request_data.get("memory_limit_mb", mem_default),
        energy_measurement_trials=request_data.get("energy_measurement_trials", 5),
        warmup_trials=request_data.get("warmup_trials", 2),
        cpu_config=cpu_config,
        request_id=UUID(request_id),
        candidate_id=request_data.get("candidate_id"),
        problem_name=request_data.get("problem_name")
    )
    
    return internal_request

def _convert_internal_result_to_api(result, start_time: float) -> Dict[str, Any]:
    """Convert internal measurement result to API response format."""
    
    from ..api.schemas import ValidationSummary, EnergyMetricsSummary, MeasurementEnvironmentInfo
    
    if result.validation is None:
        raise ValueError(result.error_details or "Validation results unavailable")

    # Convert validation result
    validation = ValidationSummary(
        is_correct=result.validation.is_correct,
        passed_tests=result.validation.passed_tests,
        total_tests=result.validation.total_tests,
        pass_rate=result.validation.pass_rate,
        error_summary=result.validation.summary if not result.validation.is_correct else None
    )
    
    # Convert energy metrics (only if available)
    energy_metrics = None
    if result.has_energy_data:
        energy_metrics = EnergyMetricsSummary(
            median_package_energy_joules=result.median_package_energy_joules,
            median_ram_energy_joules=result.median_ram_energy_joules,
            median_total_energy_joules=result.median_total_energy_joules,
            median_execution_time_seconds=result.median_execution_time_seconds,
            energy_per_test_case_joules=result.energy_per_test_case_joules,
            power_consumption_watts=result.power_consumption_watts,
            energy_efficiency_score=result.energy_per_test_case_joules  # Lower is better
        )
    
    # Convert measurement environment
    measurement_env = None
    if result.measurement_environment:
        env_info = result.measurement_environment
        measurement_env = MeasurementEnvironmentInfo(
            meter_type=env_info.get("energy_meter", {}).get("name", "unknown"),
            cpu_model=env_info.get("cpu_isolation", {}).get("cpu_topology", {}).get("cpu_model"),
            measurement_core=env_info.get("cpu_isolation", {}).get("isolation_config", {}).get("measurement_core"),
            thermal_controlled=env_info.get("cpu_isolation", {}).get("isolation_config", {}).get("thermal_control", False),
            timestamp=result.measurement_timestamp
        )
    
    # Build API response
    is_success = result.status == MeasurementStatus.SUCCESS and result.has_energy_data

    api_response = {
        "request_id": str(result.request_id),
        "candidate_id": result.candidate_id,
        "problem_name": result.problem_name,
        "status": "completed" if is_success else "failed",
        "validation": validation.model_dump(),
        "processing_time_seconds": time.time() - start_time,
        "measurement_timestamp": result.measurement_timestamp
    }

    if not is_success:
        api_response["error_type"] = result.status.value
        api_response["error_message"] = result.error_details or "Energy measurement failed"
    
    # Add energy metrics if available
    if energy_metrics:
        api_response["energy_metrics"] = energy_metrics.model_dump()
    
    # Add measurement environment if available  
    if measurement_env:
        api_response["measurement_environment"] = measurement_env.model_dump()
    
    return api_response

@celery_app.task(bind=True, name="health_check_task")
def health_check_task(self) -> Dict[str, Any]:
    """Task for performing worker health checks."""
    
    try:
        # Test energy meter availability
        energy_meter = create_energy_meter()
        energy_available = energy_meter is not None
        
        # Test basic pipeline functionality
        pipeline = JouleTracePipeline(energy_meter=energy_meter)
        pipeline_ready = pipeline is not None
        
        return {
            "timestamp": time.time(),
            "worker_id": self.request.id,
            "energy_meter_available": energy_available,
            "energy_meter_type": type(energy_meter).__name__ if energy_meter else None,
            "pipeline_ready": pipeline_ready,
            "status": "healthy" if energy_available and pipeline_ready else "degraded"
        }
        
    except Exception as e:
        logger.error(f"Health check task failed: {e}", exc_info=True)
        return {
            "timestamp": time.time(),
            "worker_id": self.request.id,
            "status": "unhealthy",
            "error": str(e)
        }

# Worker lifecycle event handlers
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Handle worker ready event."""
    logger.info(f"Celery worker {sender} is ready for energy measurement tasks")
    
    try:
        # Initialize energy meter to validate setup
        energy_meter = create_energy_meter()
        if energy_meter:
            logger.info(f"Energy meter initialized: {type(energy_meter).__name__}")
        else:
            logger.warning("No energy meter available - will run in runtime-only mode")
    except Exception as e:
        logger.error(f"Failed to initialize energy meter: {e}", exc_info=True)

@worker_shutting_down.connect  
def worker_shutting_down_handler(sender=None, **kwargs):
    """Handle worker shutdown event."""
    logger.info(f"Celery worker {sender} is shutting down")
    
    # Clean up any resources if needed
    try:
        # Energy meters should clean up automatically, but we can add explicit cleanup here
        pass
    except Exception as e:
        logger.error(f"Error during worker shutdown: {e}", exc_info=True)

# Task routing configuration
celery_app.conf.task_routes = {
    "measure_energy_task": {"queue": "energy_measurement"},
    "health_check_task": {"queue": "health_checks"},
}

# Additional Celery configuration for production
celery_app.conf.update(
    # Worker configuration
    worker_concurrency=1,  # Only one measurement at a time for energy isolation
    worker_max_memory_per_child=2000000,  # 2GB memory limit per worker
    
    # Task configuration
    task_compression="gzip",
    result_compression="gzip",
    
    # Monitoring
    worker_send_task_events=True,
    task_send_sent_event=True,
    
    # Error handling
    task_reject_on_worker_lost=True,
    task_acks_late=True,
)
