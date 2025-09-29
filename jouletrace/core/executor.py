# jouletrace/core/executor.py
from __future__ import annotations
import time
import signal
import resource
import psutil
import tempfile
import os
from typing import Any, List, Dict, Callable, Tuple, Optional
from contextlib import contextmanager
import logging

from .models import JouleTraceTestCase, InputArgs

logger = logging.getLogger(__name__)

class ExecutionTimeout(Exception):
    """Raised when code execution exceeds timeout."""
    pass

class ExecutionMemoryLimit(Exception):
    """Raised when code execution exceeds memory limit."""
    pass

class ExecutionError(Exception):
    """Raised when code execution fails."""
    pass

class SafeCodeExecutor:
    """
    Safe execution engine for user code with proper isolation.
    Core foundation for both validation and energy measurement.
    """
    
    def __init__(self):
        self.safe_builtins = self._create_safe_builtins()
        self.safe_modules = self._create_safe_modules()
    
    def _create_safe_builtins(self) -> Dict[str, Any]:
        """Create restricted builtins that prevent dangerous operations."""
        # Allow essential operations, block dangerous ones
        safe_names = {
            # Basic types and operations
            'abs', 'all', 'any', 'bin', 'bool', 'chr', 'dict', 'divmod',
            'enumerate', 'filter', 'float', 'frozenset', 'hex', 'int',
            'len', 'list', 'map', 'max', 'min', 'oct', 'ord', 'pow',
            'range', 'reversed', 'round', 'set', 'slice', 'sorted', 'str',
            'sum', 'tuple', 'zip', 'type', 'isinstance', 'issubclass',

            # Minimal support for classes
            '__build_class__', 'object', 'super', 'staticmethod', 'classmethod', 'property',
            
            # Function introspection (safe)
            'hasattr', 'getattr', 'setattr', 'delattr', 'callable',
            'iter', 'next', 'vars', 'dir',
            
            # Math operations
            'abs', 'divmod', 'pow', 'round',
            
            # Exception handling
            'Exception', 'ValueError', 'TypeError', 'IndexError', 'KeyError',
            'AttributeError', 'StopIteration',
        }
        
        import builtins
        safe_builtins = {}
        
        for name in safe_names:
            if hasattr(builtins, name):
                safe_builtins[name] = getattr(builtins, name)
        # Restricted __import__: allow only a small whitelist of stdlib modules
        allowed_import_roots = {
            'math', 'collections', 'itertools', 'functools', 'heapq', 'bisect', 'random', 'typing'
        }

        def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split('.', 1)[0]
            if root in allowed_import_roots:
                import importlib
                return importlib.import_module(name)
            raise ImportError(f"Module not allowed: {name}")

        safe_builtins['__import__'] = _safe_import

        return safe_builtins
    
    def _create_safe_modules(self) -> Dict[str, Any]:
        """Create safe module imports for common algorithms."""
        safe_imports = {}
        
        # Math operations
        try:
            import math
            safe_imports['math'] = math
        except ImportError:
            pass
        
        # Collections and data structures
        try:
            import collections
            safe_imports['collections'] = collections
        except ImportError:
            pass
        
        try:
            import itertools
            safe_imports['itertools'] = itertools
        except ImportError:
            pass
        
        try:
            import functools
            safe_imports['functools'] = functools
        except ImportError:
            pass
        
        try:
            import heapq
            safe_imports['heapq'] = heapq
        except ImportError:
            pass
        
        try:
            import bisect
            safe_imports['bisect'] = bisect
        except ImportError:
            pass
        
        # Controlled random for algorithms that need it
        try:
            import random
            safe_imports['random'] = random
        except ImportError:
            pass
        
        # Typing for modern Python code
        try:
            import typing
            safe_imports.update({
                'List': typing.List,
                'Dict': typing.Dict,
                'Set': typing.Set,
                'Tuple': typing.Tuple,
                'Optional': typing.Optional,
                'Union': typing.Union,
                'Any': typing.Any,
            })
        except ImportError:
            pass
        
        return safe_imports
    
    def _create_execution_namespace(self) -> Dict[str, Any]:
        """Create a safe execution namespace."""
        # Provide minimal module globals expected by Python/class machinery
        return {
            '__builtins__': self.safe_builtins,
            '__name__': '__main__',
            '__package__': None,
            '__doc__': None,
            '__spec__': None,
            **self.safe_modules,
        }
    
    @contextmanager
    def _resource_limits(self, memory_limit_mb: int, timeout_seconds: int):
        """Apply resource limits during execution.

        Never attempts to raise the hard limit; clamps soft limit to current hard limit.
        """

        RLIM_INFINITY = getattr(resource, "RLIM_INFINITY", -1)

        # Memory limit (address space)
        memory_bytes = int(memory_limit_mb) * 1024 * 1024
        old_mem_soft, old_mem_hard = resource.getrlimit(resource.RLIMIT_AS)
        # Clamp soft to current hard (unless unlimited), never raise hard
        if old_mem_hard == RLIM_INFINITY or old_mem_hard < 0:
            new_mem_soft = memory_bytes
            new_mem_hard = old_mem_hard
        else:
            new_mem_soft = min(memory_bytes, old_mem_hard)
            new_mem_hard = old_mem_hard
        try:
            resource.setrlimit(resource.RLIMIT_AS, (new_mem_soft, new_mem_hard))
        except (ValueError, OSError) as e:
            # If we cannot set RLIMIT_AS, continue with existing limits
            logger.warning(f"Unable to set RLIMIT_AS: {e}")
            new_mem_soft, new_mem_hard = old_mem_soft, old_mem_hard

        # Do NOT set RLIMIT_CPU here: exceeding soft CPU time sends SIGXCPU
        # to the entire worker process and can kill the Celery worker.
        # We rely on SIGALRM for wall-clock timeout enforcement instead.
        old_cpu_soft = old_cpu_hard = None
        
        # Set up timeout signal
        def timeout_handler(signum, frame):
            raise ExecutionTimeout(f"Execution timed out after {timeout_seconds} seconds")
        
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout_seconds)
        
        try:
            yield
        finally:
            # Restore limits and handlers
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            try:
                resource.setrlimit(resource.RLIMIT_AS, (old_mem_soft, old_mem_hard))
            except Exception:
                pass
            # No RLIMIT_CPU changes to restore
    
    def _load_function(self, code: str, function_name: str) -> Callable:
        """Load and validate the target function from user code."""
        namespace = self._create_execution_namespace()
        
        try:
            # Execute user code in safe namespace
            exec(code, namespace)
        except SyntaxError as e:
            raise ExecutionError(f"Syntax error in code: {e}")
        except Exception as e:
            raise ExecutionError(f"Failed to load code: {type(e).__name__}: {e}")
        
        # Check if target function exists
        if function_name not in namespace:
            available_functions = [name for name, obj in namespace.items() 
                                 if callable(obj) and not name.startswith('_')]
            raise ExecutionError(
                f"Function '{function_name}' not found. "
                f"Available functions: {available_functions}"
            )
        
        func = namespace[function_name]
        if not callable(func):
            raise ExecutionError(f"'{function_name}' is not callable")
        
        return func
    
    def _prepare_function_call(self, inputs: InputArgs) -> Tuple[tuple, dict]:
        """Convert flexible input format to function call arguments."""
        
        if isinstance(inputs, dict):
            # Keyword arguments: inputs = {"x": 1, "y": 2} → func(**inputs)
            return (), inputs
        
        elif isinstance(inputs, (list, tuple)):
            # Multiple positional arguments: inputs = [1, 2] → func(*inputs)  
            return tuple(inputs), {}
        
        else:
            # Single argument: inputs = 5 → func(inputs)
            return (inputs,), {}
    
    def _measure_memory_usage(self) -> float:
        """Get current process memory usage in MB."""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            return memory_info.rss / (1024 * 1024)  # Convert bytes to MB
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0
    
    def execute_function(self, 
                        code: str, 
                        function_name: str,
                        inputs: InputArgs,
                        timeout_seconds: int = 30,
                        memory_limit_mb: int = 512) -> Tuple[Any, float, float]:
        """
        Execute user function with given inputs.
        
        Returns:
            Tuple of (output, execution_time_seconds, memory_used_mb)
        
        Raises:
            ExecutionTimeout: If execution exceeds timeout
            ExecutionMemoryLimit: If execution exceeds memory limit  
            ExecutionError: If execution fails for other reasons
        """
        
        # Memory tracking
        initial_memory = self._measure_memory_usage()
        
        # Load the function
        func = self._load_function(code, function_name)
        
        # Prepare function arguments
        args, kwargs = self._prepare_function_call(inputs)
        
        # Execute with resource limits
        try:
            with self._resource_limits(memory_limit_mb, timeout_seconds):
                start_time = time.perf_counter()
                
                try:
                    result = func(*args, **kwargs)
                except MemoryError:
                    raise ExecutionMemoryLimit("Function exceeded memory limit")
                except RecursionError:
                    raise ExecutionError("Function exceeded recursion limit")
                except Exception as e:
                    raise ExecutionError(f"Function execution failed: {type(e).__name__}: {e}")
                
                execution_time = time.perf_counter() - start_time
                
        except ExecutionTimeout:
            raise
        except ExecutionMemoryLimit:
            raise
        except Exception as e:
            raise ExecutionError(f"Execution environment error: {e}")
        
        # Calculate memory usage
        final_memory = self._measure_memory_usage()
        memory_used = max(0.0, final_memory - initial_memory)
        
        return result, execution_time, memory_used
    
    def execute_test_case(self,
                         code: str,
                         function_name: str, 
                         test_case: JouleTraceTestCase,
                         timeout_seconds: int = 30,
                         memory_limit_mb: int = 512) -> Tuple[Any, float, float]:
        """
        Execute a single test case.
        Convenience wrapper around execute_function.
        """
        return self.execute_function(
            code=code,
            function_name=function_name,
            inputs=test_case.inputs,
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb
        )
    
    def execute_multiple_test_cases(self,
                                   code: str,
                                   function_name: str,
                                   test_cases: List[JouleTraceTestCase],
                                   timeout_seconds: int = 30,
                                   memory_limit_mb: int = 512) -> List[Tuple[Any, float, float, Optional[str]]]:
        """
        Execute multiple test cases, collecting all results.
        
        Returns:
            List of (output, execution_time, memory_used, error_message) tuples.
            error_message is None if execution was successful.
        """
        results = []
        
        for i, test_case in enumerate(test_cases):
            try:
                output, exec_time, memory_used = self.execute_test_case(
                    code, function_name, test_case, timeout_seconds, memory_limit_mb
                )
                results.append((output, exec_time, memory_used, None))
                
            except (ExecutionTimeout, ExecutionMemoryLimit, ExecutionError) as e:
                # Log execution failure but continue with other test cases
                logger.warning(f"Test case {i} ({test_case.test_id}) failed: {e}")
                results.append((None, 0.0, 0.0, str(e)))
        
        return results
    
    def validate_code_syntax(self, code: str) -> Tuple[bool, str]:
        """
        Quick validation that code can be compiled.
        Fast pre-check before expensive execution.
        """
        try:
            compile(code, '<user_code>', 'exec')
            return True, "Code syntax is valid"
        except SyntaxError as e:
            return False, f"Syntax error: {e}"
        except Exception as e:
            return False, f"Compilation error: {e}"
    
    def validate_function_exists(self, code: str, function_name: str) -> Tuple[bool, str]:
        """
        Validate that the specified function exists in the code.
        """
        try:
            self._load_function(code, function_name)
            return True, f"Function '{function_name}' found and callable"
        except ExecutionError as e:
            return False, str(e)
