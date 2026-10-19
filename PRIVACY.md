# Privacy

python-leak-hunter collects no data and sends nothing anywhere.

- **The agent skill** is a set of instructions for your coding agent. It contains no code
  that runs on its own.
- **The Python package** runs locally, inside your own environment, on code you choose to
  measure. It makes no network requests, has no telemetry and writes no files except the
  output you ask for (for example a memory sample log that you configure yourself).
- When the skill tells your agent to install the package, it is downloaded from PyPI like any
  other Python dependency; PyPI's own privacy policy applies to that download.

Questions: open an issue at https://github.com/tariqamarneh/python-leak-hunter/issues.
