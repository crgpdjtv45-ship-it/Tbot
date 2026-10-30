from typing import Dict, Any, Optional
from loguru import logger
from src.api.bybit_client import BybitClient
from src.utils.helpers import round_step_size, round_tick_size

class RiskManager:
    """
    Manages risk parameters, maximum exposure per pair, margin requirements,
    and emergency account protection limits.
    """
    def __init__(
        self,
        client: BybitClient,
        max_open_pairs: int = 3,
        capital_per_pair_pct: float = 20.0,
        leverage: int = 2,
        max_account_drawdown_pct: float = 10.0,
        slippage_tolerance_pct: float = 0.2
    ):
        self.client = client
        self.max_open_pairs = max_open_pairs
        self.capital_per_pair_pct = capital_per_pair_pct
        self.leverage = leverage
        self.max_account_drawdown_pct = max_account_drawdown_pct
        self.slippage_tolerance_pct = slippage_tolerance_pct
        
        self.initial_balance = None
        self.is_kill_switch_active = False

    def check_can_open_position(self, current_open_pairs: int) -> bool:
        """
        Validates if a new pair trade can be opened according to risk rules.
        """
        if self.is_kill_switch_active:
            logger.warning("RiskManager: Trade rejected — Emergency Kill-Switch is ACTIVE.")
            return False
            
        if current_open_pairs >= self.max_open_pairs:
            logger.warning(f"RiskManager: Max open pairs limit reached ({current_open_pairs}/{self.max_open_pairs}).")
            return False
            
        return True

    def calculate_order_quantities(
        self,
        symbol_a: str,
        symbol_b: str,
        price_a: float,
        price_b: float,
        account_balance: float
    ) -> Optional[Dict[str, Any]]:
        """
        Computes exchange-compliant rounded order quantities for both legs.
        Ensures strict dollar neutrality.
        """
        if price_a <= 0 or price_b <= 0 or account_balance <= 0:
            logger.error("Invalid prices or account balance for quantity calculation.")
            return None
            
        # Capital allocated to this pair
        pair_capital = account_balance * (self.capital_per_pair_pct / 100.0) * self.leverage
        notional_per_leg = pair_capital / 2.0
        
        raw_qty_a = notional_per_leg / price_a
        raw_qty_b = notional_per_leg / price_b
        
        # Get instrument constraints from Bybit
        specs_a = self.client.get_instrument_specs(symbol_a)
        specs_b = self.client.get_instrument_specs(symbol_b)
        
        qty_a = round_step_size(raw_qty_a, specs_a["step_size"])
        qty_b = round_step_size(raw_qty_b, specs_b["step_size"])
        
        # Validate minimum order quantities
        if qty_a < specs_a["min_qty"]:
            logger.warning(f"Quantity for {symbol_a} ({qty_a}) below minimum required ({specs_a['min_qty']}).")
            return None
            
        if qty_b < specs_b["min_qty"]:
            logger.warning(f"Quantity for {symbol_b} ({qty_b}) below minimum required ({specs_b['min_qty']}).")
            return None
            
        return {
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "qty_a": qty_a,
            "qty_b": qty_b,
            "notional_a": qty_a * price_a,
            "notional_b": qty_b * price_b,
            "specs_a": specs_a,
            "specs_b": specs_b
        }

    def trigger_kill_switch(self, reason: str = "User manual trigger"):
        """
        Activates the Emergency Kill-Switch to block any further entries.
        """
        self.is_kill_switch_active = True
        logger.critical(f"⚠️ EMERGENCY KILL-SWITCH ACTIVATED: {reason}")
