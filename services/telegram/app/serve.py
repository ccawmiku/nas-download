import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8000")),
        access_log=not bool(os.getenv('NAS_CORE_URL')),
    )


if __name__ == "__main__":
    main()
