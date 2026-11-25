"""Entry point: ``python -m router [core|peripheral]``.

Two deployable services live in this repository (design §3): the decision kernel
(``core``) and the peripheral console/SQLite/standalone data plane
(``peripheral``).  ``peripheral`` stays the default so existing process
managers and smoke scripts that run plain ``python -m router`` keep starting the
console service.
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m router",
        description="Run the RoutePilot decision kernel or the peripheral service.",
    )
    parser.add_argument(
        "service", choices=("core", "peripheral"), default="peripheral", nargs="?",
        help="which process to start (default: peripheral)",
    )
    args = parser.parse_args()
    if args.service == "core":
        from router.api.core_app import create_core_app
        from router.core_config import from_environment

        settings = from_environment()
        uvicorn.run(
            create_core_app(settings), host=settings.host, port=settings.port)
        return
    from router.api.app import create_app
    from router.config import Settings

    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
