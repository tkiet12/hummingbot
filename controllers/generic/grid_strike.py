from decimal import Decimal
from typing import List, Optional

from typing import List, Optional

import pandas_ta as ta  # noqa: F401
from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class GridStrikeConfig(ControllerConfigBase):
    """
    Configuration required to run the GridStrike strategy for one connector and trading pair.
    """
    controller_type: str = "generic"
    controller_name: str = "grid_strike"

    # Account configuration
    leverage: int = 20
    position_mode: PositionMode = PositionMode.HEDGE

    # Boundaries
    connector_name: str = "okx"
    trading_pair: str = "WLD-USDT"
    side: TradeType = TradeType.BUY
    start_price: Decimal = Field(default=Decimal("0.38"), json_schema_extra={"is_updatable": True})
    end_price: Decimal = Field(default=Decimal("0.75"), json_schema_extra={"is_updatable": True})
    limit_price: Decimal = Field(default=Decimal("0.35"), json_schema_extra={"is_updatable": True})

    # Profiling
    total_amount_quote: Decimal = Field(default=Decimal("1000"), json_schema_extra={"is_updatable": True})
    min_spread_between_orders: Optional[Decimal] = Field(default=Decimal("0.001"), json_schema_extra={"is_updatable": True})
    min_order_amount_quote: Optional[Decimal] = Field(default=Decimal("5"), json_schema_extra={"is_updatable": True})

    # Execution
    max_open_orders: int = Field(default=2, json_schema_extra={"is_updatable": True})
    max_orders_per_batch: Optional[int] = Field(default=1, json_schema_extra={"is_updatable": True})
    order_frequency: int = Field(default=3, json_schema_extra={"is_updatable": True})
    activation_bounds: Optional[Decimal] = Field(default=None, json_schema_extra={"is_updatable": True})
    keep_position: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    deduct_base_fees: bool = Field(default=False, json_schema_extra={"is_updatable": True})

    # Risk Management
    triple_barrier_config: TripleBarrierConfig = TripleBarrierConfig(
        take_profit=Decimal("0.001"),
        open_order_type=OrderType.LIMIT_MAKER,
        take_profit_order_type=OrderType.LIMIT_MAKER,
    )

    # Dynamic Parameters
    dynamic_target: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    use_bollinger_bands: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    bb_length: int = Field(default=100, json_schema_extra={"is_updatable": True})
    bb_std: float = Field(default=2.0, json_schema_extra={"is_updatable": True})
    interval: str = Field(default="1m", json_schema_extra={"is_updatable": True})
    grid_range: Optional[Decimal] = Field(default=Decimal("0.02"), json_schema_extra={"is_updatable": True})

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class GridStrike(ControllerBase):
    def __init__(self, config: GridStrikeConfig, *args, **kwargs):
        config.candles_config = [CandlesConfig(
            connector=config.connector_name,
            trading_pair=config.trading_pair,
            interval=config.interval,
            max_records=config.bb_length + 100
        )]
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_grid_levels_update = 0
        self.trading_rules = None
        self.grid_levels = []
        self.initialize_rate_sources()

    def initialize_rate_sources(self):
        self.market_data_provider.initialize_rate_sources([ConnectorPair(connector_name=self.config.connector_name,
                                                                         trading_pair=self.config.trading_pair)])

    def active_executors(self) -> List[ExecutorInfo]:
        return [
            executor for executor in self.executors_info
            if executor.is_active
        ]

    def is_inside_bounds(self, price: Decimal, start_price: Decimal, end_price: Decimal) -> bool:
        return start_price <= price <= end_price

    def determine_executor_actions(self) -> List[ExecutorAction]:
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)

        # Calculate dynamic parameters
        start_price = self.config.start_price
        end_price = self.config.end_price
        
        if self.config.dynamic_target:
            if self.config.use_bollinger_bands:
                 # Use pre-calculated BB width from processed_data
                bb_width = self.processed_data.get("bb_width", self.config.grid_range)
                grid_range = Decimal(str(bb_width))
            else:
                grid_range = self.config.grid_range

            # Calculate start and end price centered around mid_price
            half_range = grid_range / 2
            start_price = mid_price * (1 - half_range)
            end_price = mid_price * (1 + half_range)

            # Update limit price based on side
            if self.config.side == TradeType.BUY:
                 self.config.limit_price = start_price * (1 - Decimal("0.001")) # slightly below start
            else:
                 self.config.limit_price = end_price * (1 + Decimal("0.001")) # slightly above end

        if len(self.active_executors()) == 0 and self.is_inside_bounds(mid_price, start_price, end_price):
            return [CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=GridExecutorConfig(
                    timestamp=self.market_data_provider.time(),
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    start_price=start_price,
                    end_price=end_price,
                    leverage=self.config.leverage,
                    limit_price=self.config.limit_price,
                    side=self.config.side,
                    total_amount_quote=self.config.total_amount_quote,
                    min_spread_between_orders=self.config.min_spread_between_orders,
                    min_order_amount_quote=self.config.min_order_amount_quote,
                    max_open_orders=self.config.max_open_orders,
                    max_orders_per_batch=self.config.max_orders_per_batch,
                    order_frequency=self.config.order_frequency,
                    activation_bounds=self.config.activation_bounds,
                    triple_barrier_config=self.config.triple_barrier_config,
                    level_id=None,
                    keep_position=self.config.keep_position,
                    deduct_base_fees=self.config.deduct_base_fees,
                ))]
        return []

    async def update_processed_data(self):
        if self.config.dynamic_target and self.config.use_bollinger_bands:
            candles = self.market_data_provider.get_candles_df(
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                interval=self.config.interval,
                max_records=self.config.bb_length + 100
            )
            if len(candles) > self.config.bb_length:
                bb = ta.bbands(candles["close"], length=self.config.bb_length, std=self.config.bb_std)
                # handle different column names from pandas_ta
                std_str = f"{self.config.bb_std}"
                if std_str.endswith(".0"):
                    std_str = std_str[:-2]
                
                potential_cols = [
                    f"BBB_{self.config.bb_length}_{self.config.bb_std}",
                    f"BBB_{self.config.bb_length}_{std_str}",
                    f"BBB_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}",
                ]
                
                found_col = None
                for col in potential_cols:
                    if col in bb.columns:
                        found_col = col
                        break
                
                if not found_col:
                    prefix = f"BBB_{self.config.bb_length}"
                    for col in bb.columns:
                        if col.startswith(prefix):
                            found_col = col
                            break
                            
                if found_col:
                     # pandas_ta returns percentage as whole number (e.g. 2.5 for 2.5%), so divide by 100
                     bb_width = bb[found_col].iloc[-1] / 100
                     self.processed_data["bb_width"] = bb_width
                else:
                    self.logger().error(f"Could not find BBB column. Available columns: {bb.columns}")

    def to_format_status(self) -> List[str]:
        status = []
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        # Define standard box width for consistency
        box_width = 114
        # Top Grid Configuration box with simple borders
        status.append("┌" + "─" * box_width + "┐")
        # First line: Grid Configuration and Mid Price
        left_section = "Grid Configuration:"
        padding = box_width - len(left_section) - 4  # -4 for the border characters and spacing
        config_line1 = f"│ {left_section}{' ' * padding}"
        padding2 = box_width - len(config_line1) + 1  # +1 for correct right border alignment
        config_line1 += " " * padding2 + "│"
        status.append(config_line1)
        # Second line: Configuration parameters
        config_line2 = f"│ Start: {self.config.start_price:.4f} │ End: {self.config.end_price:.4f} │ Side: {self.config.side} │ Limit: {self.config.limit_price:.4f} │ Mid Price: {mid_price:.4f} │"
        padding = box_width - len(config_line2) + 1  # +1 for correct right border alignment
        config_line2 += " " * padding + "│"
        status.append(config_line2)
        # Third line: Max orders and Inside bounds
        # Determine current start/end price for display (if no active executor)
        display_start = self.config.start_price
        display_end = self.config.end_price
        if self.config.dynamic_target:
             # Just show static config or maybe indicate it's dynamic? 
             # For now keep it simple, or ideally calculate it just for display?
             # Let's note checking is_inside_bounds will fail if we don't pass correct params in next line
             pass 

        # We need to recalculate dynamic bounds for display if no active executor is running
        # But for status we can just show what is configured, or maybe the last calculated values?
        # Let's just use a simple display check
        is_inside = False
        # To avoid complexity in status formatting, let's just check against config for now or handle dynamic logic later
        # Re-using the logic from determine_executor_actions partly for display is okay but maybe overkill for status
        # For now, let's just show "Dynamic" if dynamic_target is True for bounds checking
        
        config_line3 = f"│ Max Orders: {self.config.max_open_orders}   │ Inside bounds: {'Dynamic' if self.config.dynamic_target else (1 if self.is_inside_bounds(mid_price, display_start, display_end) else 0)}"
        padding = box_width - len(config_line3) + 1  # +1 for correct right border alignment
        config_line3 += " " * padding + "│"
        status.append(config_line3)
        status.append("└" + "─" * box_width + "┘")
        for level in self.active_executors():
            # Define column widths for perfect alignment
            col_width = box_width // 3  # Dividing the total width by 3 for equal columns
            total_width = box_width
            # Grid Status header - use long line and running status
            status_header = f"Grid Status: {level.id} (RunnableStatus.RUNNING)"
            status_line = f"┌ {status_header}" + "─" * (total_width - len(status_header) - 2) + "┐"
            status.append(status_line)
            # Calculate exact column widths for perfect alignment
            col1_end = col_width
            # Column headers
            header_line = "│ Level Distribution" + " " * (col1_end - 20) + "│"
            header_line += " Order Statistics" + " " * (col_width - 18) + "│"
            header_line += " Performance Metrics" + " " * (col_width - 21) + "│"
            status.append(header_line)
            # Data for the three columns
            level_dist_data = [
                f"NOT_ACTIVE: {len(level.custom_info['levels_by_state'].get('NOT_ACTIVE', []))}",
                f"OPEN_ORDER_PLACED: {len(level.custom_info['levels_by_state'].get('OPEN_ORDER_PLACED', []))}",
                f"OPEN_ORDER_FILLED: {len(level.custom_info['levels_by_state'].get('OPEN_ORDER_FILLED', []))}",
                f"CLOSE_ORDER_PLACED: {len(level.custom_info['levels_by_state'].get('CLOSE_ORDER_PLACED', []))}",
                f"COMPLETE: {len(level.custom_info['levels_by_state'].get('COMPLETE', []))}"
            ]
            order_stats_data = [
                f"Total: {sum(len(level.custom_info[k]) for k in ['filled_orders', 'failed_orders', 'canceled_orders'])}",
                f"Filled: {len(level.custom_info['filled_orders'])}",
                f"Failed: {len(level.custom_info['failed_orders'])}",
                f"Canceled: {len(level.custom_info['canceled_orders'])}"
            ]
            perf_metrics_data = [
                f"Buy Vol: {level.custom_info['realized_buy_size_quote']:.4f}",
                f"Sell Vol: {level.custom_info['realized_sell_size_quote']:.4f}",
                f"R. PnL: {level.custom_info['realized_pnl_quote']:.4f}",
                f"R. Fees: {level.custom_info['realized_fees_quote']:.4f}",
                f"P. PnL: {level.custom_info['position_pnl_quote']:.4f}",
                f"Position: {level.custom_info['position_size_quote']:.4f}"
            ]
            # Build rows with perfect alignment
            max_rows = max(len(level_dist_data), len(order_stats_data), len(perf_metrics_data))
            for i in range(max_rows):
                col1 = level_dist_data[i] if i < len(level_dist_data) else ""
                col2 = order_stats_data[i] if i < len(order_stats_data) else ""
                col3 = perf_metrics_data[i] if i < len(perf_metrics_data) else ""
                row = "│ " + col1
                row += " " * (col1_end - len(col1) - 2)  # -2 for the "│ " at the start
                row += "│ " + col2
                row += " " * (col_width - len(col2) - 2)  # -2 for the "│ " before col2
                row += "│ " + col3
                row += " " * (col_width - len(col3) - 2)  # -2 for the "│ " before col3
                row += "│"
                status.append(row)
            # Liquidity line with perfect alignment
            status.append("├" + "─" * total_width + "┤")
            liquidity_line = f"│ Open Liquidity: {level.custom_info['open_liquidity_placed']:.4f} │ Close Liquidity: {level.custom_info['close_liquidity_placed']:.4f} │"
            liquidity_line += " " * (total_width - len(liquidity_line) + 1)  # +1 for correct right border alignment
            liquidity_line += "│"
            status.append(liquidity_line)
            status.append("└" + "─" * total_width + "┘")
        return status
