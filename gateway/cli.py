"""Private operator entry point. Diagnostics contain flags, never credential values."""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import stat
import time
import json

from .config import Config
from .provider import rates_current
from .tasks import default_registry

ENV_NAMES = {"ATELIER_GATEWAY_ORIGIN", "ATELIER_SITE_DOMAIN", "ATELIER_LEDGER_PATH",
             "ATELIER_SHARED_AI_ENABLED", "ATELIER_DAILY_LIMIT_USD", "ATELIER_OPENAI_API_KEY"}


def private_environment(path):
    selected = Path(path).expanduser()
    info = selected.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ValueError("Use an owned regular mode-0600 environment file.")
    result = {}
    for line in selected.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in ENV_NAMES or name in result:
            raise ValueError("Check the Atelier environment variable names.")
        if len(value) > 1024:
            raise ValueError("Check the Atelier environment value bounds.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[name] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "serve"])
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    try:
        values = {name: os.environ[name] for name in ENV_NAMES if name in os.environ}
        if args.env_file:
            values.update(private_environment(args.env_file))
        config = Config.from_env(values)
        if args.command == "doctor":
            registry = default_registry(config)
            print(json.dumps({"shared_enabled": config.shared_enabled, "personal_key_configured": bool(config.shared_key),
                              "daily_limit_within_approved_ceiling": True, "rate_review_current": rates_current(time.time()),
                              "registered_projects": len(registry.projects), "registered_tasks": len(registry.tasks),
                              "provider_calls": 0}))
            return
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(config, default_registry(config)), host="127.0.0.1", port=8817, access_log=False,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1", limit_concurrency=64, timeout_keep_alive=5)
    except (ValueError, OSError):
        parser.exit(2, "Check the private Atelier configuration and file permissions.\n")


if __name__ == "__main__":
    main()
