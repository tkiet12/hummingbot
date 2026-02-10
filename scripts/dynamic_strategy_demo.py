import os
from decimal import Decimal
from typing import Dict, List, Optional

import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction


class DynamicStrategyDemoConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    markets: Dict[str, List[str]] = {}
    candles_config: List[CandlesConfig] = []
    controllers_config: List[str] = []
    
    # Market Configuration
    exchange: str = Field(default="binance_perpetual", description="Exchange to trade on")
    trading_pair: str = Field(default="ETH-USDT", description="Trading pair to trade")
    
    # Candles Configuration
    candles_exchange: str = Field(default="binance_perpetual", description="Exchange to get candles from")
    candles_pair: str = Field(default="ETH-USDT", description="Trading pair to get candles from")
    candles_interval: str = Field(default="1m", description="Candle interval")
    candles_length: int = Field(default=100, gt=0, description="Number of candles to keep")
    
    # Strategy Parameters
    rsi_length: int = Field(default=14, gt=0, description="RSI period length")
    rsi_overbought: float = Field(default=70, gt=0, description="RSI overbought threshold")
    rsi_oversold: float = Field(default=30, gt=0, description="RSI oversold threshold")
    
    # Dynamic Risk Parameters
    atr_length: int = Field(default=14, gt=0, description="ATR period length for volatility calculation")
    atr_multiplier_sl: float = Field(default=2.0, gt=0, description="Multiplier for ATR to calculate Stop Loss distance")
    risk_reward_ratio: float = Field(default=1.5, gt=0, description="Ratio of Take Profit distance to Stop Loss distance")
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=1.0, description="Percentage of account balance to risk per trade (0.01 = 1%)")
    leverage: int = Field(default=10, gt=0, description="Leverage to use")
    
    # Execution Limits
    time_limit: int = Field(default=60 * 60, gt=0, description="Time limit for open positions in seconds")

    @field_validator('risk_per_trade_pct')
    @classmethod
    def validate_risk_pct(cls, v):
        if v <= 0 or v > 0.1:  # Safety check: don't allow risking more than 10% per trade
            raise ValueError("Risk per trade must be between 0 and 0.1 (10%)")
        return v


class DynamicStrategyDemo(StrategyV2Base):
    """
    Demonstrates dynamic parameter calculation:
    1. Stop Loss: Based on ATR (Volatility).
    2. Take Profit: Based on Stop Loss distance * Risk:Reward Ratio.
    3. Order Amount: Based on Risk % of Account Balance.
    """
    
    account_config_set = False

    @classmethod
    def init_markets(cls, config: DynamicStrategyDemoConfig):
        cls.markets = {config.exchange: {config.trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase], config: DynamicStrategyDemoConfig):
        # Ensure we have enough candles for indicators
        max_records = max(config.candles_length, config.rsi_length, config.atr_length) + 20
        if len(config.candles_config) == 0:
            config.candles_config.append(CandlesConfig(
                connector=config.candles_exchange,
                trading_pair=config.candles_pair,
                interval=config.candles_interval,
                max_records=max_records
            ))
        super().__init__(connectors, config)
        self.config = config
        
        # State variables for reporting
        self.current_rsi = None
        self.current_atr = None
        self.dynamic_sl_pct = None
        self.dynamic_tp_pct = None
        self.calculated_amount = None
        self.last_signal = 0  # 0: None, 1: Buy, -1: Sell

    def start(self, clock: Clock, timestamp: float) -> None:
        self._last_timestamp = timestamp
        self.apply_initial_setting()

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        create_actions = []
        
        # 1. Get Market Data
        candles = self.market_data_provider.get_candles_df(
            self.config.candles_exchange, 
            self.config.candles_pair, 
            self.config.candles_interval,
            self.config.candles_length + 20
        )
        
        if candles.empty:
            return []

        # 2. Calculate Indicators (RSI & ATR)
        # RSI
        candles.ta.rsi(length=self.config.rsi_length, append=True)
        self.current_rsi = candles.iloc[-1][f"RSI_{self.config.rsi_length}"]
        
        # ATR (Average True Range) for Volatility
        candles.ta.atr(length=self.config.atr_length, append=True)
        self.current_atr = candles.iloc[-1][f"ATRr_{self.config.atr_length}"]
        
        # 3. Determine Signal
        signal = 0
        if self.current_rsi < self.config.rsi_oversold:
            signal = 1  # Buy Signal
        elif self.current_rsi > self.config.rsi_overbought:
            signal = -1  # Sell Signal
        
        self.last_signal = signal

        # 4. Check for existing positions
        active_longs, active_shorts = self.get_active_executors_by_side(self.config.exchange, self.config.trading_pair)
        
        # 5. Execute Logic if Signal exists and no conflicting position
        if signal != 0:
            # We only enter if we don't have a position in that direction
            # For simplicity in this demo, we assume One-Way mode (hedging logic omitted for clarity)
            if (signal == 1 and len(active_longs) == 0) or (signal == -1 and len(active_shorts) == 0):
                
                # --- DYNAMIC PARAMETER CALCULATION ---
                
                mid_price = self.market_data_provider.get_price_by_type(
                    self.config.exchange, self.config.trading_pair, PriceType.MidPrice
                )
                
                # A. Calculate Dynamic Stop Loss based on ATR
                # SL Distance = ATR * Multiplier
                sl_distance = self.current_atr * self.config.atr_multiplier_sl
                
                # Convert to percentage for the Executor Config (required by TripleBarrierConfig)
                # SL % = SL Distance / Price
                self.dynamic_sl_pct = Decimal(sl_distance / mid_price)
                
                # B. Calculate Dynamic Take Profit based on Risk:Reward
                # TP Distance = SL Distance * R:R Ratio
                tp_distance = sl_distance * self.config.risk_reward_ratio
                self.dynamic_tp_pct = Decimal(tp_distance / mid_price)
                
                # C. Calculate Dynamic Order Amount based on Risk %
                # Risk Amount = Account Balance * Risk % (e.g., $1000 * 1% = $10)
                # Position Size = Risk Amount / SL % (e.g., $10 / 5% = $200)
                
                # Get Quote Balance (USDT)
                # Note: For real trading, check available balance. Using total balance for demo calculation.
                quote_balance = self.get_balance_df()
                # Simple lookup for quote balance (assuming quote asset is the second part of pair)
                quote_asset = self.config.trading_pair.split("-")[1]
                quote_balance_row = quote_balance[quote_balance["Asset"] == quote_asset]
                
                if not quote_balance_row.empty:
                    total_balance = float(quote_balance_row.iloc[0]["Total Balance"])
                else:
                    total_balance = 0.0
                
                risk_amount = total_balance * self.config.risk_per_trade_pct
                
                # Avoid division by zero
                if self.dynamic_sl_pct > 0:
                    position_size_quote = risk_amount / float(self.dynamic_sl_pct)
                else:
                    position_size_quote = risk_amount # Fallback
                
                # Apply leverage to amount if needed? 
                # Ideally, Position Size is the Notional Value. 
                # The 'amount' in Executor is Base Asset Amount.
                
                amount_base = Decimal(position_size_quote / mid_price)
                self.calculated_amount = amount_base
                
                # --- END DYNAMIC CALCULATION ---

                # Create Executor
                triple_barrier_config = TripleBarrierConfig(
                    stop_loss=self.dynamic_sl_pct,
                    take_profit=self.dynamic_tp_pct,
                    time_limit=self.config.time_limit,
                    open_order_type=OrderType.MARKET,
                    take_profit_order_type=OrderType.LIMIT,
                    stop_loss_order_type=OrderType.MARKET,
                    time_limit_order_type=OrderType.MARKET
                )
                
                create_actions.append(CreateExecutorAction(
                    executor_config=PositionExecutorConfig(
                        timestamp=self.current_timestamp,
                        connector_name=self.config.exchange,
                        trading_pair=self.config.trading_pair,
                        side=TradeType.BUY if signal == 1 else TradeType.SELL,
                        entry_price=Decimal(mid_price),
                        amount=self.calculated_amount,
                        triple_barrier_config=triple_barrier_config,
                        leverage=self.config.leverage
                    )))
                    
        return create_actions

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        # Simple logic: If signal reverses, close opposite positions?
        # For this demo, we rely on Triple Barrier (TP/SL/Time) to close.
        return []

    def get_active_executors_by_side(self, connector_name: str, trading_pair: str):
        active_executors = self.filter_executors(
            executors=self.get_all_executors(),
            filter_func=lambda e: e.connector_name == connector_name and e.trading_pair == trading_pair and e.is_active
        )
        active_longs = [e for e in active_executors if e.side == TradeType.BUY]
        active_shorts = [e for e in active_executors if e.side == TradeType.SELL]
        return active_longs, active_shorts

    def apply_initial_setting(self):
        if not self.account_config_set:
            for connector_name, connector in self.connectors.items():
                if self.is_perpetual(connector_name):
                    connector.set_position_mode(PositionMode.ONEWAY)
                    for trading_pair in self.market_data_provider.get_trading_pairs(connector_name):
                        connector.set_leverage(trading_pair, self.config.leverage)
            self.account_config_set = True

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        
        lines = []
        lines.append(f"\n{'=' * 40}")
        lines.append(f" Dynamic Strategy Demo Status")
        lines.append(f"{'=' * 40}\n")
        
        # 1. Market Data & Indicators
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.exchange, self.config.trading_pair, PriceType.MidPrice
        )
        lines.append(f"Market: {self.config.exchange} | {self.config.trading_pair}")
        lines.append(f"Price: {mid_price:.4f}")
        
        rsi_str = f"{self.current_rsi:.2f}" if self.current_rsi is not None else "N/A"
        atr_str = f"{self.current_atr:.4f}" if self.current_atr is not None else "N/A"
        lines.append(f"RSI ({self.config.rsi_length}): {rsi_str}")
        lines.append(f"ATR ({self.config.atr_length}): {atr_str}")
        
        # 2. Dynamic Parameters Preview
        # Show what the parameters would be if we traded NOW
        lines.append(f"\n--- Dynamic Parameters (Real-time Calculation) ---")
        
        if self.current_atr and mid_price:
            calc_sl_dist = self.current_atr * self.config.atr_multiplier_sl
            calc_sl_pct = (calc_sl_dist / mid_price) * 100
            
            calc_tp_dist = calc_sl_dist * self.config.risk_reward_ratio
            calc_tp_pct = (calc_tp_dist / mid_price) * 100
            
            lines.append(f"Based on Current Volatility (ATR):")
            lines.append(f"  • Stop Loss Distance: {calc_sl_dist:.4f} ({calc_sl_pct:.2f}%)")
            lines.append(f"  • Take Profit Distance: {calc_tp_dist:.4f} ({calc_tp_pct:.2f}%)")
            lines.append(f"  • Risk Per Trade: {self.config.risk_per_trade_pct * 100}% of Balance")
            
            if self.calculated_amount:
                 lines.append(f"  • Last Calc Amount: {self.calculated_amount:.4f} {self.config.trading_pair.split('-')[0]}")
        else:
            lines.append("  Waiting for enough candle data...")

        # 3. Active Orders/Positions
        lines.append(f"\n--- Active Executors ---")
        active_executors = self.get_all_executors()
        if active_executors:
            for e in active_executors:
                lines.append(f"ID: {e.id[:8]} | Side: {e.side.name} | PnL: {e.net_pnl_pct:.2f}% | Status: {e.status.name}")
        else:
            lines.append("  No active executors.")
            
        lines.append(f"\n{'=' * 40}")
        return "\n".join(lines)
