# jouletrace/api/dependencies.py
from __future__ import annotations
import logging
from typing import Optional, Generator
from functools import lru_cache

from fastapi import Depends, HTTPException, status

from ..core.pipeline import JouleTracePipeline
from ..energy.meter_factory import EnergyMeterFactory, create_energy_meter
from ..energy.interfaces import EnergyMeter, EnergyMeterError
from ..infrastructure.config import get_config

logger = logging.getLogger(__name__)

# Global instances for dependency injection
_energy_meter_factory: Optional[EnergyMeterFactory] = None
_energy_meter_instance: Optional[EnergyMeter] = None
_pipeline_instance: Optional[JouleTracePipeline] = None

@lru_cache()
def get_energy_meter_factory() -> EnergyMeterFactory:
    """
    Get a singleton EnergyMeterFactory instance.
    
    This factory is used to create and configure energy meters
    based on system capabilities.
    """
    global _energy_meter_factory
    
    if _energy_meter_factory is None:
        logger.info("Initializing EnergyMeterFactory")
        _energy_meter_factory = EnergyMeterFactory()
        
        # Log system capabilities at startup
        try:
            system_info = _energy_meter_factory.get_system_energy_info()
            logger.info(f"Energy measurement capabilities: {system_info['energy_measurement_ready']}")
            
            if not system_info['energy_measurement_ready']:
                issues = system_info.get('validation_issues', [])
                logger.warning(f"Energy measurement not ready: {issues}")
        except Exception as e:
            logger.error(f"Failed to get system energy info: {e}")
    
    return _energy_meter_factory

def get_energy_meter() -> Optional[EnergyMeter]:
    """
    Get a configured energy meter instance.
    
    Returns None if energy measurement is not available on this system.
    This is used by the pipeline for actual energy measurement.
    """
    global _energy_meter_instance
    
    if _energy_meter_instance is None:
        try:
            logger.info("Creating energy meter instance")
            config = get_config()
            _energy_meter_instance = create_energy_meter(
                use_sudo=config.energy.use_sudo,
                perf_timeout=config.energy.perf_timeout
            )
            
            if _energy_meter_instance:
                meter_type = _energy_meter_instance.meter_type.value
                logger.info(f"Energy meter created successfully: {meter_type}")
            else:
                logger.warning("No energy meter available on this system")
                
        except Exception as e:
            logger.error(f"Failed to create energy meter: {e}", exc_info=True)
            _energy_meter_instance = None
    
    return _energy_meter_instance

def get_pipeline() -> JouleTracePipeline:
    """
    Get a configured JouleTrace pipeline instance.
    
    The pipeline uses the available energy meter, or operates in runtime-only mode
    if energy measurement is not available.
    """
    global _pipeline_instance
    
    if _pipeline_instance is None:
        logger.info("Creating JouleTrace pipeline")
        
        # Get energy meter (may be None)
        energy_meter = get_energy_meter()
        
        # Create pipeline
        _pipeline_instance = JouleTracePipeline(energy_meter=energy_meter)
        
        if energy_meter:
            logger.info("Pipeline created with energy measurement support")
        else:
            logger.info("Pipeline created in runtime-only mode (no energy measurement)")
    
    return _pipeline_instance

def validate_energy_measurement_available() -> None:
    """
    Dependency that validates energy measurement is available.
    
    Raises HTTPException if energy measurement is not supported on this system.
    Use this for endpoints that require actual energy measurement.
    """
    energy_meter = get_energy_meter()
    
    if energy_meter is None:
        factory = get_energy_meter_factory()
        system_info = factory.get_system_energy_info()
        
        issues = system_info.get('validation_issues', ['Energy measurement not available'])
        error_detail = {
            "error": "energy_measurement_not_available",
            "message": "Energy measurement is not available on this system",
            "issues": issues,
            "system_capabilities": system_info.get('system_capabilities', {})
        }
        
        logger.warning(f"Energy measurement validation failed: {issues}")
        
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail
        )

def get_validated_energy_meter() -> EnergyMeter:
    """
    Get a validated energy meter that is guaranteed to work.
    
    This dependency validates the energy meter and raises an HTTP exception
    if energy measurement is not available.
    """
    validate_energy_measurement_available()
    return get_energy_meter()

def check_system_health() -> dict:
    """
    Dependency that performs a quick system health check.
    
    Returns system health information that can be used by endpoints.
    """
    try:
        factory = get_energy_meter_factory()
        energy_meter = get_energy_meter()
        pipeline = get_pipeline()
        
        health_info = {
            "timestamp": time.time(),
            "energy_meter_available": energy_meter is not None,
            "energy_meter_type": energy_meter.meter_type.value if energy_meter else None,
            "pipeline_ready": pipeline is not None,
            "factory_ready": factory is not None
        }
        
        # Get system capabilities
        try:
            system_info = factory.get_system_energy_info()
            health_info["system_ready"] = system_info["energy_measurement_ready"]
            health_info["validation_issues"] = system_info.get("validation_issues", [])
        except Exception as e:
            health_info["system_ready"] = False
            health_info["system_error"] = str(e)
        
        return health_info
        
    except Exception as e:
        logger.error(f"System health check failed: {e}", exc_info=True)
        return {
            "timestamp": time.time(),
            "energy_meter_available": False,
            "pipeline_ready": False,
            "factory_ready": False,
            "system_ready": False,
            "error": str(e)
        }

# Startup and shutdown handlers
def startup_dependencies():
    """
    Initialize all dependencies at application startup.
    
    This ensures that energy meter initialization happens early
    and any configuration issues are detected at startup.
    """
    logger.info("Initializing JouleTrace dependencies")
    
    try:
        # Initialize factory
        factory = get_energy_meter_factory()
        logger.info("EnergyMeterFactory initialized")
        
        # Attempt to create energy meter
        energy_meter = get_energy_meter()
        if energy_meter:
            logger.info(f"Energy meter available: {energy_meter.meter_type.value}")
        else:
            logger.warning("Energy meter not available - service will run in validation-only mode")
        
        # Initialize pipeline
        pipeline = get_pipeline()
        logger.info("JouleTrace pipeline initialized")
        
        # Log system capabilities
        system_info = factory.get_system_energy_info()
        logger.info(f"System energy measurement ready: {system_info['energy_measurement_ready']}")
        
        if not system_info['energy_measurement_ready']:
            logger.warning("Energy measurement not available:")
            for issue in system_info.get('validation_issues', []):
                logger.warning(f"  - {issue}")
        
    except Exception as e:
        logger.error(f"Failed to initialize dependencies: {e}", exc_info=True)
        # Don't raise exception - allow service to start in degraded mode

def shutdown_dependencies():
    """
    Clean up dependencies at application shutdown.
    """
    global _energy_meter_instance, _pipeline_instance, _energy_meter_factory
    
    logger.info("Cleaning up JouleTrace dependencies")
    
    try:
        # Clean up energy meter
        if _energy_meter_instance:
            _energy_meter_instance.cleanup()
            logger.info("Energy meter cleaned up")
        
        # Reset global instances
        _energy_meter_instance = None
        _pipeline_instance = None
        _energy_meter_factory = None
        
    except Exception as e:
        logger.error(f"Error during dependency cleanup: {e}", exc_info=True)

# Utility functions for testing and development
def reset_dependencies():
    """
    Reset all dependency instances.
    
    Useful for testing and development to force re-initialization.
    """
    global _energy_meter_instance, _pipeline_instance, _energy_meter_factory
    
    logger.info("Resetting JouleTrace dependencies")
    
    # Clean up existing instances
    if _energy_meter_instance:
        try:
            _energy_meter_instance.cleanup()
        except Exception as e:
            logger.warning(f"Error cleaning up energy meter: {e}")
    
    # Reset instances
    _energy_meter_instance = None
    _pipeline_instance = None
    _energy_meter_factory = None
    
    # Clear LRU cache
    get_energy_meter_factory.cache_clear()

def get_dependency_status() -> dict:
    """
    Get detailed status of all dependencies.
    
    Useful for debugging and monitoring.
    """
    status_info = {
        "factory_initialized": _energy_meter_factory is not None,
        "energy_meter_initialized": _energy_meter_instance is not None,
        "pipeline_initialized": _pipeline_instance is not None,
    }
    
    if _energy_meter_instance:
        try:
            status_info["energy_meter_type"] = _energy_meter_instance.meter_type.value
            status_info["energy_meter_available"] = _energy_meter_instance.is_available()
        except Exception as e:
            status_info["energy_meter_error"] = str(e)
    
    if _energy_meter_factory:
        try:
            system_info = _energy_meter_factory.get_system_energy_info()
            status_info["system_energy_ready"] = system_info["energy_measurement_ready"]
            status_info["validation_issues"] = system_info.get("validation_issues", [])
        except Exception as e:
            status_info["factory_error"] = str(e)
    
    return status_info

# Import time for health check
import time
