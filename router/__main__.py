"""Entry point: ``python -m router [core|peripheral]``.

Two deployable services live in this repository (design §3): the decision kernel
(``core``) and the peripheral console/SQLite/standalone data plane
(``peripheral``).  ``peripheral`` stays the default so existing process managers
and smoke scripts that run plain ``python -m router`` keep starting the console
service.

The peripheral process now runs **two listeners in one asyncio loop** (C1/I5):

* the public console listener on ``ROUTER_HOST``/``ROUTER_PORT`` — console,
  ``/admin`` and the standalone ``/v1`` data plane;
* the internal decision-event listener on ``ROUTER_INTERNAL_HOST``/
  ``ROUTER_INTERNAL_PORT`` — only ``POST /internal/v1/decision-logs`` and
  ``/healthz``, so the kernel feed is never reachable from the published port.

Both apps share one :class:`~router.decision_log_store.DecisionLogStore`, and a
single SIGINT/SIGTERM handler stops both ``uvicorn.Server`` instances.
"""

import argparse
import asyncio
import contextlib
import signal

import uvicorn


def _hand_signals_to_one_handler(server: uvicorn.Server) -> None:
    """Stop a uvicorn server from installing its own SIGINT/SIGTERM handlers.

    With two servers in one loop each would otherwise try to own the signals and
    stop only itself.  ``install_signal_handlers`` exists on some uvicorn
    versions; newer ones capture signals in ``Server.capture_signals`` instead,
    so both are neutralised and the single asyncio handler below owns them.
    """
    if hasattr(server, "install_signal_handlers"):
        server.install_signal_handlers = lambda: None
    server.capture_signals = contextlib.nullcontext


def _run_peripheral() -> None:
    """Start the public console app and the internal receiver app together."""
    from router.api.internal_app import create_service_apps
    from router.config import Settings

    settings = Settings.from_env()
    public_app, internal_app = create_service_apps(settings)
    public = uvicorn.Server(uvicorn.Config(
        public_app, host=settings.host, port=settings.port))
    internal = uvicorn.Server(uvicorn.Config(
        internal_app,
        host=settings.internal_host,
        port=settings.internal_port,
    ))
    # One process → one signal owner.  Letting each server install its own
    # handlers would make the first SIGTERM stop only that server.
    _hand_signals_to_one_handler(public)
    _hand_signals_to_one_handler(internal)

    async def serve() -> None:
        loop = asyncio.get_running_loop()

        def stop(*_args) -> None:
            public.should_exit = True
            internal.should_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop)
        await asyncio.gather(public.serve(), internal.serve())

    asyncio.run(serve())


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
    _run_peripheral()


if __name__ == "__main__":
    main()
