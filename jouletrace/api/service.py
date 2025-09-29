# jouletrace/api/service.py
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .routes import router as api_router
from .error_handlers import setup_error_handlers, ErrorReportingMiddleware
from .dependencies import startup_dependencies, shutdown_dependencies

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Handles startup and shutdown procedures for JouleTrace service.
    """
    # Startup
    logger.info("Starting JouleTrace energy measurement service")
    
    try:
        # Initialize dependencies (energy meters, pipeline, etc.)
        startup_dependencies()
        logger.info("JouleTrace service startup completed successfully")
        
        yield
        
    finally:
        # Shutdown
        logger.info("Shutting down JouleTrace energy measurement service")
        try:
            shutdown_dependencies()
            logger.info("JouleTrace service shutdown completed successfully")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)

def create_app() -> FastAPI:
    """
    Create and configure the JouleTrace FastAPI application.
    
    Returns:
        Configured FastAPI application ready for production deployment.
    """
    
    # Create FastAPI app with metadata
    app = FastAPI(
        title="JouleTrace Energy Measurement API",
        description="""
        **JouleTrace** is a production-ready energy measurement service for code evaluation.
        
        ## Features
        
        * **Accurate Energy Measurement**: Hardware-level energy consumption measurement using RAPL
        * **Solution Validation**: Comprehensive correctness checking before energy measurement  
        * **CPU Isolation**: Fair multi-core measurement with thermal control
        * **Async Processing**: Non-blocking task queue for long-running measurements
        * **Production Ready**: Comprehensive error handling, monitoring, and logging
        
        ## Use Cases
        
        * **AI Training Pipelines**: Energy-efficient model training (GRPO, etc.)
        * **Algorithm Optimization**: Compare energy efficiency of different implementations
        * **Green Computing**: Measure and optimize software energy consumption
        * **Research**: Academic research on energy-efficient computing
        
        ## API Workflow
        
        1. **Queue Measurement**: `POST /api/v1/measure` - Submit code for energy measurement
        2. **Poll Results**: `GET /api/v1/tasks/{task_id}` - Check measurement progress and results
        3. **Quick Validation**: `POST /api/v1/validate` - Fast correctness checking without energy measurement
        
        ## System Requirements
        
        * Linux x86_64 with Intel/AMD processor supporting RAPL
        * Perf tools installed (`linux-tools` package)
        * Appropriate permissions for energy measurement
        """,
        version="1.0.0",
        contact={
            "name": "JouleTrace",
            "email": "support@jouletrace.com",
        },
        license_info={
            "name": "MIT License",
            "url": "https://opensource.org/licenses/MIT",
        },
        lifespan=lifespan,
        # Enable automatic OpenAPI docs
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json"
    )
    
    # Add middleware
    setup_middleware(app)
    
    # Add error handlers
    setup_error_handlers(app)
    
    # Include API routes
    app.include_router(api_router)
    
    # Add root endpoint
    @app.get("/", tags=["root"])
    async def root():
        """Root endpoint with service information."""
        return {
            "service": "JouleTrace Energy Measurement API",
            "version": "1.0.0",
            "status": "healthy",
            "docs": "/docs",
            "api": "/api/v1"
        }
    
    @app.get("/ping", tags=["health"])
    async def ping():
        """Simple health check endpoint."""
        return {"status": "ok", "service": "jouletrace"}
    
    logger.info("JouleTrace FastAPI application created successfully")
    return app

def setup_middleware(app: FastAPI) -> None:
    """Configure middleware for the application."""
    
    # Error reporting middleware (must be first)
    app.add_middleware(ErrorReportingMiddleware)
    
    # Compression middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    
    # CORS middleware for cross-origin requests
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",  # React development server
            "http://localhost:8080",  # Common dev server
            "https://your-training-dashboard.com",  # Production dashboard
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )
    
    logger.info("Middleware configuration completed")

def get_app_info() -> Dict[str, Any]:
    """Get comprehensive application information."""
    from .dependencies import get_dependency_status, check_system_health
    
    try:
        dependency_status = get_dependency_status()
        system_health = check_system_health()
        
        return {
            "service_info": {
                "name": "JouleTrace Energy Measurement API",
                "version": "1.0.0",
                "description": "Production-ready energy measurement service"
            },
            "dependencies": dependency_status,
            "system_health": system_health,
            "api_endpoints": {
                "measure": "POST /api/v1/measure",
                "poll_results": "GET /api/v1/tasks/{task_id}",
                "quick_validation": "POST /api/v1/validate",
                "health_check": "GET /api/v1/health",
                "capabilities": "GET /api/v1/capabilities"
            }
        }
    except Exception as e:
        logger.error(f"Failed to get app info: {e}", exc_info=True)
        return {
            "service_info": {
                "name": "JouleTrace Energy Measurement API",
                "version": "1.0.0",
                "status": "error"
            },
            "error": str(e)
        }

# Production ASGI app instance
app = create_app()

# Development/testing utilities
def run_development_server():
    """
    Run the development server.
    For production, use a proper ASGI server like uvicorn or gunicorn.
    """
    import uvicorn
    
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting JouleTrace development server")
    
    uvicorn.run(
        "jouletrace.api.service:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )

def run_production_server():
    """
    Production server configuration.
    This shows recommended settings for production deployment.
    """
    import uvicorn
    
    # Production logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    logger.info("Starting JouleTrace production server")
    
    uvicorn.run(
        "jouletrace.api.service:app",
        host="0.0.0.0",
        port=8000,
        workers=1,  # Single worker for energy measurement isolation
        loop="uvloop",  # High-performance event loop
        http="h11",  # HTTP/1.1 protocol
        log_level="info",
        access_log=True,
        server_header=False,  # Security
        date_header=False     # Security
    )

# Deployment configuration
DEPLOYMENT_CONFIG = {
    "development": {
        "host": "127.0.0.1",
        "port": 8000,
        "reload": True,
        "workers": 1,
        "log_level": "debug"
    },
    "production": {
        "host": "0.0.0.0", 
        "port": 8000,
        "reload": False,
        "workers": 1,  # Energy measurement requires single worker
        "log_level": "info",
        "access_log": True
    },
    "docker": {
        "host": "0.0.0.0",
        "port": 8000,
        "workers": 1,
        "log_level": "info", 
        "proxy_headers": True,
        "forwarded_allow_ips": "*"
    }
}

if __name__ == "__main__":
    # For development testing
    run_development_server()