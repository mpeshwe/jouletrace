# jouletrace/core/validator.py
from __future__ import annotations
import logging
import math
from typing import Any, List, Dict, Tuple, Optional
from dataclasses import dataclass

from .models import (
    JouleTraceTestCase, 
    ValidationResult, 
    ComparisonConfig
)
from .executor import SafeCodeExecutor, ExecutionTimeout, ExecutionMemoryLimit, ExecutionError

logger = logging.getLogger(__name__)

@dataclass
class ValidationConfig:
    """Configuration for solution validation."""
    
    # Execution limits
    timeout_seconds: int = 30
    memory_limit_mb: int = 512
    
    # Output comparison
    comparison: ComparisonConfig = None
    
    # Validation behavior
    stop_on_first_failure: bool = False         # Whether to stop at first failed test
    max_failed_details: int = 10               # Maximum failed test details to store
    
    def __post_init__(self):
        if self.comparison is None:
            self.comparison = ComparisonConfig()

class OutputComparator:
    """Handles comparison between expected and actual outputs."""
    
    def __init__(self, config: ComparisonConfig):
        self.config = config
    
    def _compare_floats(self, expected: float, actual: float) -> bool:
        """Compare floating point numbers with tolerance."""
        if math.isnan(expected) and math.isnan(actual):
            return True
        if math.isinf(expected) and math.isinf(actual):
            return expected == actual  # Same sign of infinity
        
        # Absolute tolerance check
        if abs(expected - actual) <= self.config.float_tolerance:
            return True
        
        # Relative tolerance check
        if expected != 0:
            relative_error = abs((expected - actual) / expected)
            return relative_error <= self.config.relative_tolerance
        
        return False
    
    def _compare_strings(self, expected: str, actual: str) -> bool:
        """Compare strings according to configuration."""
        if not self.config.string_case_sensitive:
            expected = expected.lower()
            actual = actual.lower()
        
        if self.config.ignore_whitespace:
            expected = ''.join(expected.split())
            actual = ''.join(actual.split())
        
        return expected == actual
    
    def _compare_lists(self, expected: List[Any], actual: List[Any]) -> bool:
        """Compare lists/arrays according to configuration."""
        if len(expected) != len(actual):
            return False
        
        if not self.config.list_order_matters:
            # Sort both lists for comparison if order doesn't matter
            try:
                expected = sorted(expected)
                actual = sorted(actual)
            except TypeError:
                # Can't sort mixed types, fall back to set comparison
                try:
                    return set(expected) == set(actual)
                except TypeError:
                    # Elements not hashable, compare as-is
                    pass
        
        return all(self.compare(e, a) for e, a in zip(expected, actual))
    
    def compare(self, expected: Any, actual: Any) -> bool:
        """
        Compare expected and actual outputs with appropriate comparison logic.
        
        Returns:
            True if outputs match according to configuration, False otherwise.
        """
        # Handle None cases
        if expected is None and actual is None:
            return True
        if expected is None or actual is None:
            return False
        
        # Type mismatch check
        if type(expected) != type(actual):
            # Allow int/float comparison
            if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                return self._compare_floats(float(expected), float(actual))
            return False
        
        # Type-specific comparison
        if isinstance(expected, float):
            return self._compare_floats(expected, actual)
        
        elif isinstance(expected, str):
            return self._compare_strings(expected, actual)
        
        elif isinstance(expected, (list, tuple)):
            return self._compare_lists(list(expected), list(actual))
        
        elif isinstance(expected, dict):
            if set(expected.keys()) != set(actual.keys()):
                return False
            return all(self.compare(expected[k], actual[k]) for k in expected.keys())
        
        elif isinstance(expected, set):
            return expected == actual
        
        else:
            # Default comparison for other types
            return expected == actual

class SolutionValidator:
    """
    Validates candidate solutions against test cases.
    Implements the correctness gate for energy measurement.
    """
    
    def __init__(self, config: ValidationConfig = None):
        self.config = config or ValidationConfig()
        self.executor = SafeCodeExecutor()
        self.comparator = OutputComparator(self.config.comparison)
    
    def _create_test_failure_detail(self, 
                                   test_case: JouleTraceTestCase, 
                                   actual_output: Any,
                                   error_message: Optional[str] = None) -> Dict[str, Any]:
        """Create detailed information about a test failure."""
        detail = {
            'test_id': test_case.test_id,
            'inputs': test_case.inputs,
            'expected_output': test_case.expected_output,
            'actual_output': actual_output,
        }
        
        if error_message:
            detail['error_message'] = error_message
        
        # Add metadata if available
        if test_case.metadata:
            detail['metadata'] = test_case.metadata
        
        return detail
    
    def _validate_single_test(self, 
                             code: str, 
                             function_name: str,
                             test_case: JouleTraceTestCase) -> Tuple[bool, Any, Optional[str]]:
        """
        Validate a single test case.
        
        Returns:
            Tuple of (is_correct, actual_output, error_message)
        """
        try:
            actual_output, exec_time, memory_used = self.executor.execute_test_case(
                code=code,
                function_name=function_name,
                test_case=test_case,
                timeout_seconds=self.config.timeout_seconds,
                memory_limit_mb=self.config.memory_limit_mb
            )
            
            # Compare outputs
            is_correct = self.comparator.compare(test_case.expected_output, actual_output)
            
            if is_correct:
                logger.debug(f"Test {test_case.test_id} passed in {exec_time:.3f}s")
            else:
                logger.debug(f"Test {test_case.test_id} failed: expected {test_case.expected_output}, got {actual_output}")
            
            return is_correct, actual_output, None
            
        except ExecutionTimeout:
            error_msg = f"Execution timed out after {self.config.timeout_seconds}s"
            logger.warning(f"Test {test_case.test_id} timed out")
            return False, None, error_msg
            
        except ExecutionMemoryLimit:
            error_msg = f"Execution exceeded memory limit of {self.config.memory_limit_mb}MB"
            logger.warning(f"Test {test_case.test_id} exceeded memory limit")
            return False, None, error_msg
            
        except ExecutionError as e:
            error_msg = str(e)
            logger.warning(f"Test {test_case.test_id} execution error: {error_msg}")
            return False, None, error_msg
    
    def validate_solution(self, 
                         candidate_code: str, 
                         function_name: str,
                         test_cases: List[JouleTraceTestCase]) -> ValidationResult:
        """
        Validate candidate solution against all test cases.
        
        This is the main validation interface that implements the correctness gate.
        Energy measurement only proceeds if this returns ValidationResult.is_correct = True.
        
        Returns:
            ValidationResult with detailed validation outcome.
        """
        logger.info(f"Validating solution with {len(test_cases)} test cases")
        
        # Pre-validation checks
        syntax_valid, syntax_error = self.executor.validate_code_syntax(candidate_code)
        if not syntax_valid:
            logger.error(f"Code syntax validation failed: {syntax_error}")
            return ValidationResult(
                is_correct=False,
                passed_tests=0,
                total_tests=len(test_cases),
                execution_errors=[f"Syntax error: {syntax_error}"]
            )
        
        function_valid, function_error = self.executor.validate_function_exists(candidate_code, function_name)
        if not function_valid:
            logger.error(f"Function validation failed: {function_error}")
            return ValidationResult(
                is_correct=False,
                passed_tests=0,
                total_tests=len(test_cases),
                execution_errors=[f"Function error: {function_error}"]
            )
        
        # Run validation on all test cases
        passed_tests = 0
        failed_test_details = []
        execution_errors = []
        actual_outputs = []
        expected_outputs = []
        
        for i, test_case in enumerate(test_cases):
            is_correct, actual_output, error_message = self._validate_single_test(
                candidate_code, function_name, test_case
            )
            
            actual_outputs.append(actual_output)
            expected_outputs.append(test_case.expected_output)
            
            if is_correct:
                passed_tests += 1
            else:
                # Record failure details
                if len(failed_test_details) < self.config.max_failed_details:
                    failure_detail = self._create_test_failure_detail(
                        test_case, actual_output, error_message
                    )
                    failed_test_details.append(failure_detail)
                
                if error_message:
                    execution_errors.append(f"Test {test_case.test_id}: {error_message}")
                
                # Early termination if configured
                if self.config.stop_on_first_failure:
                    logger.info(f"Stopping validation on first failure (test {test_case.test_id})")
                    break
        
        # Determine overall correctness
        is_correct = (passed_tests == len(test_cases))
        
        result = ValidationResult(
            is_correct=is_correct,
            passed_tests=passed_tests,
            total_tests=len(test_cases),
            failed_test_details=failed_test_details,
            execution_errors=execution_errors,
            actual_outputs=actual_outputs,
            expected_outputs=expected_outputs
        )
        
        if is_correct:
            logger.info(f"Solution validation PASSED: {passed_tests}/{len(test_cases)} tests passed")
        else:
            logger.warning(f"Solution validation FAILED: {passed_tests}/{len(test_cases)} tests passed")
        
        return result
    
    def quick_validate(self, 
                      candidate_code: str, 
                      function_name: str,
                      test_cases: List[JouleTraceTestCase],
                      max_tests: int = 5) -> ValidationResult:
        """
        Quick validation using a subset of test cases.
        Useful for fast feedback before full validation.
        
        Args:
            max_tests: Maximum number of test cases to run for quick validation.
        """
        if len(test_cases) <= max_tests:
            return self.validate_solution(candidate_code, function_name, test_cases)
        
        # Use first N test cases for quick validation
        quick_test_cases = test_cases[:max_tests]
        logger.info(f"Quick validation using {len(quick_test_cases)}/{len(test_cases)} test cases")
        
        return self.validate_solution(candidate_code, function_name, quick_test_cases)
    
    def validate_with_custom_comparator(self,
                                       candidate_code: str,
                                       function_name: str, 
                                       test_cases: List[JouleTraceTestCase],
                                       comparison_config: ComparisonConfig) -> ValidationResult:
        """
        Validate with custom comparison configuration.
        Useful for problems with specific output format requirements.
        """
        # Temporarily override comparator
        original_comparator = self.comparator
        self.comparator = OutputComparator(comparison_config)
        
        try:
            return self.validate_solution(candidate_code, function_name, test_cases)
        finally:
            # Restore original comparator
            self.comparator = original_comparator