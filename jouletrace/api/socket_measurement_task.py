"""
Socket 0 measurement task for Celery.

New measurement task using SocketExecutor and StatisticalAggregator
with Redis-based serialization lock for Socket 0.
"""

import time
import logging
from typing import Dict, Any, List
from uuid import UUID
import redis

from celery import Task

from .tasks import celery_app
from ..core.socket_executor import SocketExecutor, ExecutionResult
from ..core.statistical_aggregator import StatisticalAggregator, AggregatedResult
from ..core.validator import SolutionValidator, ValidationConfig
from ..core.models import (
    JouleTraceMeasurementRequest,
    JouleTraceTestCase,
    MeasurementStatus
)
from ..infrastructure.config import get_config

logger = logging.getLogger(__name__)


class Socket0Lock:
    """
    Redis-based lock for serializing Socket 0 access.
    
    Ensures only one measurement runs on Socket 0 at a time,
    even with multiple Celery workers.
    """
    
    def __init__(self, redis_url: str, lock_timeout: int = 300):
        """
        Args:
            redis_url: Redis connection URL
            lock_timeout: Lock timeout in seconds (default: 5 minutes)
        """
        self.redis_client = redis.from_url(redis_url, decode_responses=True)
        self.lock_name = "jouletrace:socket0:lock"
        self.lock_timeout = lock_timeout
        self._lock = None
    
    def acquire(self, blocking: bool = True, timeout: int = 60) -> bool:
        """
        Acquire Socket 0 lock.
        
        Args:
            blocking: Wait for lock if not available
            timeout: Maximum wait time in seconds
            
        Returns:
            True if lock acquired, False otherwise
        """
        if blocking:
            # Try to acquire with timeout
            start_time = time.time()
            while time.time() - start_time < timeout:
                acquired = self.redis_client.set(
                    self.lock_name,
                    "locked",
                    nx=True,  # Only set if not exists
                    ex=self.lock_timeout  # Auto-expire
                )
                if acquired:
                    logger.info("Acquired Socket 0 lock")
                    return True
                
                # Wait before retry
                time.sleep(0.5)
            
            logger.warning(f"Failed to acquire Socket 0 lock after {timeout}s")
            return False
        else:
            # Non-blocking acquire
            acquired = self.redis_client.set(
                self.lock_name,
                "locked",
                nx=True,
                ex=self.lock_timeout
            )
            return bool(acquired)
    
    def release(self) -> None:
        """Release Socket 0 lock."""
        try:
            self.redis_client.delete(self.lock_name)
            logger.info("Released Socket 0 lock")
        except Exception as e:
            logger.error(f"Failed to release Socket 0 lock: {e}")
    
    def __enter__(self):
        """Context manager entry."""
        if not self.acquire(blocking=True, timeout=60):
            raise RuntimeError("Failed to acquire Socket 0 lock")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.release()
        return False


class SocketMeasurementTask(Task):
    """
    Custom task class for Socket 0 measurements.
    
    Manages SocketExecutor and StatisticalAggregator lifecycle.
    """
    
    def __init__(self):
        self._executor: SocketExecutor = None
        self._aggregator: StatisticalAggregator = None
        self._validator: SolutionValidator = None
        self._lock: Socket0Lock = None
    
    @property
    def executor(self) -> SocketExecutor:
        """Lazy-loaded executor."""
        if self._executor is None:
            config = get_config()
            self._executor = SocketExecutor(
                socket_id=0,
                cpu_core=4,  # Socket 0 CPU
                timeout_seconds=30
            )
            logger.info("SocketExecutor initialized")
        return self._executor
    
    @property
    def aggregator(self) -> StatisticalAggregator:
        """Lazy-loaded aggregator."""
        if self._aggregator is None:
            self._aggregator = StatisticalAggregator(
                min_trials=3,
                max_trials=20,
                target_cv_percent=5.0,
                early_stop_enabled=True,
                cooldown_seconds=0.5
            )
            self._aggregator.setup(self.executor)
            logger.info("StatisticalAggregator initialized")
        return self._aggregator
    
    @property
    def validator(self) -> SolutionValidator:
        """Lazy-loaded validator."""
        if self._validator is None:
            self._validator = SolutionValidator()
            logger.info("SolutionValidator initialized")
        return self._validator
    
    @property
    def lock(self) -> Socket0Lock:
        """Lazy-loaded Socket 0 lock."""
        if self._lock is None:
            config = get_config()
            self._lock = Socket0Lock(
                redis_url=config.celery.broker_url,
                lock_timeout=300
            )
            logger.info("Socket0Lock initialized")
        return self._lock
    
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Handle task failure."""
        logger.error(f"Socket measurement task {task_id} failed: {exc}", exc_info=True)
        
        # Ensure lock is released
        if self._lock:
            try:
                self._lock.release()
            except:
                pass
    
    def on_success(self, retval, task_id, args, kwargs):
        """Handle task success."""
        request_id = kwargs.get("request_id", "unknown")
        logger.info(f"Socket measurement task {task_id} succeeded - RequestID: {request_id}")


@celery_app.task(bind=True, base=SocketMeasurementTask, name="socket_measurement_task")
def socket_measurement_task(self, request_data: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    """
    Celery task for Socket 0 energy measurement.
    
    Uses new SocketExecutor + StatisticalAggregator with Redis lock.
    
    Args:
        request_data: Serialized measurement request
        request_id: Unique request identifier
        
    Returns:
        Serialized measurement response
    """
    start_time = time.time()
    
    try:
        # Update state
        self.update_state(
            state="STARTED",
            meta={
                "request_id": request_id,
                "stage": "initializing",
                "progress": 0.0
            }
        )
        
        logger.info(f"Starting Socket 0 measurement for request {request_id}")
        
        # Parse request
        internal_request = _parse_request(request_data, request_id)
        
        # Update progress
        self.update_state(
            state="STARTED",
            meta={
                "request_id": request_id,
                "stage": "validation",
                "progress": 0.1
            }
        )
        
        # Step 1: Validate solution correctness
        validation_result = _validate_solution(
            self.validator,
            internal_request
        )
        
        if not validation_result['is_correct']:
            logger.info(f"Solution validation failed for {request_id}")
            return _build_validation_only_response(
                request_id,
                internal_request,
                validation_result,
                start_time
            )
        
        logger.info(f"Solution validation passed for {request_id}")
        
        # Update progress
        self.update_state(
            state="STARTED",
            meta={
                "request_id": request_id,
                "stage": "acquiring_socket0_lock",
                "progress": 0.3
            }
        )
        
        # Step 2: Acquire Socket 0 lock and measure
        with self.lock:
            logger.info(f"Socket 0 lock acquired for {request_id}")
            
            # Update progress
            self.update_state(
                state="STARTED",
                meta={
                    "request_id": request_id,
                    "stage": "energy_measurement",
                    "progress": 0.4
                }
            )
            
            # Setup executor
            self.executor.setup()
            
            # Step 3: Run aggregated measurements
            measurement_result = self.aggregator.aggregate_measurements(
                code=internal_request.candidate_code,
                function_name=internal_request.function_name,
                test_inputs=_extract_test_inputs(internal_request),
                verbose=False  # Don't print to logs in production
            )
            
            # Cleanup executor
            self.executor.cleanup()
        
        logger.info(f"Socket 0 lock released for {request_id}")
        
        # Update progress
        self.update_state(
            state="STARTED",
            meta={
                "request_id": request_id,
                "stage": "finalizing",
                "progress": 0.9
            }
        )
        
        # Step 4: Build response
        response = _build_success_response(
            request_id,
            internal_request,
            validation_result,
            measurement_result,
            start_time
        )
        
        logger.info(
            f"Socket 0 measurement complete for {request_id}: "
            f"Energy={measurement_result.median_energy_joules:.3f}J, "
            f"CV={measurement_result.cv_percent:.2f}%, "
            f"Confidence={measurement_result.confidence_level}"
        )
        
        return response
        
    except Exception as e:
        logger.error(f"Socket measurement task failed for {request_id}: {e}", exc_info=True)
        
        # Ensure lock is released
        try:
            self.lock.release()
        except:
            pass
        
        return {
            "request_id": request_id,
            "status": "failed",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "processing_time_seconds": time.time() - start_time
        }


def _parse_request(request_data: Dict[str, Any], request_id: str) -> JouleTraceMeasurementRequest:
    """Parse API request to internal format."""
    
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
    
    # Build CPU config (not used by SocketExecutor, but kept for compatibility)
    from ..core.models import CPUIsolationConfig
    cpu_config = CPUIsolationConfig(
        measurement_core=4,  # Socket 0
        thermal_baseline_wait_seconds=0.0,  # Not used
        isolate_other_processes=True,
        disable_frequency_scaling=True
    )
    
    # Create request
    config = get_config()
    return JouleTraceMeasurementRequest(
        candidate_code=request_data["candidate_code"],
        test_cases=test_cases,
        function_name=request_data.get("function_name", "solve"),
        timeout_seconds=request_data.get("timeout_seconds", config.energy.default_timeout),
        memory_limit_mb=request_data.get("memory_limit_mb", config.energy.default_memory_limit),
        energy_measurement_trials=request_data.get("energy_measurement_trials", 5),
        warmup_trials=0,  # Not used
        cpu_config=cpu_config,
        request_id=UUID(request_id),
        candidate_id=request_data.get("candidate_id"),
        problem_name=request_data.get("problem_name")
    )


def _extract_test_inputs(request: JouleTraceMeasurementRequest) -> List[Any]:
    """Extract inputs from test cases."""
    return [tc.inputs for tc in request.test_cases]


def _validate_solution(validator: SolutionValidator, 
                       request: JouleTraceMeasurementRequest) -> Dict[str, Any]:
    """Validate solution correctness."""
    
    validation_config = ValidationConfig(
        timeout_seconds=request.timeout_seconds,
        memory_limit_mb=request.memory_limit_mb,
        stop_on_first_failure=False,
        max_failed_details=10
    )
    
    validator.config = validation_config
    
    validation_result = validator.validate_solution(
        candidate_code=request.candidate_code,
        function_name=request.function_name,
        test_cases=request.test_cases
    )
    
    return {
        'is_correct': validation_result.is_correct,
        'passed_tests': validation_result.passed_tests,
        'total_tests': validation_result.total_tests,
        'pass_rate': validation_result.pass_rate,
        'error_summary': validation_result.summary if not validation_result.is_correct else None
    }


def _build_validation_only_response(request_id: str,
                                    request: JouleTraceMeasurementRequest,
                                    validation_result: Dict[str, Any],
                                    start_time: float) -> Dict[str, Any]:
    """Build response for validation-only (incorrect solution)."""
    
    return {
        "request_id": request_id,
        "candidate_id": request.candidate_id,
        "problem_name": request.problem_name,
        "status": "validation_failed",
        "validation": validation_result,
        "processing_time_seconds": time.time() - start_time,
        "measurement_timestamp": time.time()
    }


def _build_success_response(request_id: str,
                           request: JouleTraceMeasurementRequest,
                           validation_result: Dict[str, Any],
                           measurement_result: AggregatedResult,
                           start_time: float) -> Dict[str, Any]:
    """Build successful measurement response compatible with EnergyMeasurementResponse."""

    now_ts = time.time()
    energy_per_test_case = (
        measurement_result.median_energy_joules / len(request.test_cases)
        if request.test_cases else measurement_result.median_energy_joules
    )

    # Map Socket 0 aggregated metrics to the API schema fields
    energy_metrics = {
        # Treat net socket energy as both total and package energy; DRAM not separated
        "median_package_energy_joules": measurement_result.median_energy_joules,
        "median_ram_energy_joules": 0.0,
        "median_total_energy_joules": measurement_result.median_energy_joules,
        "median_execution_time_seconds": measurement_result.median_time_seconds,
        "energy_per_test_case_joules": energy_per_test_case,
        "power_consumption_watts": measurement_result.median_power_watts,
        "energy_efficiency_score": energy_per_test_case,
    }

    measurement_environment = {
        "meter_type": "PCM_Socket",
        "cpu_model": None,
        "measurement_core": 4,
        "thermal_controlled": False,
        "timestamp": now_ts,
    }

    return {
        "request_id": request_id,
        "candidate_id": request.candidate_id,
        "problem_name": request.problem_name,
        "status": "completed",
        "validation": validation_result,
        "energy_metrics": energy_metrics,
        "measurement_environment": measurement_environment,
        "processing_time_seconds": time.time() - start_time,
        "measurement_timestamp": now_ts,
    }


# Register task route
celery_app.conf.task_routes.update({
    "socket_measurement_task": {"queue": "socket0_measurements"}
})
