"""
Statistical aggregation for energy measurements.

Runs multiple trials and calculates robust statistics with adaptive stopping
for efficient measurements while maintaining quality.
"""

import time
import statistics
from typing import List, Any, Dict, Optional
from dataclasses import dataclass

from jouletrace.core.socket_executor import SocketExecutor, ExecutionResult


@dataclass
class AggregatedResult:
    """Aggregated statistics from multiple measurement trials."""
    
    # Energy statistics (net energy after baseline subtraction)
    median_energy_joules: float
    mean_energy_joules: float
    stddev_energy_joules: float
    cv_percent: float
    # Package and DRAM breakdown
    median_pkg_energy_joules: float
    median_dram_energy_joules: float
    
    # Timing statistics
    median_time_seconds: float
    mean_time_seconds: float
    
    # Power statistics (derived)
    median_power_watts: float
    mean_power_watts: float
    
    # Trial metadata
    successful_trials: int
    failed_trials: int
    total_trials: int
    
    # Individual trial results
    trial_energies: List[float]
    trial_times: List[float]
    
    # Confidence metrics
    confidence_level: str  # "high", "medium", "low"
    early_stop: bool
    early_stop_reason: Optional[str] = None


class StatisticalAggregator:
    """
    Aggregates multiple energy measurements with adaptive stopping.
    
    Features:
    - Runs multiple trials for statistical significance
    - Adaptive stopping when CV < target threshold
    - Outlier detection and handling
    - Confidence level reporting
    """
    
    def __init__(self,
                 min_trials: int = 3,
                 max_trials: int = 20,
                 target_cv_percent: float = 5.0,
                 early_stop_enabled: bool = True,
                 cooldown_seconds: float = 0.5,
                 min_trial_wall_time_seconds: float = 0.1):
        """
        Args:
            min_trials: Minimum trials before considering early stop
            max_trials: Maximum trials to run
            target_cv_percent: Target CV% for early stopping
            early_stop_enabled: Whether to enable adaptive stopping
            cooldown_seconds: Delay between trials
        """
        self.min_trials = min_trials
        self.max_trials = max_trials
        self.target_cv_percent = target_cv_percent
        self.early_stop_enabled = early_stop_enabled
        self.cooldown_seconds = cooldown_seconds
        self.min_trial_wall_time_seconds = min_trial_wall_time_seconds
        
        self.executor: Optional[SocketExecutor] = None
    
    def setup(self, executor: SocketExecutor) -> None:
        """Initialize with an executor."""
        self.executor = executor
    
    def _calculate_cv(self, values: List[float]) -> float:
        """Calculate coefficient of variation (CV%)."""
        if len(values) < 2:
            return 0.0
        
        mean = statistics.mean(values)
        if mean == 0:
            return 0.0
        
        stddev = statistics.stdev(values)
        return (stddev / mean) * 100.0
    
    def _assess_confidence(self, cv_percent: float, n_trials: int) -> str:
        """
        Assess confidence level based on CV% and sample size.
        
        Returns:
            "high", "medium", or "low"
        """
        if cv_percent < 5.0 and n_trials >= self.min_trials:
            return "high"
        elif cv_percent < 10.0 and n_trials >= self.min_trials:
            return "medium"
        else:
            return "low"
    
    def _should_stop_early(self, 
                          energies: List[float], 
                          trial_num: int) -> tuple[bool, Optional[str]]:
        """
        Check if we should stop early based on convergence.
        
        Returns:
            (should_stop, reason)
        """
        if not self.early_stop_enabled:
            return False, None
        
        if trial_num < self.min_trials:
            return False, None
        
        # Calculate current CV
        cv = self._calculate_cv(energies)
        
        # Stop if we've achieved target CV
        if cv < self.target_cv_percent:
            return True, f"Achieved target CV={cv:.2f}% < {self.target_cv_percent}%"
        
        # Continue if below max trials
        if trial_num < self.max_trials:
            return False, None
        
        # Hit max trials
        return True, f"Reached max trials ({self.max_trials})"
    
    def aggregate_measurements(self,
                              code: str,
                              function_name: str,
                              test_inputs: List[Any],
                              verbose: bool = True) -> AggregatedResult:
        """
        Run multiple trials and aggregate results.
        
        Args:
            code: Python code containing function
            function_name: Function to measure
            test_inputs: Test inputs for function
            verbose: Print progress messages
            
        Returns:
            AggregatedResult with statistics and metadata
        """
        if not self.executor:
            raise RuntimeError("Aggregator not initialized. Call setup() first.")
        
        if verbose:
            print(f"\nRunning measurements (min={self.min_trials}, max={self.max_trials})...")
            print(f"Target CV: {self.target_cv_percent}%")
            print(f"Early stop: {'enabled' if self.early_stop_enabled else 'disabled'}\n")
        
        successful_results: List[ExecutionResult] = []
        failed_count = 0
        early_stopped = False
        stop_reason = None
        
        for trial in range(self.max_trials):
            if verbose:
                print(f"Trial {trial + 1}/{self.max_trials}...", end=" ", flush=True)
            
            # Execute trial
            result = self.executor.execute_single_trial(
                code=code,
                function_name=function_name,
                test_inputs=test_inputs,
                trial_number=trial,
                verify_idle=True,
                min_wall_time_seconds=self.min_trial_wall_time_seconds
            )
            
            if result.success:
                successful_results.append(result)
                if verbose:
                    print(f"✓ {result.net_energy_joules:.3f}J, {result.execution_time_seconds:.3f}s")
            else:
                failed_count += 1
                if verbose:
                    print(f"✗ Failed: {result.error_message}")
                continue
            
            # Check if we have enough successful trials
            if len(successful_results) >= self.min_trials:
                energies = [r.net_energy_joules for r in successful_results]
                should_stop, reason = self._should_stop_early(energies, len(successful_results))
                
                if should_stop:
                    early_stopped = True
                    stop_reason = reason
                    if verbose:
                        print(f"\n✓ Early stop: {reason}")
                    break
            
            # Cooldown between trials
            if trial < self.max_trials - 1:
                time.sleep(self.cooldown_seconds)
        
        # Check if we have enough data
        if len(successful_results) == 0:
            raise RuntimeError("All trials failed")
        
        if len(successful_results) < self.min_trials:
            print(f"\n⚠ Warning: Only {len(successful_results)} successful trials (min={self.min_trials})")
        
        # Extract data
        energies = [r.net_energy_joules for r in successful_results]
        pkg_energies = [r.package_net_energy_joules for r in successful_results]
        dram_energies = [r.dram_energy_joules for r in successful_results]
        times = [r.execution_time_seconds for r in successful_results]
        
        # Calculate statistics
        median_energy = statistics.median(energies)
        median_pkg = statistics.median(pkg_energies)
        median_dram = statistics.median(dram_energies)
        mean_energy = statistics.mean(energies)
        stddev_energy = statistics.stdev(energies) if len(energies) > 1 else 0.0
        cv_percent = self._calculate_cv(energies)
        
        median_time = statistics.median(times)
        mean_time = statistics.mean(times)
        
        # Calculate power statistics
        median_power = median_energy / median_time if median_time > 0 else 0.0
        mean_power = mean_energy / mean_time if mean_time > 0 else 0.0
        
        # Assess confidence
        confidence = self._assess_confidence(cv_percent, len(successful_results))
        
        # Print summary
        if verbose:
            print(f"\n{'='*60}")
            print("Measurement Summary")
            print(f"{'='*60}")
            print(f"Successful trials: {len(successful_results)}/{trial + 1}")
            print(f"Failed trials:     {failed_count}")
            print(f"\nEnergy Statistics:")
            print(f"  Median:          {median_energy:.6f}J")
            print(f"  Mean:            {mean_energy:.6f}J")
            print(f"  Std Dev:         {stddev_energy:.6f}J")
            print(f"  CV:              {cv_percent:.2f}%")
            print(f"\nTiming Statistics:")
            print(f"  Median:          {median_time:.6f}s")
            print(f"  Mean:            {mean_time:.6f}s")
            print(f"\nPower Statistics:")
            print(f"  Median:          {median_power:.3f}W")
            print(f"  Mean:            {mean_power:.3f}W")
            print(f"\nConfidence:        {confidence.upper()}")
            if early_stopped:
                print(f"Early stopped:     Yes ({stop_reason})")
            print(f"{'='*60}\n")
        
        return AggregatedResult(
            median_energy_joules=median_energy,
            mean_energy_joules=mean_energy,
            stddev_energy_joules=stddev_energy,
            cv_percent=cv_percent,
            median_pkg_energy_joules=median_pkg,
            median_dram_energy_joules=median_dram,
            median_time_seconds=median_time,
            mean_time_seconds=mean_time,
            median_power_watts=median_power,
            mean_power_watts=mean_power,
            successful_trials=len(successful_results),
            failed_trials=failed_count,
            total_trials=trial + 1,
            trial_energies=energies,
            trial_times=times,
            confidence_level=confidence,
            early_stop=early_stopped,
            early_stop_reason=stop_reason
        )


# Standalone test
if __name__ == '__main__':
    print("Statistical Aggregator Test\n")
    
    # Test code: fibonacci
    test_code = '''
def fibonacci(n):
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b
'''
    
    # Test inputs - enough iterations to reach 100ms
    test_inputs = [35] * 50000
    
    # Create executor and aggregator
    executor = SocketExecutor(cpu_core=4)
    aggregator = StatisticalAggregator(
        min_trials=3,
        max_trials=10,
        target_cv_percent=5.0,
        early_stop_enabled=True,
        cooldown_seconds=0.5
    )
    
    try:
        print("Setting up...")
        executor.setup()
        aggregator.setup(executor)
        
        print(f"\nTest: fibonacci(35) × {len(test_inputs)} iterations")
        
        # Run aggregated measurement
        result = aggregator.aggregate_measurements(
            code=test_code,
            function_name='fibonacci',
            test_inputs=test_inputs,
            verbose=True
        )
        
        # Additional analysis
        print("Trial Data:")
        for i, (energy, time_val) in enumerate(zip(result.trial_energies, result.trial_times)):
            power = energy / time_val if time_val > 0 else 0
            print(f"  Trial {i+1}: {energy:.6f}J, {time_val:.6f}s, {power:.3f}W")
        
    finally:
        executor.cleanup()
        print("\nCleanup complete")
