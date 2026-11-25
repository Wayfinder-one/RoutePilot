from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LogSinkSettings:
    type: str = "disabled"
    url: str = ""
    timeout_ms: int = 1000


@dataclass(frozen=True)
class CoreSettings:
    host: str = "0.0.0.0"
    port: int = 8400
    graph_dir: str = "./routing-graph"
    jev_api_key_env: str = "ROUTER_JEV_API_KEY"
    logging: LogSinkSettings = LogSinkSettings()

    @staticmethod
    def from_file(path: str) -> "CoreSettings":
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("config must be an object")
        server = raw.get("server") or {}
        graphs = raw.get("graphs") or {}
        jev = raw.get("jev") or {}
        logging = raw.get("logging") or {"type": "disabled"}
        sink = LogSinkSettings(
            type=logging.get("type", "disabled"),
            url=logging.get("url", ""),
            timeout_ms=logging.get("timeout_ms", 1000),
        )
        if sink.type not in {"routepilot", "disabled"}:
            raise ValueError("logging.type must be routepilot or disabled")
        if sink.type == "routepilot":
            parsed = urlsplit(sink.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("logging.url must be an http/https URL")
        if not 1 <= sink.timeout_ms <= 10_000:
            raise ValueError("logging.timeout_ms must be in [1,10000]")
        return CoreSettings(
            host=server.get("host", "0.0.0.0"),
            port=int(server.get("port", 8400)),
            graph_dir=graphs.get("directory", "./routing-graph"),
            jev_api_key_env=jev.get("api_key_env", "ROUTER_JEV_API_KEY"),
            logging=sink,
        )


def from_environment() -> CoreSettings:
    return CoreSettings.from_file(os.environ.get("ROUTER_CONFIG", "config.json"))
