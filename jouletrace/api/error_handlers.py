# jouletrace/api/error_handlers.py
from __future__ import annotations
import time
import logging
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from .schemas import APIErrorResponse, ValidationErrorResponse
from ..energy.interfaces import (
    EnergyMeterError,
    EnergyMeterNotAvailableError, 
    EnergyMeterPermissionError,
    EnergyMeterTimeoutError
)
from ..core.executor import ExecutionTimeout, ExecutionMemoryLimit, ExecutionError

logger = logging.getLogger(__name__)

def setup_error_handlers(app: FastAPI) -> None:
    """
    Set up comprehensive error handlers for the FastAPI application.
    
    This ensures consistent error responses and proper logging of all errors.
    """
    
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle standard HTTP exceptions."""
        
        # Log the error
        logger.warning(f"HTTP {exc.status_code}: {exc.detail} - {request.method} {request.url}")
        
        error_response = APIErrorResponse(
            error="http_error",
            message=str(exc.detail),
            details={"status_code": exc.status_code},
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Handle Pydantic validation errors with detailed field information."""
        
        # Extract field-specific errors
        field_errors = []
        for error in exc.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            field_errors.append({
                "field": field_path,
                "message": error["msg"],
                "type": error["type"]
            })
        
        logger.warning(f"Validation error: {len(field_errors)} field errors - {request.method} {request.url}")
        
        error_response = ValidationErrorResponse(
            error="validation_error",
            message=f"Request validation failed with {len(field_errors)} errors",
            field_errors=field_errors,
            timestamp=time.time()
        )
        
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(EnergyMeterNotAvailableError)
    async def energy_meter_not_available_handler(request: Request, exc: EnergyMeterNotAvailableError) -> JSONResponse:
        """Handle energy meter not available errors."""
        
        logger.error(f"Energy meter not available: {exc}")
        
        error_response = APIErrorResponse(
            error="energy_meter_not_available",
            message="Energy measurement is not available on this system",
            details={
                "reason": str(exc),
                "suggestion": "Check system requirements and run diagnostics via GET /api/v1/capabilities"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(EnergyMeterPermissionError)
    async def energy_meter_permission_handler(request: Request, exc: EnergyMeterPermissionError) -> JSONResponse:
        """Handle energy meter permission errors."""
        
        logger.error(f"Energy meter permission error: {exc}")
        
        error_response = APIErrorResponse(
            error="energy_meter_permission_error",
            message="Insufficient permissions for energy measurement",
            details={
                "reason": str(exc),
                "suggestion": "Run with sudo or adjust perf_event_paranoid settings"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(EnergyMeterTimeoutError)
    async def energy_meter_timeout_handler(request: Request, exc: EnergyMeterTimeoutError) -> JSONResponse:
        """Handle energy measurement timeout errors."""
        
        logger.error(f"Energy measurement timeout: {exc}")
        
        error_response = APIErrorResponse(
            error="energy_measurement_timeout",
            message="Energy measurement operation timed out",
            details={
                "reason": str(exc),
                "suggestion": "Reduce number of trials or increase timeout limits"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(EnergyMeterError)
    async def energy_meter_error_handler(request: Request, exc: EnergyMeterError) -> JSONResponse:
        """Handle general energy meter errors."""
        
        logger.error(f"Energy meter error: {exc}", exc_info=True)
        
        error_response = APIErrorResponse(
            error="energy_meter_error",
            message="Energy measurement failed",
            details={
                "reason": str(exc),
                "error_type": type(exc).__name__
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(ExecutionTimeout)
    async def execution_timeout_handler(request: Request, exc: ExecutionTimeout) -> JSONResponse:
        """Handle code execution timeout errors."""
        
        logger.warning(f"Code execution timeout: {exc}")
        
        error_response = APIErrorResponse(
            error="execution_timeout",
            message="Code execution timed out",
            details={
                "reason": str(exc),
                "suggestion": "Optimize code or increase timeout_seconds parameter"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(ExecutionMemoryLimit)
    async def execution_memory_limit_handler(request: Request, exc: ExecutionMemoryLimit) -> JSONResponse:
        """Handle code execution memory limit errors."""
        
        logger.warning(f"Code execution memory limit exceeded: {exc}")
        
        error_response = APIErrorResponse(
            error="execution_memory_limit",
            message="Code execution exceeded memory limit",
            details={
                "reason": str(exc),
                "suggestion": "Optimize memory usage or increase memory_limit_mb parameter"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_413_PAYLOAD_TOO_LARGE,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(ExecutionError) 
    async def execution_error_handler(request: Request, exc: ExecutionError) -> JSONResponse:
        """Handle general code execution errors."""
        
        logger.warning(f"Code execution error: {exc}")
        
        error_response = APIErrorResponse(
            error="execution_error",
            message="Code execution failed",
            details={
                "reason": str(exc),
                "suggestion": "Check code syntax and function definition"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """Handle value errors with user-friendly messages."""
        
        logger.warning(f"Value error: {exc}")
        
        error_response = APIErrorResponse(
            error="invalid_value",
            message="Invalid parameter value",
            details={
                "reason": str(exc),
                "suggestion": "Check parameter values and types"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_response.model_dump()
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle all other unexpected exceptions."""
        
        # Log the full traceback for debugging
        logger.error(f"Unexpected error: {exc}", exc_info=True)
        
        # Don't expose internal details in production
        error_response = APIErrorResponse(
            error="internal_server_error",
            message="An unexpected error occurred",
            details={
                "error_type": type(exc).__name__,
                "suggestion": "Please try again or contact support if the problem persists"
            },
            timestamp=time.time(),
            request_id=getattr(request.state, 'request_id', None)
        )
        
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_response.model_dump()
        )

class ErrorReportingMiddleware:
    """
    Middleware for enhanced error reporting and request tracking.
    """
    
    def __init__(self, app: FastAPI):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # Generate request ID for tracking
        import uuid
        request_id = str(uuid.uuid4())
        
        # Add request ID to request state
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["request_id"] = request_id
        
        # Log request start
        method = scope.get("method", "UNKNOWN")
        path = scope.get("path", "UNKNOWN")
        logger.debug(f"Request started: {request_id} - {method} {path}")
        
        start_time = time.time()
        
        try:
            await self.app(scope, receive, send)
        except Exception as e:
            # Log unhandled exceptions with request context
            logger.error(f"Unhandled exception in request {request_id}: {e}", exc_info=True)
            raise
        finally:
            # Log request completion
            duration = time.time() - start_time
            logger.debug(f"Request completed: {request_id} - {duration:.3f}s")

def get_error_statistics() -> Dict[str, Any]:
    """
    Get error statistics for monitoring and debugging.
    
    This would integrate with logging systems to provide error metrics.
    """
    # This is a placeholder implementation
    # In production, this would query logging/monitoring systems
    
    return {
        "timestamp": time.time(),
        "error_counts": {
            "total_errors_24h": 0,
            "energy_meter_errors_24h": 0,
            "validation_errors_24h": 0,
            "execution_errors_24h": 0,
            "timeout_errors_24h": 0
        },
        "error_rates": {
            "error_rate_percentage": 0.0,
            "availability_percentage": 100.0
        },
        "recent_errors": []
    }

def create_error_response(
    error_type: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None
) -> APIErrorResponse:
    """
    Utility function to create standardized error responses.
    """
    return APIErrorResponse(
        error=error_type,
        message=message,
        details=details or {},
        timestamp=time.time(),
        request_id=request_id
    )

def log_error_with_context(
    logger: logging.Logger,
    error: Exception,
    request: Optional[Request] = None,
    additional_context: Optional[Dict[str, Any]] = None
) -> None:
    """
    Log errors with additional context for debugging.
    """
    context_info = {
        "error_type": type(error).__name__,
        "error_message": str(error),
        "timestamp": time.time()
    }
    
    if request:
        context_info.update({
            "method": request.method,
            "url": str(request.url),
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, 'request_id', None)
        })
    
    if additional_context:
        context_info.update(additional_context)
    
    logger.error(f"Error with context: {context_info}", exc_info=True)