# jouletrace/core/pipeline.py
from __future__ import annotations
import statistics
import time
from typing import List, Optional, Protocol
from contextlib import contextmanager
import logging

from .models import (
    JouleTraceMeasurementRequest,
    JouleTraceMeasurementResult, 
    MeasurementStatus,
    EnergyMeasurement
)
from .validator import SolutionValidator, ValidationConfig
from .cpu_isolation import CPUIsolationManager
from .executor import SafeCodeExecutor

logger = logging.getLogger(__name__)

class EnergyMeter(Protocol):
    """Protocol for energy measurement backends."""
    
    def measure_execution(self, 
                         executor: SafeCodeExecutor,
                         code: str,
                         function_name: str,
                         test_inputs: List[any],
                         trials: int,
                         cpu_core: int) -> List[EnergyMeasurement]:
        """
        Measure energy consumption during code execution.
        
        Args:
            executor: Safe code executor to use
            code: Code to execute
            function_name: Function to call
            test_inputs: Inputs to feed to the function
            trials: Number of measurement trials
            cpu_core: CPU core to run on
            
        Returns:
            List of energy measurements from each trial
        """
        ...
    
    def get_environment_info(self) -> dict:
        """Get information about the measurement environment."""
        ...

class JouleTracePipeline:
    """
    Core orchestration pipeline for JouleTrace energy measurement.
    
    This is the main interface that coordinates:
    1. Solution validation (correctness gate)
    2. CPU isolation setup (fair measurement)
    3. Energy measurement (only for correct solutions)
    4. Result aggregation and reporting
    """
    
    def __init__(self, energy_meter: Optional[EnergyMeter] = None):
        self.energy_meter = energy_meter
        self.validator = SolutionValidator()
        self.executor = SafeCodeExecutor()
        self._current_isolation_manager: Optional[CPUIsolationManager] = None
    
    def _validate_request(self, request: JouleTraceMeasurementRequest) -> tuple[bool, str]:
        """Validate the measurement request."""
        valid, error = request.validate()
        if not valid:
            return False, error
        
        # Additional validation
        if not request.test_cases:
            return False, "No test cases provided"
        
        # Validate that we can parse the test cases
        for i, test_case in enumerate(request.test_cases[:3]):  # Check first few
            if not hasattr(test_case, 'inputs') or not hasattr(test_case, 'expected_output'):
                return False, f"Invalid test case format at index {i}"
        
        return True, "Request validation passed"
    
    def _setup_validation_config(self, request: JouleTraceMeasurementRequest) -> ValidationConfig:
        """Create validation configuration from request."""
        return ValidationConfig(
            timeout_seconds=request.timeout_seconds,
            memory_limit_mb=request.memory_limit_mb,
            stop_on_first_failure=False,  # Always run all tests for complete validation
            max_failed_details=10
        )
    
    @contextmanager
    def _managed_cpu_isolation(self, request: JouleTraceMeasurementRequest):
        """Context manager for CPU isolation setup and cleanup."""
        isolation_manager = CPUIsolationManager(request.cpu_config)
        self._current_isolation_manager = isolation_manager
        
        try:
            # Set up isolation
            measurement_core = isolation_manager.setup_isolation()
            
            # Wait for thermal baseline if configured
            thermal_ready = isolation_manager.wait_thermal_baseline()
            if not thermal_ready:
                logger.warning("Thermal baseline not reached, measurements may be inconsistent")
            
            yield measurement_core, isolation_manager
            
        finally:
            # Always clean up isolation
            isolation_manager.cleanup_isolation()
            self._current_isolation_manager = None
    
    def _perform_validation(self, request: JouleTraceMeasurementRequest) -> tuple[bool, any]:
        """Perform solution validation with detailed logging."""
        logger.info(f"Starting validation for request {request.request_id}")
        
        validation_config = self._setup_validation_config(request)
        validator = SolutionValidator(validation_config)
        
        validation_result = validator.validate_solution(
            candidate_code=request.candidate_code,
            function_name=request.function_name,
            test_cases=request.test_cases
        )
        
        if validation_result.is_correct:
            logger.info(f"Validation PASSED: {validation_result.passed_tests}/{validation_result.total_tests} tests")
        else:
            logger.warning(f"Validation FAILED: {validation_result.passed_tests}/{validation_result.total_tests} tests")
            if validation_result.execution_errors:
                logger.warning(f"Execution errors: {validation_result.execution_errors[:3]}")
        
        return validation_result.is_correct, validation_result
    
    def _extract_test_inputs(self, request: JouleTraceMeasurementRequest) -> List[any]:
        """Extract just the inputs from test cases for energy measurement."""
        return [test_case.inputs for test_case in request.test_cases]
    
    def _perform_energy_measurement(self, 
                                   request: JouleTraceMeasurementRequest,
                                   measurement_core: int) -> List[EnergyMeasurement]:
        """Perform energy measurement using the configured energy meter."""
        if not self.energy_meter:
            raise RuntimeError("No energy meter configured")
        
        logger.info(f"Starting energy measurement with {request.energy_measurement_trials} trials")
        
        # Extract inputs for measurement
        test_inputs = self._extract_test_inputs(request)
        
        # Perform warmup if configured
        if request.warmup_trials > 0:
            logger.info(f"Performing {request.warmup_trials} warmup trials")
            try:
                self.energy_meter.measure_execution(
                    executor=self.executor,
                    code=request.candidate_code,
                    function_name=request.function_name,
                    test_inputs=test_inputs,
                    trials=request.warmup_trials,
                    cpu_core=measurement_core
                )
            except Exception as e:
                logger.warning(f"Warmup failed: {e}, proceeding with measurement")
        
        # Perform actual energy measurement
        return self.energy_meter.measure_execution(
            executor=self.executor,
            code=request.candidate_code,
            function_name=request.function_name,
            test_inputs=test_inputs,
            trials=request.energy_measurement_trials,
            cpu_core=measurement_core
        )
    
    def _calculate_aggregate_metrics(self, 
                                   measurements: List[EnergyMeasurement],
                                   num_test_cases: int) -> dict:
        """Calculate aggregate energy efficiency metrics."""
        if not measurements:
            return {
                'median_package_energy_joules': 0.0,
                'median_ram_energy_joules': 0.0,
                'median_execution_time_seconds': 0.0,
                'median_total_energy_joules': 0.0,
                'energy_per_test_case_joules': 0.0,
                'power_consumption_watts': 0.0
            }
        
        # Extract measurement values
        package_energies = [m.package_energy_joules for m in measurements]
        ram_energies = [m.ram_energy_joules for m in measurements]
        execution_times = [m.execution_time_seconds for m in measurements]
        total_energies = [m.total_energy_joules for m in measurements]
        
        # Calculate medians
        median_package = statistics.median(package_energies)
        median_ram = statistics.median(ram_energies)
        median_time = statistics.median(execution_times)
        median_total = statistics.median(total_energies)
        
        # Calculate derived metrics
        energy_per_test_case = median_total / num_test_cases if num_test_cases > 0 else 0.0
        power_consumption = median_total / median_time if median_time > 0 else 0.0
        
        return {
            'median_package_energy_joules': median_package,
            'median_ram_energy_joules': median_ram,
            'median_execution_time_seconds': median_time,
            'median_total_energy_joules': median_total,
            'energy_per_test_case_joules': energy_per_test_case,
            'power_consumption_watts': power_consumption
        }
    
    def _create_environment_info(self, 
                                isolation_manager: Optional[CPUIsolationManager],
                                energy_meter_info: Optional[dict]) -> dict:
        """Create measurement environment information."""
        env_info = {
            'measurement_timestamp': time.time(),
            'jouletrace_version': '1.0.0',  # TODO: Get from package
        }
        
        if isolation_manager:
            env_info['cpu_isolation'] = isolation_manager.get_isolation_info()
        
        if energy_meter_info:
            env_info['energy_meter'] = energy_meter_info
        
        return env_info
    
    def measure_energy(self, request: JouleTraceMeasurementRequest) -> JouleTraceMeasurementResult:
        """
        Main interface: measure energy consumption of candidate solution.
        
        This implements the complete JouleTrace workflow:
        1. Validate request
        2. Validate solution correctness (100% correctness gate)
        3. Set up CPU isolation for fair measurement
        4. Measure energy consumption (only if solution is correct)
        5. Calculate aggregate metrics
        6. Return comprehensive result
        
        Args:
            request: Standard JouleTrace measurement request
            
        Returns:
            Complete measurement result with validation and energy data
        """
        logger.info(f"Starting JouleTrace measurement for request {request.request_id}")
        start_time = time.time()
        
        # Request validation
        request_valid, validation_error = self._validate_request(request)
        if not request_valid:
            logger.error(f"Request validation failed: {validation_error}")
            return JouleTraceMeasurementResult(
                request_id=request.request_id,
                candidate_id=request.candidate_id,
                problem_name=request.problem_name,
                validation=None,  # Will be populated with error info
                status=MeasurementStatus.VALIDATION_ERROR,
                error_details=validation_error
            )
        
        # Solution validation (correctness gate)
        try:
            solution_correct, validation_result = self._perform_validation(request)
        except Exception as e:
            logger.error(f"Solution validation error: {e}", exc_info=True)
            return JouleTraceMeasurementResult(
                request_id=request.request_id,
                candidate_id=request.candidate_id,
                problem_name=request.problem_name,
                validation=None,
                status=MeasurementStatus.EXECUTION_ERROR,
                error_details=f"Validation error: {e}"
            )
        
        # If solution is incorrect, return early (no energy measurement)
        if not solution_correct:
            logger.info("Solution validation failed, skipping energy measurement")
            return JouleTraceMeasurementResult(
                request_id=request.request_id,
                candidate_id=request.candidate_id,
                problem_name=request.problem_name,
                validation=validation_result,
                status=MeasurementStatus.INCORRECT_SOLUTION
            )
        
        # Solution is correct, proceed with energy measurement
        logger.info("Solution validation passed, proceeding with energy measurement")
        
        # Energy measurement with CPU isolation
        try:
            with self._managed_cpu_isolation(request) as (measurement_core, isolation_manager):
                
                if self.energy_meter:
                    # Perform energy measurement
                    energy_measurements = self._perform_energy_measurement(request, measurement_core)
                    energy_meter_info = self.energy_meter.get_environment_info()
                    status = MeasurementStatus.SUCCESS
                else:
                    # No energy meter configured - runtime-only mode
                    logger.warning("No energy meter configured, returning runtime-only result")
                    energy_measurements = []
                    energy_meter_info = {'mode': 'runtime_only', 'reason': 'no_energy_meter_configured'}
                    status = MeasurementStatus.SUCCESS
                
                # Calculate aggregate metrics
                aggregate_metrics = self._calculate_aggregate_metrics(
                    energy_measurements, len(request.test_cases)
                )
                
                # Create environment information
                environment_info = self._create_environment_info(isolation_manager, energy_meter_info)
                
        except Exception as e:
            logger.error(f"Energy measurement failed: {e}", exc_info=True)
            return JouleTraceMeasurementResult(
                request_id=request.request_id,
                candidate_id=request.candidate_id,
                problem_name=request.problem_name,
                validation=validation_result,
                status=MeasurementStatus.ENERGY_MEASUREMENT_FAILED,
                error_details=f"Energy measurement error: {e}"
            )
        
        # Create successful result
        result = JouleTraceMeasurementResult(
            request_id=request.request_id,
            candidate_id=request.candidate_id,
            problem_name=request.problem_name,
            validation=validation_result,
            energy_measurements=energy_measurements,
            status=status,
            measurement_environment=environment_info,
            **aggregate_metrics
        )
        
        total_time = time.time() - start_time
        logger.info(f"JouleTrace measurement completed in {total_time:.2f}s - Status: {status.value}")
        
        return result
    
    def quick_check(self, request: JouleTraceMeasurementRequest) -> JouleTraceMeasurementResult:
        """
        Quick validation check without energy measurement.
        Useful for fast feedback during development.
        """
        logger.info(f"Quick check for request {request.request_id}")
        
        # Just perform validation
        try:
            solution_correct, validation_result = self._perform_validation(request)
            
            status = MeasurementStatus.SUCCESS if solution_correct else MeasurementStatus.INCORRECT_SOLUTION
            
            return JouleTraceMeasurementResult(
                request_id=request.request_id,
                candidate_id=request.candidate_id,
                problem_name=request.problem_name,
                validation=validation_result,
                status=status,
                measurement_environment={'mode': 'quick_check', 'energy_measurement': False}
            )
            
        except Exception as e:
            logger.error(f"Quick check failed: {e}", exc_info=True)
            return JouleTraceMeasurementResult(
                request_id=request.request_id,
                candidate_id=request.candidate_id,
                problem_name=request.problem_name,
                validation=None,
                status=MeasurementStatus.EXECUTION_ERROR,
                error_details=f"Quick check error: {e}"
            )
    
    def get_pipeline_info(self) -> dict:
        """Get information about the current pipeline configuration."""
        return {
            'energy_meter_available': self.energy_meter is not None,
            'energy_meter_type': type(self.energy_meter).__name__ if self.energy_meter else None,
            'validator_config': self.validator.config.__dict__ if self.validator.config else {},
            'current_isolation_active': self._current_isolation_manager is not None,
        }