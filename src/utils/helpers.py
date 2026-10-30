import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Union, Dict, Any
import yaml
from pathlib import Path

def round_step_size(quantity: Union[float, Decimal], step_size: Union[float, Decimal, str]) -> float:
    """
    Rounds a quantity down to the nearest exchange stepSize precision.
    Prevents Bybit rejecting order with invalid qty step.
    """
    step = Decimal(str(step_size))
    qty = Decimal(str(quantity))
    
    # Floor to nearest step
    rounded = (qty // step) * step
    return float(rounded)

def round_tick_size(price: Union[float, Decimal], tick_size: Union[float, Decimal, str]) -> float:
    """
    Rounds a price to the nearest exchange tickSize precision.
    """
    tick = Decimal(str(tick_size))
    p = Decimal(str(price))
    
    # Nearest tick
    steps = (p / tick).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return float(steps * tick)

def load_yaml_config(config_path: str = "configs/config.yaml") -> Dict[str, Any]:
    """
    Loads config.yaml if exists, otherwise falls back to config.example.yaml
    """
    path = Path(config_path)
    if not path.exists():
        example_path = Path("configs/config.example.yaml")
        if example_path.exists():
            with open(example_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        raise FileNotFoundError(f"Configuration file not found at {config_path}")
    
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
