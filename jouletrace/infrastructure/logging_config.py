# jouletrace/infrastructure/logging_config.py
from __future__ import annotations
import logging
import logging.handlers
import sys
import json
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
import traceback

from .config import get_config, LogLevel

class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        
        # Base log entry
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info)
            }
        
        # Add extra fields from record
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        
        if hasattr(record, "task_id"):
            log_entry["task_id"] = record.task_id
        
        if hasattr(record, "candidate_id"):
            log_entry["candidate_id"] = record.candidate_id
        
        if hasattr(record, "duration_ms"):
            log_entry["duration_ms"] = record.duration_ms
        
        # Add any other custom fields
        for key, value in record.__dict__.items():
            if key not in ["name", "msg", "args", "created", "filename", "funcName",
                          "levelname", "levelno", "lineno", "module", "msecs",
                          "message", "pathname", "process", "processName", "relativeCreated",
                          "thread", "threadName", "exc_info", "exc_text", "stack_info"]:
                if not key.startswith("_"):
                    log_entry[key] = value
        
        return json.dumps(log_entry)

class ContextFilter(logging.Filter):
    """Add contextual information to log records."""
    
    def __init__(self, service_name: str, version: str):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Add service context to log record."""
        record.service_name = self.service_name
        record.service_version = self.version
        return True

class EnergyMeasurementFilter(logging.Filter):
    """Filter for energy measurement specific logs."""
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Only pass energy measurement related logs."""
        return "energy" in record.name.lower() or hasattr(record, "energy_metrics")

def setup_logging() -> None:
    """
    Set up logging configuration for JouleTrace.
    
    Configures:
    - Console logging with appropriate format
    - File logging (if enabled)
    - JSON structured logging (if enabled)
    - Log rotation
    - Context filters
    """
    config = get_config()
    
    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(config.logging.level.value)
    
    # Remove existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(config.logging.level.value)
    
    if config.logging.json_logging:
        # JSON formatter for structured logging
        console_formatter = JSONFormatter()
    else:
        # Human-readable formatter
        console_formatter = logging.Formatter(
            config.logging.format,
            datefmt="%Y-%m-%d %H:%M:%S"
        )
    
    console_handler.setFormatter(console_formatter)
    
    # Add context filter
    context_filter = ContextFilter(
        service_name=config.service_name,
        version=config.version
    )
    console_handler.addFilter(context_filter)
    
    root_logger.addHandler(console_handler)
    
    # File handler (if enabled)
    if config.logging.log_to_file:
        _setup_file_logging(root_logger, config)
    
    # Set specific logger levels
    _configure_library_loggers(config)
    
    # Log startup message
    logging.info(f"Logging configured - Level: {config.logging.level.value}, "
                f"JSON: {config.logging.json_logging}, "
                f"File: {config.logging.log_to_file}")

def _setup_file_logging(root_logger: logging.Logger, config) -> None:
    """Set up file-based logging with rotation."""
    
    # Ensure log directory exists
    log_file = Path(config.logging.log_file_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=config.logging.log_file_max_bytes,
        backupCount=config.logging.log_file_backup_count,
        encoding="utf-8"
    )
    
    file_handler.setLevel(config.logging.level.value)
    
    # Always use JSON format for file logs (easier to parse)
    file_formatter = JSONFormatter()
    file_handler.setFormatter(file_formatter)
    
    # Add context filter
    context_filter = ContextFilter(
        service_name=config.service_name,
        version=config.version
    )
    file_handler.addFilter(context_filter)
    
    root_logger.addHandler(file_handler)
    
    logging.info(f"File logging enabled: {log_file}")

def _configure_library_loggers(config) -> None:
    """Configure logging levels for third-party libraries."""
    
    # Reduce noise from third-party libraries
    library_loggers = {
        "uvicorn": logging.WARNING if config.is_production else logging.INFO,
        "uvicorn.access": logging.WARNING if config.is_production else logging.INFO,
        "fastapi": logging.WARNING,
        "celery": logging.WARNING if config.is_production else logging.INFO,
        "redis": logging.WARNING,
        "urllib3": logging.WARNING,
        "asyncio": logging.WARNING,
    }
    
    for logger_name, level in library_loggers.items():
        logging.getLogger(logger_name).setLevel(level)

class PerformanceLogger:
    """Logger for performance metrics and timing."""
    
    def __init__(self, logger_name: str = "jouletrace.performance"):
        self.logger = logging.getLogger(logger_name)
    
    def log_measurement_duration(self, 
                                 task_id: str,
                                 duration_seconds: float,
                                 trials: int,
                                 correct: bool) -> None:
        """Log energy measurement performance."""
        self.logger.info(
            f"Energy measurement completed",
            extra={
                "task_id": task_id,
                "duration_seconds": duration_seconds,
                "trials": trials,
                "correct": correct,
                "metric_type": "measurement_duration"
            }
        )
    
    def log_validation_duration(self,
                                request_id: str, 
                                duration_seconds: float,
                                test_count: int,
                                passed: int) -> None:
        """Log validation performance."""
        self.logger.info(
            f"Validation completed",
            extra={
                "request_id": request_id,
                "duration_seconds": duration_seconds,
                "test_count": test_count,
                "passed_tests": passed,
                "metric_type": "validation_duration"
            }
        )
    
    def log_energy_metrics(self,
                          task_id: str,
                          package_energy_j: float,
                          ram_energy_j: float,
                          execution_time_s: float) -> None:
        """Log actual energy measurement values."""
        self.logger.info(
            f"Energy metrics",
            extra={
                "task_id": task_id,
                "package_energy_joules": package_energy_j,
                "ram_energy_joules": ram_energy_j,
                "total_energy_joules": package_energy_j + ram_energy_j,
                "execution_time_seconds": execution_time_s,
                "metric_type": "energy_measurement"
            }
        )

class SecurityLogger:
    """Logger for security events."""
    
    def __init__(self, logger_name: str = "jouletrace.security"):
        self.logger = logging.getLogger(logger_name)
    
    def log_authentication_failure(self, ip_address: str, reason: str) -> None:
        """Log authentication failures."""
        self.logger.warning(
            f"Authentication failed",
            extra={
                "ip_address": ip_address,
                "reason": reason,
                "event_type": "auth_failure"
            }
        )
    
    def log_rate_limit_exceeded(self, ip_address: str, endpoint: str) -> None:
        """Log rate limit violations."""
        self.logger.warning(
            f"Rate limit exceeded",
            extra={
                "ip_address": ip_address,
                "endpoint": endpoint,
                "event_type": "rate_limit"
            }
        )
    
    def log_suspicious_activity(self, 
                               ip_address: str,
                               activity: str,
                               details: Dict[str, Any]) -> None:
        """Log suspicious activity."""
        self.logger.warning(
            f"Suspicious activity detected",
            extra={
                "ip_address": ip_address,
                "activity": activity,
                "details": details,
                "event_type": "suspicious_activity"
            }
        )

def get_logger(name: str) -> logging.Logger:
    """Get a configured logger instance."""
    return logging.getLogger(name)

def get_performance_logger() -> PerformanceLogger:
    """Get performance logger instance."""
    return PerformanceLogger()

def get_security_logger() -> SecurityLogger:
    """Get security logger instance."""
    return SecurityLogger()

# Convenience functions for common logging patterns
def log_request_start(request_id: str, method: str, path: str, client_ip: str) -> None:
    """Log HTTP request start."""
    logger = get_logger("jouletrace.api")
    logger.info(
        f"Request started: {method} {path}",
        extra={
            "request_id": request_id,
            "method": method,
            "path": path,
            "client_ip": client_ip,
            "event_type": "request_start"
        }
    )

def log_request_end(request_id: str, 
                   status_code: int,
                   duration_ms: float) -> None:
    """Log HTTP request completion."""
    logger = get_logger("jouletrace.api")
    logger.info(
        f"Request completed: {status_code}",
        extra={
            "request_id": request_id,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "event_type": "request_end"
        }
    )

def log_task_queued(task_id: str, request_id: str, queue_name: str) -> None:
    """Log Celery task queued."""
    logger = get_logger("jouletrace.celery")
    logger.info(
        f"Task queued: {task_id}",
        extra={
            "task_id": task_id,
            "request_id": request_id,
            "queue": queue_name,
            "event_type": "task_queued"
        }
    )

def log_task_started(task_id: str, worker_name: str) -> None:
    """Log Celery task started."""
    logger = get_logger("jouletrace.celery")
    logger.info(
        f"Task started: {task_id}",
        extra={
            "task_id": task_id,
            "worker": worker_name,
            "event_type": "task_started"
        }
    )

def log_task_completed(task_id: str, 
                      duration_seconds: float,
                      success: bool) -> None:
    """Log Celery task completion."""
    logger = get_logger("jouletrace.celery")
    level = logging.INFO if success else logging.ERROR
    logger.log(
        level,
        f"Task {'completed' if success else 'failed'}: {task_id}",
        extra={
            "task_id": task_id,
            "duration_seconds": duration_seconds,
            "success": success,
            "event_type": "task_completed"
        }
    )