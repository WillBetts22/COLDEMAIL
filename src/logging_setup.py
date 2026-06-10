import logging
from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(log_file: str = "run.log") -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(console=console, rich_tracebacks=True, show_path=False),
            logging.FileHandler(log_file, mode="a", encoding="utf-8"),
        ],
    )
    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return logging.getLogger("coldemail")
