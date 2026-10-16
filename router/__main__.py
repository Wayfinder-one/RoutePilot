"""Entry point: python -m router"""

import uvicorn

from router.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(_AppFactory(settings), host=settings.host, port=settings.port)


class _AppFactory:
    """Defer create_app() so Settings are bound before uvicorn imports the app."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def __call__(self):
        from router.api.app import create_app
        return create_app(self._settings)


if __name__ == "__main__":
    main()
