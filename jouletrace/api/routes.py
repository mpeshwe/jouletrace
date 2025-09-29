# jouletrace/api/routes.py
from __future__ import annotations
import time
import logging
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, status, Response
from fastapi.responses import JSONResponse
from celery.result import AsyncResult

from .schemas import (
    EnergyMeasurementRequest,
    EnergyMeasurementResponse,
    QuickValidationRequest, 
    QuickValidationResponse,
    TaskQueuedResponse,
    TaskRunningResponse,
    TaskFailedResponse,
    TaskStatusResponse,
    SystemHealthResponse,
    SystemCapabilitiesResponse,
    APIErrorResponse
)
from .tasks import celery_app, measure_energy_task
from .dependencies import get_energy_meter_factory, get_pipeline
from ..core.pipeline import JouleTracePipeline
from ..energy.meter_factory import EnergyMeterFactory

logger = logging.getLogger(__name__)

# Create API router
router = APIRouter(prefix="/api/v1", tags=["energy-measurement"])

@router.post(
    "/measure",
    response_model=TaskQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue Energy Measurement Task",
    description="""
    Queue an energy measurement task for asynchronous processing.
    
    This endpoint validates the request, queues a Celery task for energy measurement,
    and returns immediately with a task ID for polling results.
    
    The measurement process:
    1. Validates solution correctness against test cases
    2. If correct, measures energy consumption using hardware energy meters
    3. Returns comprehensive energy efficiency metrics
    
    Use the returned task_id to poll for results via GET /tasks/{task_id}
    """
)
async def queue_energy_measurement(
    request: EnergyMeasurementRequest,
    factory: EnergyMeterFactory = Depends(get_energy_meter_factory)
) -> TaskQueuedResponse:
    """Queue an energy measurement task."""
    
    logger.info(f"Queuing energy measurement task for candidate {request.candidate_id}")
    
    try:
        # Generate request ID
        request_id = uuid4()
        
        # Basic request validation
        if not request.test_cases:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one test case must be provided"
            )
        
        # Queue Celery task
        task = measure_energy_task.delay(
            request_data=request.model_dump(),
            request_id=str(request_id)
        )
        
        # Estimate completion time based on trials and test cases
        estimated_seconds = (
            (request.warmup_trials + request.energy_measurement_trials) *
            len(request.test_cases) * 
            request.timeout_seconds * 0.1  # Conservative estimate
        ) + 30  # Setup overhead
        
        # Build poll URL
        poll_url = f"/api/v1/tasks/{task.id}"
        
        logger.info(f"Task {task.id} queued for request {request_id}")
        
        return TaskQueuedResponse(
            task_id=task.id,
            request_id=request_id,
            status="queued",
            estimated_completion_seconds=int(estimated_seconds),
            poll_url=poll_url
        )
        
    except Exception as e:
        logger.error(f"Failed to queue energy measurement task: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to queue measurement task: {str(e)}"
        )

@router.get(
    "/tasks/{task_id}",
    response_model=TaskStatusResponse,
    summary="Get Task Status and Results",
    description="""
    Poll the status of an energy measurement task.
    
    Returns different response types based on task status:
    - PENDING/QUEUED: TaskQueuedResponse 
    - STARTED/RUNNING: TaskRunningResponse with progress
    - SUCCESS: EnergyMeasurementResponse with complete results
    - FAILURE: TaskFailedResponse with error details
    """
)
async def get_task_status(task_id: str) -> TaskStatusResponse:
    """Get the status and results of an energy measurement task."""
    
    try:
        # Get task result from Celery
        task_result = AsyncResult(task_id, app=celery_app)
        
        if task_result.state == "PENDING":
            # Task is queued but not started
            return TaskQueuedResponse(
                task_id=task_id,
                request_id=task_id,  # Fallback to task_id if request_id not available
                status="queued",
                poll_url=f"/api/v1/tasks/{task_id}"
            )
        
        elif task_result.state == "STARTED":
            # Task is running
            meta = task_result.info or {}
            return TaskRunningResponse(
                task_id=task_id,
                request_id=meta.get("request_id", task_id),
                status="running",
                stage=meta.get("stage", "Processing"),
                progress=meta.get("progress"),
                elapsed_seconds=meta.get("elapsed_seconds", 0.0)
            )
        
        elif task_result.state == "SUCCESS":
            # Task completed (success or reported failure payload)
            result_data = task_result.get()

            if isinstance(result_data, dict) and result_data.get("status") == "failed":
                return TaskFailedResponse(
                    task_id=task_id,
                    request_id=result_data.get("request_id", task_id),
                    status="failed",
                    error_type=result_data.get("error_type", "UnknownError"),
                    error_message=str(result_data.get("error_message", "Task failed")),
                    failure_timestamp=result_data.get("measurement_timestamp", time.time())
                )

            # Convert internal result to API response format
            return EnergyMeasurementResponse(**result_data)
        
        elif task_result.state == "FAILURE":
            # Task failed
            error_info = task_result.info or {}
            
            return TaskFailedResponse(
                task_id=task_id,
                request_id=error_info.get("request_id", task_id),
                status="failed",
                error_type=error_info.get("error_type", "UnknownError"),
                error_message=str(error_info.get("error_message", "Task failed")),
                failure_timestamp=time.time()
            )
        
        else:
            # Unknown state
            logger.warning(f"Unknown task state: {task_result.state}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unknown task state: {task_result.state}"
            )
            
    except Exception as e:
        logger.error(f"Error getting task status for {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get task status: {str(e)}"
        )

@router.post(
    "/validate",
    response_model=QuickValidationResponse,
    summary="Quick Solution Validation",
    description="""
    Perform quick validation of a solution without energy measurement.
    
    This is useful for:
    - Fast feedback during development
    - Pre-screening solutions before energy measurement
    - Testing solution correctness without hardware requirements
    
    Returns validation results immediately (synchronous operation).
    """
)
async def quick_validation(
    request: QuickValidationRequest,
    pipeline: JouleTracePipeline = Depends(get_pipeline)
) -> QuickValidationResponse:
    """Perform quick validation without energy measurement."""
    
    logger.info("Performing quick validation")
    start_time = time.time()
    
    try:
        # Generate request ID
        request_id = uuid4()
        
        # Convert API request to internal format
        from ..core.models import JouleTraceMeasurementRequest, JouleTraceTestCase
        
        internal_test_cases = [
            JouleTraceTestCase(
                inputs=tc.inputs,
                expected_output=tc.expected_output,
                test_id=tc.test_id,
                metadata=tc.metadata
            )
            for tc in request.test_cases
        ]
        
        internal_request = JouleTraceMeasurementRequest(
            candidate_code=request.candidate_code,
            test_cases=internal_test_cases,
            function_name=request.function_name,
            timeout_seconds=request.timeout_seconds,
            energy_measurement_trials=1,  # Not used for quick validation
            request_id=request_id
        )
        
        # Perform quick validation
        result = pipeline.quick_check(internal_request)
        
        processing_time = time.time() - start_time
        
        # Convert internal result to API response
        from .schemas import ValidationSummary
        
        validation_summary = ValidationSummary(
            is_correct=result.validation.is_correct,
            passed_tests=result.validation.passed_tests,
            total_tests=result.validation.total_tests,
            pass_rate=result.validation.pass_rate,
            error_summary=result.validation.summary if not result.validation.is_correct else None
        )
        
        logger.info(f"Quick validation completed in {processing_time:.3f}s - "
                   f"Result: {validation_summary.passed_tests}/{validation_summary.total_tests} passed")
        
        return QuickValidationResponse(
            request_id=request_id,
            validation=validation_summary,
            processing_time_seconds=processing_time
        )
        
    except Exception as e:
        logger.error(f"Quick validation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Validation failed: {str(e)}"
        )

@router.get(
    "/health",
    response_model=SystemHealthResponse,
    summary="System Health Check",
    description="""
    Get system health status and operational metrics.
    
    Returns information about:
    - Overall system health status
    - Energy measurement availability
    - Celery worker status
    - Redis connectivity
    - Performance metrics
    """
)
async def health_check(
    factory: EnergyMeterFactory = Depends(get_energy_meter_factory)
) -> SystemHealthResponse:
    """Get system health status."""
    
    logger.debug("Performing health check")
    
    try:
        # Check energy meter availability
        energy_info = factory.get_system_energy_info()
        energy_available = energy_info["energy_measurement_ready"]
        meter_type = energy_info.get("recommended_meter")
        
        # Check Celery workers
        inspect = celery_app.control.inspect()
        active_workers = inspect.active() or {}
        worker_count = len(active_workers)
        
        # Check Redis connectivity (simplified)
        redis_connected = True  # TODO: Implement actual Redis health check
        
        # Calculate overall status
        if energy_available and worker_count > 0 and redis_connected:
            overall_status = "healthy"
        elif worker_count > 0 and redis_connected:
            overall_status = "degraded"  # Energy measurement not available
        else:
            overall_status = "unhealthy"
        
        return SystemHealthResponse(
            status=overall_status,
            timestamp=time.time(),
            version="1.0.0",  # TODO: Get from package
            energy_meter_available=energy_available,
            energy_meter_type=meter_type,
            celery_worker_count=worker_count,
            redis_connected=redis_connected,
            active_tasks=sum(len(tasks) for tasks in active_workers.values()),
            completed_tasks_24h=0,  # TODO: Implement task metrics
            average_measurement_time_seconds=None  # TODO: Implement performance metrics
        )
        
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        
        # Return unhealthy status if health check itself fails
        return SystemHealthResponse(
            status="unhealthy",
            timestamp=time.time(),
            version="1.0.0",
            energy_meter_available=False,
            energy_meter_type=None,
            celery_worker_count=0,
            redis_connected=False,
            active_tasks=0,
            completed_tasks_24h=0
        )

@router.get(
    "/capabilities",
    response_model=SystemCapabilitiesResponse,
    summary="System Capabilities",
    description="""
    Get detailed information about system capabilities and configuration.
    
    Returns information about:
    - Energy measurement capabilities
    - Supported meter types
    - System hardware information
    - Operational limits and constraints
    """
)
async def get_capabilities(
    factory: EnergyMeterFactory = Depends(get_energy_meter_factory)
) -> SystemCapabilitiesResponse:
    """Get system capabilities and configuration."""
    
    logger.debug("Getting system capabilities")
    
    try:
        # Get energy measurement info
        energy_info = factory.get_system_energy_info()
        system_caps = energy_info["system_capabilities"]
        
        import psutil
        
        return SystemCapabilitiesResponse(
            energy_measurement_available=energy_info["energy_measurement_ready"],
            supported_meter_types=energy_info.get("available_meter_types", []),
            cpu_isolation_supported=True,  # We built CPU isolation support
            thermal_monitoring_supported=system_caps.get("rapl_available", False),
            platform=system_caps.get("platform", "unknown"),
            cpu_vendor=system_caps.get("cpu_vendor"),
            cpu_cores=psutil.cpu_count(logical=True),
            available_memory_gb=psutil.virtual_memory().available / (1024**3),
            max_concurrent_measurements=4,  # Conservative limit
            max_measurement_timeout_seconds=300,
            max_memory_limit_mb=8192
        )
        
    except Exception as e:
        logger.error(f"Failed to get capabilities: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get system capabilities: {str(e)}"
        )

@router.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Cancel Task",
    description="""
    Cancel a running or queued energy measurement task.
    
    Note: Tasks that are already executing may not be immediately cancelled
    due to the nature of energy measurement operations.
    """
)
async def cancel_task(task_id: str) -> Response:
    """Cancel an energy measurement task."""
    
    logger.info(f"Cancelling task {task_id}")
    
    try:
        # Revoke the task
        celery_app.control.revoke(task_id, terminate=True)
        logger.info(f"Task {task_id} cancelled")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as e:
        logger.error(f"Failed to cancel task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel task: {str(e)}"
        )

@router.get(
    "/metrics",
    summary="System Metrics",
    description="Get operational metrics and statistics (basic implementation)"
)
async def get_metrics() -> Dict[str, Any]:
    """Get system operational metrics."""
    
    try:
        # Basic metrics implementation
        inspect = celery_app.control.inspect()
        stats = inspect.stats() or {}
        
        import psutil
        
        return {
            "timestamp": time.time(),
            "system": {
                "cpu_usage": psutil.cpu_percent(interval=1),
                "memory_usage": psutil.virtual_memory().percent,
                "disk_usage": psutil.disk_usage('/').percent,
            },
            "celery": {
                "worker_count": len(stats),
                "worker_stats": stats
            },
            "energy_measurement": {
                "available": True,  # TODO: Check actual availability
                "meter_type": "perf"  # TODO: Get from actual meter
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get metrics: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get metrics: {str(e)}"
        )
