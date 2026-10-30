import time
from typing import Dict, Any, Optional, List
from loguru import logger
from src.api.bybit_client import BybitClient

class OrderManager:
    """
    Executes and monitors quasi-atomic two-legged arbitrage orders on Bybit V5.
    Protects against 'Leg Risk' (unhedged single-sided exposure).
    """
    def __init__(self, client: BybitClient):
        self.client = client
        self.active_positions: Dict[str, Dict[str, Any]] = {}

    def open_pair_position(
        self,
        symbol_a: str,
        symbol_b: str,
        side: int,           # +1: Long A / Short B, -1: Short A / Long B
        qty_a: float,
        qty_b: float,
        current_z: float,
        beta: float
    ) -> Optional[Dict[str, Any]]:
        """
        Executes simultaneous entry of both legs with emergency leg-risk unwind protection.
        """
        side_a = "Buy" if side == 1 else "Sell"
        side_b = "Sell" if side == 1 else "Buy"
        pair_key = f"{symbol_a}/{symbol_b}"
        
        logger.info(f"Initiating pair entry for {pair_key}: Leg A ({side_a} {qty_a} {symbol_a}) | Leg B ({side_b} {qty_b} {symbol_b})")
        
        # 1. Place Leg A
        order_id_a = self.client.place_market_order(symbol_a, side_a, qty_a)
        if not order_id_a:
            logger.error(f"Leg A ({symbol_a}) order failed! Aborting entry. Leg B will NOT be placed.")
            return None
            
        # 2. Place Leg B immediately
        order_id_b = self.client.place_market_order(symbol_b, side_b, qty_b)
        if not order_id_b:
            logger.critical(f"LEG B ({symbol_b}) FAILED AFTER LEG A FILLED! Emergency unwind of Leg A triggered!")
            # Emergency Leg-Risk Unwind: Close Leg A immediately
            unwind_side = "Sell" if side_a == "Buy" else "Buy"
            self.client.place_market_order(symbol_a, unwind_side, qty_a)
            return None
            
        # Position successfully opened
        pos_record = {
            "pair": pair_key,
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "side": side,
            "side_a": side_a,
            "side_b": side_b,
            "qty_a": qty_a,
            "qty_b": qty_b,
            "entry_time": time.time(),
            "entry_z": current_z,
            "beta": beta,
            "order_id_a": order_id_a,
            "order_id_b": order_id_b
        }
        self.active_positions[pair_key] = pos_record
        logger.info(f"✅ Successfully opened pair position for {pair_key}!")
        return pos_record

    def close_pair_position(self, pair_key: str, reason: str = "Mean Reversion") -> Optional[Dict[str, Any]]:
        """
        Closes both legs of an active pair position simultaneously.
        """
        if pair_key not in self.active_positions:
            logger.warning(f"No active position found for {pair_key}")
            return None
            
        pos = self.active_positions[pair_key]
        close_side_a = "Sell" if pos["side_a"] == "Buy" else "Buy"
        close_side_b = "Sell" if pos["side_b"] == "Buy" else "Buy"
        
        logger.info(f"Closing pair {pair_key} ({reason}): {close_side_a} {pos['qty_a']} {pos['symbol_a']} | {close_side_b} {pos['qty_b']} {pos['symbol_b']}")
        
        # Close both legs
        self.client.place_market_order(pos["symbol_a"], close_side_a, pos["qty_a"])
        self.client.place_market_order(pos["symbol_b"], close_side_b, pos["qty_b"])
        
        closed_record = self.active_positions.pop(pair_key)
        closed_record["exit_time"] = time.time()
        closed_record["exit_reason"] = reason
        logger.info(f"✅ Successfully closed pair {pair_key}")
        return closed_record

    def close_all_positions(self, reason: str = "Kill Switch") -> List[Dict[str, Any]]:
        """
        Emergency close of all open pairs.
        """
        closed = []
        keys = list(self.active_positions.keys())
        for k in keys:
            res = self.close_pair_position(k, reason=reason)
            if res:
                closed.append(res)
        return closed
