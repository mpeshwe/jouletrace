#!/bin/bash
# JouleTrace Socket 0 Architecture - Startup Script
# Starts Redis, Celery workers, and FastAPI API server

set -e

JOULETRACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JOULETRACE_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# PID file locations
REDIS_PID="/tmp/jouletrace-redis.pid"
WORKER_PID="/tmp/jouletrace-worker.pid"
API_PID="/tmp/jouletrace-api.pid"
LOG_DIR="/var/log/jouletrace"
API_PORT="${API_PORT:=8000}"

echo "============================================================"
echo "JouleTrace Socket 0 Architecture - Startup"
echo "============================================================"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Error: Must run as root (need sudo for PCM/RAPL access)${NC}"
    exit 1
fi

# Create log directory
mkdir -p "$LOG_DIR"
chown -R $SUDO_USER:$SUDO_USER "$LOG_DIR" 2>/dev/null || true

# Function to check if process is running
is_running() {
    local pid_file=$1
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

# Function to stop service
stop_service() {
    local name=$1
    local pid_file=$2
    
    if is_running "$pid_file"; then
        local pid=$(cat "$pid_file")
        echo -e "${YELLOW}Stopping $name (PID: $pid)...${NC}"
        kill "$pid" 2>/dev/null || true
        sleep 2
        
        # Force kill if still running
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        
        rm -f "$pid_file"
        echo -e "${GREEN}✓ $name stopped${NC}"
    fi
}

# Cleanup function
cleanup() {
    echo ""
    echo "============================================================"
    echo "Shutting down JouleTrace services..."
    echo "============================================================"
    
    stop_service "API Server" "$API_PID"
    stop_service "Celery Worker" "$WORKER_PID"
    stop_service "Redis" "$REDIS_PID"
    
    echo -e "${GREEN}✓ All services stopped${NC}"
    exit 0
}

# Set trap for cleanup
trap cleanup SIGINT SIGTERM

# Pre-flight checks
echo "Pre-flight checks:"

# Check calibration
if [ ! -f "config/socket0_calibration.json" ]; then
    echo -e "${RED}✗ Socket 0 not calibrated${NC}"
    echo "  Run: sudo python3 scripts/calibrate_socket0.py"
    exit 1
fi
echo -e "${GREEN}✓ Calibration found${NC}"

# Check isolation
if [ ! -f "/sys/devices/system/cpu/isolated" ]; then
    echo -e "${RED}✗ CPU isolation not configured${NC}"
    echo "  Run Part 1 setup first"
    exit 1
fi

ISOLATED_CPUS=$(cat /sys/devices/system/cpu/isolated)
if [ -z "$ISOLATED_CPUS" ]; then
    echo -e "${RED}✗ No isolated CPUs${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Socket 0 isolated: $ISOLATED_CPUS${NC}"

# Check Python environment
if ! python3 -c "import jouletrace" 2>/dev/null; then
    echo -e "${RED}✗ JouleTrace not installed${NC}"
    echo "  Run: pip install -e ."
    exit 1
fi
echo -e "${GREEN}✓ JouleTrace installed${NC}"

# Check API port availability early
if lsof -Pi :"$API_PORT" -sTCP:LISTEN > /dev/null 2>&1; then
    echo -e "${RED}✗ API port $API_PORT already in use${NC}"
    echo "  Free the port or run with API_PORT=<port> ./scripts/start_jouletrace.sh"
    exit 1
fi

echo ""

# Start Redis
echo "Starting Redis..."
if is_running "$REDIS_PID"; then
    echo -e "${YELLOW}Redis already running${NC}"
else
    redis-server --daemonize yes --pidfile "$REDIS_PID" \
        --logfile "$LOG_DIR/redis.log" \
        --dir /var/lib/redis 2>&1 | tee -a "$LOG_DIR/redis.log"
    
    sleep 1
    
    if redis-cli ping > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Redis started (PID: $(cat $REDIS_PID))${NC}"
    else
        echo -e "${RED}✗ Redis failed to start${NC}"
        exit 1
    fi
fi

# Start Celery Worker
echo "Starting Celery worker (Socket 0 measurements)..."
if is_running "$WORKER_PID"; then
    echo -e "${YELLOW}Worker already running${NC}"
else
    # Detect python/celery from current environment
    PYTHON_BIN=$(which python3)
    CELERY_BIN=$(which celery 2>/dev/null || echo "python3 -m celery")
    
    # Start worker (stay as root for RAPL/MSR access)
    nohup $CELERY_BIN -A jouletrace.api.tasks worker \
        --loglevel=info \
        --concurrency=1 \
        --queues=socket0_measurements \
        --hostname=socket0-worker@localhost \
        --logfile=$LOG_DIR/worker.log \
        --pidfile=$WORKER_PID \
        --detach > $LOG_DIR/worker-startup.log 2>&1 &
    
    sleep 3
    
    if is_running "$WORKER_PID"; then
        echo -e "${GREEN}✓ Celery worker started (PID: $(cat $WORKER_PID))${NC}"
    else
        echo -e "${RED}✗ Worker failed to start${NC}"
        echo "  Check logs: tail -f $LOG_DIR/worker.log"
        echo "  Startup log: tail -f $LOG_DIR/worker-startup.log"
        cleanup
        exit 1
    fi
fi

# Start API Server
echo "Starting FastAPI server..."
if is_running "$API_PID"; then
    echo -e "${YELLOW}API already running${NC}"
else
    # Start API server
    nohup python3 -m uvicorn jouletrace.api.service:app \
        --host 0.0.0.0 \
        --port "$API_PORT" \
        --workers 4 \
        > $LOG_DIR/api.log 2>&1 &
    
    echo $! > "$API_PID"
    sleep 3
    
    if is_running "$API_PID"; then
        API_PID_VAL=$(cat "$API_PID")
        echo -e "${GREEN}✓ API server started on port $API_PORT (PID: $API_PID_VAL)${NC}"
    else
        echo -e "${RED}✗ API failed to start${NC}"
        echo "  Check logs: tail -f $LOG_DIR/api.log"
        cleanup
        exit 1
    fi
fi

echo ""
echo "============================================================"
echo "JouleTrace Services Running"
echo "============================================================"
echo -e "Redis:         ${GREEN}Running${NC} (PID: $(cat $REDIS_PID 2>/dev/null || echo 'N/A'))"
echo -e "Celery Worker: ${GREEN}Running${NC} (PID: $(cat $WORKER_PID 2>/dev/null || echo 'N/A'))"
echo -e "API Server:    ${GREEN}Running${NC} (PID: $(cat $API_PID 2>/dev/null || echo 'N/A'))"
echo ""
echo "Access Points:"
echo "  API Docs:  http://$(hostname -I | awk '{print $1}'):8000/docs"
echo "  Health:    http://$(hostname -I | awk '{print $1}'):8000/api/v1/health"
echo "  Socket 0:  http://$(hostname -I | awk '{print $1}'):8000/api/v1/socket0/status"
echo ""
echo "Logs:"
echo "  Redis:     tail -f $LOG_DIR/redis.log"
echo "  Worker:    tail -f $LOG_DIR/worker.log"
echo "  API:       tail -f $LOG_DIR/api.log"
echo ""
echo "To stop all services: Press Ctrl+C or run:"
echo "  sudo $0 stop"
echo "============================================================"
echo ""

# Check if called with 'stop' argument
if [ "$1" == "stop" ]; then
    cleanup
    exit 0
fi

# Keep script running and monitor services
echo "Monitoring services (Ctrl+C to stop)..."
echo ""

while true; do
    sleep 10
    
    # Check if services are still running
    if ! is_running "$API_PID"; then
        echo -e "${RED}✗ API server died unexpectedly${NC}"
        cleanup
        exit 1
    fi
    
    if ! is_running "$WORKER_PID"; then
        echo -e "${RED}✗ Celery worker died unexpectedly${NC}"
        cleanup
        exit 1
    fi
    
    if ! is_running "$REDIS_PID"; then
        echo -e "${RED}✗ Redis died unexpectedly${NC}"
        cleanup
        exit 1
    fi
done
