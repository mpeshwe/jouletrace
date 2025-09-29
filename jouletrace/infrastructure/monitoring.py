# jouletrace/infrastructure/monitoring.py
from __future__ import annotations
import time
import psutil
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque
import logging
import threading

from .config import get_config

logger = logging.getLogger(__name__)

@dataclass
class MeasurementMetrics:
    """Metrics for a single energy measurement."""
    task_id: str
    request_id: str
    timestamp: float
    duration_seconds: float
    correct: bool
    trials: int
    test_cases: int
    package_energy_j: Optional[float] = None
    ram_energy_j: Optional[float] = None
    total_energy_j: Optional[float] = None

@dataclass
class SystemMetrics:
    """Current system resource metrics."""
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_available_mb: float
    disk_usage_percent: float
    load_average_1m: float
    load_average_5m: float
    load_average_15m: float

@dataclass
class APIMetrics:
    """API request metrics."""
    timestamp: float
    endpoint: str
    method: str
    status_code: int
    duration_ms: float
    request_size_bytes: int = 0
    response_size_bytes: int = 0

class MetricsCollector:
    """
    Central metrics collector for JouleTrace.
    
    Tracks:
    - Energy measurements
    - System resource usage
    - API request metrics
    - Task queue statistics
    """
    
    def __init__(self, retention_hours: int = 24):
        self.retention_hours = retention_hours
        self.retention_seconds = retention_hours * 3600
        
        # Metrics storage (thread-safe with lock)
        self._lock = threading.Lock()
        self._measurement_metrics: deque[MeasurementMetrics] = deque(maxlen=10000)
        self._system_metrics: deque[SystemMetrics] = deque(maxlen=1440)  # 24h at 1min intervals
        self._api_metrics: deque[APIMetrics] = deque(maxlen=10000)
        
        # Aggregated statistics
        self._stats = {
            "total_measurements": 0,
            "successful_measurements": 0,
            "failed_measurements": 0,
            "total_api_requests": 0,
            "api_errors": 0,
        }
        
        # Performance tracking
        self._measurement_durations: deque[float] = deque(maxlen=1000)
        self._energy_values: deque[float] = deque(maxlen=1000)
    
    def record_measurement(self, metrics: MeasurementMetrics) -> None:
        """Record energy measurement metrics."""
        with self._lock:
            self._measurement_metrics.append(metrics)
            self._stats["total_measurements"] += 1
            
            if metrics.correct:
                self._stats["successful_measurements"] += 1
                self._measurement_durations.append(metrics.duration_seconds)
                
                if metrics.total_energy_j is not None:
                    self._energy_values.append(metrics.total_energy_j)
            else:
                self._stats["failed_measurements"] += 1
        
        logger.debug(f"Recorded measurement metrics: {metrics.task_id}")
    
    def record_system_metrics(self) -> SystemMetrics:
        """Record current system metrics."""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            load_avg = psutil.getloadavg()
            
            metrics = SystemMetrics(
                timestamp=time.time(),
                cpu_percent=cpu_percent,
                memory_percent=memory.percent,
                memory_used_mb=memory.used / (1024 * 1024),
                memory_available_mb=memory.available / (1024 * 1024),
                disk_usage_percent=disk.percent,
                load_average_1m=load_avg[0],
                load_average_5m=load_avg[1],
                load_average_15m=load_avg[2]
            )
            
            with self._lock:
                self._system_metrics.append(metrics)
            
            return metrics
            
        except Exception as e:
            logger.error(f"Failed to collect system metrics: {e}")
            return SystemMetrics(
                timestamp=time.time(),
                cpu_percent=0.0,
                memory_percent=0.0,
                memory_used_mb=0.0,
                memory_available_mb=0.0,
                disk_usage_percent=0.0,
                load_average_1m=0.0,
                load_average_5m=0.0,
                load_average_15m=0.0
            )
    
    def record_api_request(self, metrics: APIMetrics) -> None:
        """Record API request metrics."""
        with self._lock:
            self._api_metrics.append(metrics)
            self._stats["total_api_requests"] += 1
            
            if metrics.status_code >= 400:
                self._stats["api_errors"] += 1
        
        logger.debug(f"Recorded API metrics: {metrics.method} {metrics.endpoint} - {metrics.status_code}")
    
    def get_measurement_statistics(self, hours: Optional[int] = None) -> Dict[str, Any]:
        """Get aggregated measurement statistics."""
        cutoff_time = time.time() - (hours * 3600 if hours else self.retention_seconds)
        
        with self._lock:
            recent_measurements = [m for m in self._measurement_metrics if m.timestamp >= cutoff_time]
            
            if not recent_measurements:
                return {
                    "count": 0,
                    "success_rate": 0.0,
                    "average_duration_seconds": 0.0,
                    "median_energy_joules": 0.0
                }
            
            successful = [m for m in recent_measurements if m.correct]
            
            # Calculate statistics
            total_count = len(recent_measurements)
            success_count = len(successful)
            success_rate = (success_count / total_count * 100) if total_count > 0 else 0.0
            
            # Duration statistics
            durations = [m.duration_seconds for m in recent_measurements]
            avg_duration = sum(durations) / len(durations) if durations else 0.0
            
            # Energy statistics
            energy_values = [m.total_energy_j for m in successful 
                           if m.total_energy_j is not None]
            median_energy = sorted(energy_values)[len(energy_values)//2] if energy_values else 0.0
            
            return {
                "count": total_count,
                "successful": success_count,
                "failed": total_count - success_count,
                "success_rate": success_rate,
                "average_duration_seconds": avg_duration,
                "median_energy_joules": median_energy,
                "min_energy_joules": min(energy_values) if energy_values else 0.0,
                "max_energy_joules": max(energy_values) if energy_values else 0.0,
            }
    
    def get_api_statistics(self, hours: Optional[int] = None) -> Dict[str, Any]:
        """Get aggregated API statistics."""
        cutoff_time = time.time() - (hours * 3600 if hours else self.retention_seconds)
        
        with self._lock:
            recent_requests = [m for m in self._api_metrics if m.timestamp >= cutoff_time]
            
            if not recent_requests:
                return {
                    "total_requests": 0,
                    "error_rate": 0.0,
                    "average_duration_ms": 0.0
                }
            
            total = len(recent_requests)
            errors = sum(1 for m in recent_requests if m.status_code >= 400)
            error_rate = (errors / total * 100) if total > 0 else 0.0
            
            durations = [m.duration_ms for m in recent_requests]
            avg_duration = sum(durations) / len(durations) if durations else 0.0
            
            # Requests by endpoint
            endpoint_counts = defaultdict(int)
            for m in recent_requests:
                endpoint_counts[f"{m.method} {m.endpoint}"] += 1
            
            return {
                "total_requests": total,
                "errors": errors,
                "error_rate": error_rate,
                "average_duration_ms": avg_duration,
                "requests_by_endpoint": dict(sorted(endpoint_counts.items(), 
                                                   key=lambda x: x[1], reverse=True)[:10])
            }
    
    def get_system_health(self) -> Dict[str, Any]:
        """Get current system health status."""
        current_metrics = self.record_system_metrics()
        
        # Determine health status based on thresholds
        health_status = "healthy"
        issues = []
        
        if current_metrics.cpu_percent > 90:
            health_status = "degraded"
            issues.append("High CPU usage")
        
        if current_metrics.memory_percent > 90:
            health_status = "degraded"
            issues.append("High memory usage")
        
        if current_metrics.disk_usage_percent > 90:
            health_status = "degraded"
            issues.append("High disk usage")
        
        if current_metrics.load_average_1m > psutil.cpu_count() * 2:
            health_status = "degraded"
            issues.append("High system load")
        
        return {
            "status": health_status,
            "issues": issues,
            "metrics": {
                "cpu_percent": current_metrics.cpu_percent,
                "memory_percent": current_metrics.memory_percent,
                "disk_usage_percent": current_metrics.disk_usage_percent,
                "load_average": current_metrics.load_average_1m
            }
        }
    
    def get_comprehensive_report(self) -> Dict[str, Any]:
        """Get comprehensive monitoring report."""
        return {
            "timestamp": time.time(),
            "system_health": self.get_system_health(),
            "measurement_stats": {
                "last_hour": self.get_measurement_statistics(hours=1),
                "last_24_hours": self.get_measurement_statistics(hours=24),
            },
            "api_stats": {
                "last_hour": self.get_api_statistics(hours=1),
                "last_24_hours": self.get_api_statistics(hours=24),
            },
            "overall_stats": self._stats.copy()
        }

class PerformanceMonitor:
    """Monitor performance and detect anomalies."""
    
    def __init__(self, metrics_collector: MetricsCollector):
        self.metrics_collector = metrics_collector
        self.baseline_duration = 30.0  # seconds
        self.baseline_energy = 0.01  # joules
    
    def detect_slow_measurements(self, threshold_multiplier: float = 2.0) -> List[str]:
        """Detect measurements that took unusually long."""
        stats = self.metrics_collector.get_measurement_statistics(hours=1)
        avg_duration = stats.get("average_duration_seconds", self.baseline_duration)
        threshold = avg_duration * threshold_multiplier
        
        slow_measurements = []
        cutoff_time = time.time() - 3600  # Last hour
        
        for m in self.metrics_collector._measurement_metrics:
            if m.timestamp >= cutoff_time and m.duration_seconds > threshold:
                slow_measurements.append(m.task_id)
        
        return slow_measurements
    
    def detect_energy_anomalies(self, threshold_multiplier: float = 3.0) -> List[str]:
        """Detect measurements with unusually high energy consumption."""
        stats = self.metrics_collector.get_measurement_statistics(hours=1)
        median_energy = stats.get("median_energy_joules", self.baseline_energy)
        threshold = median_energy * threshold_multiplier
        
        anomalies = []
        cutoff_time = time.time() - 3600
        
        for m in self.metrics_collector._measurement_metrics:
            if (m.timestamp >= cutoff_time and 
                m.total_energy_j is not None and 
                m.total_energy_j > threshold):
                anomalies.append(m.task_id)
        
        return anomalies
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary with anomaly detection."""
        return {
            "slow_measurements": self.detect_slow_measurements(),
            "energy_anomalies": self.detect_energy_anomalies(),
            "system_health": self.metrics_collector.get_system_health()
        }

# Global metrics collector instance
_metrics_collector: Optional[MetricsCollector] = None
_performance_monitor: Optional[PerformanceMonitor] = None

def get_metrics_collector() -> MetricsCollector:
    """Get global metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        config = get_config()
        retention = 24  # 24 hours default
        _metrics_collector = MetricsCollector(retention_hours=retention)
    return _metrics_collector

def get_performance_monitor() -> PerformanceMonitor:
    """Get global performance monitor instance."""
    global _performance_monitor
    if _performance_monitor is None:
        _performance_monitor = PerformanceMonitor(get_metrics_collector())
    return _performance_monitor

# Background monitoring thread
class MonitoringThread(threading.Thread):
    """Background thread for continuous system monitoring."""
    
    def __init__(self, interval_seconds: int = 60):
        super().__init__(daemon=True)
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self.metrics_collector = get_metrics_collector()
    
    def run(self):
        """Run continuous monitoring loop."""
        logger.info("Monitoring thread started")
        
        while not self._stop_event.is_set():
            try:
                # Record system metrics
                self.metrics_collector.record_system_metrics()
                
                # Sleep until next interval
                self._stop_event.wait(self.interval_seconds)
                
            except Exception as e:
                logger.error(f"Error in monitoring thread: {e}", exc_info=True)
                self._stop_event.wait(self.interval_seconds)
    
    def stop(self):
        """Stop the monitoring thread."""
        logger.info("Stopping monitoring thread")
        self._stop_event.set()

_monitoring_thread: Optional[MonitoringThread] = None

def start_monitoring(interval_seconds: int = 60) -> None:
    """Start background monitoring."""
    global _monitoring_thread
    
    if _monitoring_thread is not None and _monitoring_thread.is_alive():
        logger.warning("Monitoring thread already running")
        return
    
    config = get_config()
    if not config.monitoring.enabled:
        logger.info("Monitoring disabled in configuration")
        return
    
    _monitoring_thread = MonitoringThread(interval_seconds=interval_seconds)
    _monitoring_thread.start()
    logger.info(f"Background monitoring started (interval: {interval_seconds}s)")

def stop_monitoring() -> None:
    """Stop background monitoring."""
    global _monitoring_thread
    
    if _monitoring_thread is not None:
        _monitoring_thread.stop()
        _monitoring_thread.join(timeout=5)
        _monitoring_thread = None
        logger.info("Background monitoring stopped")