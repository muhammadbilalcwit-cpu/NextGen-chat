"""Run the FastAPI Chat Microservice."""
import uvicorn
from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:application",
        host="0.0.0.0",
        port=settings.FASTAPI_PORT,
        reload=True,
        log_level="info",
    )
