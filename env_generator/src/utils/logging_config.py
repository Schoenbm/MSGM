import logging
import sys


def setup_logging(verbose: bool = False) -> None:
    """Configure root logger. Call once at program entry point."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
