import sys
from pathlib import Path
from loguru import logger

def setup_logger(log_level: str = "INFO", log_dir: str = "logs"):
    """
    Configures standard loguru logger with formatting and file rotation.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    logger.remove()
    
    # Console handler with clean, professional formatting
    logger.add(
        sys.stdout,
        level=log_level,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    
    # File handler for full history
    logger.add(
        log_path / "statarb_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
    )
    
    return logger

# Default logger instance
log = setup_logger()
