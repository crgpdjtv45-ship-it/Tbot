import time
import uuid
from typing import Dict, Any, List, Optional
import pandas as pd
from pybit.unified_trading import HTTP
from loguru import logger

class BybitClient:
    """
    Unified client for Bybit V5 API (Linear USDT Perpetual Futures).
    Supports both public market data retrieval (no keys required)
    and authenticated trading (Testnet & Mainnet).
    """
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
        category: str = "linear",
        recv_window: int = 5000
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.category = category
        self.recv_window = recv_window
        
        # Dedicated session for market data (always Mainnet for 100% liquidity & no testnet IP blocks)
        self.market_session = HTTP(testnet=False)
        
        # Trading session (Testnet or Mainnet with credentials)
        self.session = HTTP(
            testnet=self.testnet,
            api_key=self.api_key if self.api_key else None,
            api_secret=self.api_secret if self.api_secret else None,
            recv_window=self.recv_window
        )
        mode_str = "TESTNET" if self.testnet else "MAINNET"
        logger.info(f"Initialized BybitClient in {mode_str} mode (category={self.category})")

    def get_kline(
        self,
        symbol: str,
        interval: str = "60",
        limit: int = 200,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> pd.DataFrame:
        """
        Fetches historical candlestick (kline) data from Bybit V5 API.
        
        Args:
            symbol: e.g. 'SOLUSDT', 'AVAXUSDT'
            interval: 1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, M, W
            limit: Max 1000 per request
            start_time: start timestamp in ms
            end_time: end timestamp in ms
            
        Returns:
            pd.DataFrame sorted chronologically with columns:
            ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
        """
        params: Dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "interval": str(interval),
            "limit": limit
        }
        if start_time:
            params["start"] = start_time
        if end_time:
            params["end"] = end_time
            
        try:
            response = self.market_session.get_kline(**params)
            if response.get("retCode") != 0:
                logger.error(f"Bybit get_kline error for {symbol}: {response.get('retMsg')}")
                return pd.DataFrame()
                
            raw_list = response.get("result", {}).get("list", [])
            if not raw_list:
                return pd.DataFrame()
                
            # Bybit returns klines in reverse chronological order:
            # [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
            df = pd.DataFrame(raw_list, columns=[
                "timestamp", "open", "high", "low", "close", "volume", "turnover"
            ])
            
            # Convert types safely (int64 for Windows 64-bit millisecond timestamps)
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms")
            for col in ["open", "high", "low", "close", "volume", "turnover"]:
                df[col] = df[col].astype(float)
                
            # Sort oldest to newest
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"Failed to fetch kline for {symbol}: {e}")
            return pd.DataFrame()

    def get_historical_klines(
        self,
        symbol: str,
        interval: str = "60",
        total_bars: int = 1440
    ) -> pd.DataFrame:
        """
        Fetches multi-page historical klines by paginating backwards in time.
        For example: 1440 hourly bars = 60 days of data.
        """
        all_dfs = []
        bars_remaining = total_bars
        current_end_time = None
        
        while bars_remaining > 0:
            batch_limit = min(bars_remaining, 1000)
            df_batch = self.get_kline(
                symbol=symbol,
                interval=interval,
                limit=batch_limit,
                end_time=current_end_time
            )
            
            if df_batch.empty:
                break
                
            all_dfs.append(df_batch)
            bars_remaining -= len(df_batch)
            
            # The oldest bar in this batch becomes the end_time for the next batch
            oldest_ts = int(df_batch.iloc[0]["timestamp"].timestamp() * 1000) - 1
            if current_end_time is not None and oldest_ts >= current_end_time:
                break
            current_end_time = oldest_ts
            
            # Respect Bybit rate limits
            time.sleep(0.05)
            
            if len(df_batch) < batch_limit:
                # No more historical data available
                break
                
        if not all_dfs:
            return pd.DataFrame()
            
        full_df = pd.concat(all_dfs, ignore_index=True)
        full_df = full_df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        return full_df

    def get_ticker_info(self, symbol: str) -> Dict[str, Any]:
        """
        Retrieves current ticker data (lastPrice, bid1Price, ask1Price, fundingRate).
        """
        try:
            res = self.market_session.get_tickers(category=self.category, symbol=symbol)
            if res.get("retCode") == 0:
                list_data = res.get("result", {}).get("list", [])
                if list_data:
                    t = list_data[0]
                    return {
                        "symbol": symbol,
                        "last_price": float(t.get("lastPrice", 0)),
                        "bid": float(t.get("bid1Price", 0)),
                        "ask": float(t.get("ask1Price", 0)),
                        "funding_rate": float(t.get("fundingRate", 0)),
                        "next_funding_time": int(t.get("nextFundingTime", 0)),
                        "volume_24h": float(t.get("turnover24h", 0))
                    }
        except Exception as e:
            logger.error(f"Error fetching ticker for {symbol}: {e}")
        return {}

    def get_instrument_specs(self, symbol: str) -> Dict[str, Any]:
        """
        Fetches instrument lot size rules (minOrderQty, stepSize, tickSize).
        """
        try:
            res = self.market_session.get_instruments_info(category=self.category, symbol=symbol)
            if res.get("retCode") == 0:
                instruments = res.get("result", {}).get("list", [])
                if instruments:
                    spec = instruments[0]
                    lot_filter = spec.get("lotSizeFilter", {})
                    price_filter = spec.get("priceFilter", {})
                    return {
                        "symbol": symbol,
                        "step_size": float(lot_filter.get("qtyStep", 0.01)),
                        "min_qty": float(lot_filter.get("minOrderQty", 0.01)),
                        "max_qty": float(lot_filter.get("maxOrderQty", 1000000)),
                        "tick_size": float(price_filter.get("tickSize", 0.01))
                    }
        except Exception as e:
            logger.error(f"Error fetching instruments info for {symbol}: {e}")
        return {"symbol": symbol, "step_size": 0.01, "min_qty": 0.01, "tick_size": 0.01}

    def get_wallet_balance(self) -> float:
        """
        Gets available equity / balance in USDT for Unified Account.
        """
        if not self.api_key:
            return 10000.0  # Default simulation balance
        try:
            res = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            if res.get("retCode") == 0:
                coins = res.get("result", {}).get("list", [{}])[0].get("coin", [])
                for c in coins:
                    if c.get("coin") == "USDT":
                        return float(c.get("equity", 0.0))
        except Exception as e:
            logger.error(f"Error fetching wallet balance: {e}")
        return 0.0

    def place_market_order(self, symbol: str, side: str, qty: float, *, reduce_only: bool = False) -> Optional[str]:
        """Legacy simulation helper. Real orders are disabled until fill reconciliation exists."""
        if self.api_key:
            raise RuntimeError("Exchange order submission is disabled. Use the unified PAPER runtime.")
        if side not in {"Buy", "Sell"} or not __import__('math').isfinite(qty) or qty <= 0:
            raise ValueError("Invalid simulated order")
        logger.info("[SIMULATION] {} {} {} reduce_only={}", side, qty, symbol, reduce_only)
        return "SIM_" + uuid.uuid4().hex
