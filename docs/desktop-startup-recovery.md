# Desktop Startup Recovery

Packaged Storydex starts the bundled Python runtime and waits for
`/api/v1/sys/health`. A slow first start, endpoint protection scan, or a
temporary port race must not make the application disappear without a way to
recover.

The desktop process now:

- retries backend startup once by default;
- waits for a failed child process to exit before reusing its port;
- shows `Retry`, `Open log`, and `Exit` after the retry budget is exhausted;
- keeps the complete backend log under the Electron user-data directory
  (`logs/backend.log`, with the previous run in `logs/backend.prev.log`).

The retry policy is configurable for diagnostics without changing source code:

| Variable | Default | Bounds | Purpose |
| --- | ---: | ---: | --- |
| `STORYDEX_BACKEND_STARTUP_ATTEMPTS` | `2` | `1..5` | Full process-start attempts before showing the dialog |
| `STORYDEX_BACKEND_STARTUP_RETRY_DELAY_MS` | `1500` | `250..30000` | Delay between automatic attempts |
| `STORYDEX_BACKEND_HEALTH_ATTEMPTS` | `120` | `1..600` | Health probes per process attempt |
| `STORYDEX_BACKEND_HEALTH_INTERVAL_MS` | `500` | `100..5000` | Delay between health probes |

These settings are intended for troubleshooting slow machines or endpoint
protection software. They do not enable system Python fallback in packaged
builds. That fallback remains opt-in through
`STORYDEX_ALLOW_SYSTEM_PYTHON_FALLBACK=1` and should not be used for normal
release validation.

To investigate a failure, choose `Open log` in the startup dialog or inspect
`backend.log` after closing the application. The log records the selected
Python candidate, preflight result, backend output, port, health timeout, and
each automatic retry.
