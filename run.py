import uvicorn

from app.config import Settings


settings = Settings.from_env()

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
