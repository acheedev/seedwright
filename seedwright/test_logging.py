from logging_setup import configure_logging, enable_debug_file, disable_debug_file, debug_file
import logging

configure_logging({
    "level": "INFO",
    "stdout": False,
    "file": "../logs/app.log",
    "json": False,
    "quiet_loggers": {"urllib3": "WARNING", "asyncio": "WARNING"},
})

log = logging.getLogger("demo")
log.info("started", extra={"request_id": "abc123"})
log.critical("critical")
enable_debug_file("../logs/debug.log")     # flip on (e.g. from a signal handler or admin endpoint)
log.critical("critical2")
log.debug("debug output2")
log.exception("exception")
disable_debug_file()                    # flip off, logger level restored


# with debug_file("logs/trace.log"):      # or scope it to a block
#     do_the_suspicious_thing()
