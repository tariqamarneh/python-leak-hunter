"""Find, explain and confirm the fix for the per-request client leak in app.py.

python examples/contextvars_leak/demo.py
"""

from __future__ import annotations

import app

from leak_hunter import measure_growth, why_alive


def main() -> None:
    print("1. What accumulates per request?\n")
    print(measure_growth(app.handle_request_leaky, include=["app."]).format())

    print("\n2. What keeps it alive?\n")
    print(why_alive("app.RunTrace").format())

    print("\n3. After the fix (one client per process, built in an empty context):\n")
    print(measure_growth(app.handle_request_fixed, include=["app."]).format())


if __name__ == "__main__":
    main()
