"""Entry point: python -m router"""

import uvicorn

from router.config import Settings


def main() -> None:
    settings = Settings.from_env()
    from router.api.app import create_app
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
