import sys
from unittest.mock import MagicMock

# 1. Mock problematic modules BEFORE importing the unit under test
# Core and Connector
sys.modules["hummingbot.connector.exchange_py_base"] = MagicMock()
sys.modules["hummingbot.connector.trading_rule"] = MagicMock()
sys.modules["hummingbot.core.data_type.limit_order"] = MagicMock()
sys.modules["hummingbot.core.data_type.in_flight_order"] = MagicMock()
sys.modules["hummingbot.connector.client_order_tracker"] = MagicMock()
sys.modules["hummingbot.core.network_iterator"] = MagicMock()

# Utils
sys.modules["base58"] = MagicMock()

# Mock MarketsRecorder and Position which are used in imports
mock_mr_module = MagicMock()
sys.modules["hummingbot.connector.markets_recorder"] = mock_mr_module

mock_pos_module = MagicMock()
sys.modules["hummingbot.model.position"] = mock_pos_module

# Mock executors modules prevent deep imports
sys.modules["hummingbot.strategy_v2.executors.arbitrage_executor.arbitrage_executor"] = MagicMock()
sys.modules["hummingbot.strategy_v2.executors.dca_executor.dca_executor"] = MagicMock()
sys.modules["hummingbot.strategy_v2.executors.grid_executor.grid_executor"] = MagicMock()
sys.modules["hummingbot.strategy_v2.executors.order_executor.order_executor"] = MagicMock()
sys.modules["hummingbot.strategy_v2.executors.position_executor.position_executor"] = MagicMock()
sys.modules["hummingbot.strategy_v2.executors.twap_executor.twap_executor"] = MagicMock()
sys.modules["hummingbot.strategy_v2.executors.xemm_executor.xemm_executor"] = MagicMock()


# 2. Now import unittest and other standard libs
import unittest
from decimal import Decimal
from unittest.mock import PropertyMock, patch

# 3. Import common types that likely exist as pure python, or mock them if they fail
try:
    from hummingbot.core.data_type.common import TradeType
except ImportError:
    # Fallback if common.py has issues (unlikely but safe)
    TradeType = MagicMock()
    TradeType.BUY.name = "BUY"
    TradeType.SELL.name = "SELL"

# 4. Import the class under test
# This might still import other things, so we might need more mocks
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator

class TestPersistenceRepro(unittest.TestCase):
    def setUp(self):
        self.mock_strategy = self.create_mock_strategy()

    @staticmethod
    def create_mock_strategy():
        # Mock strategy and its dependencies
        strategy = MagicMock()
        strategy.markets = {"binance": {"ETH-USDT"}}
        strategy.controllers = {"test_controller": MagicMock()}
        
        # Mock connection to connector/market
        market = MagicMock()
        strategy.connectors = {"binance": market}
        
        # Mock market data provider
        mdp = MagicMock()
        mdp.get_price_by_type.return_value = Decimal(230)
        strategy.market_data_provider = mdp
        strategy.config = MagicMock() # Mock config for later use
        
        return strategy

    @patch("hummingbot.strategy_v2.executors.executor_orchestrator.MarketsRecorder")
    def test_default_behavior_loads_positions(self, mock_markets_recorder_class):
        # Setup mock recorder instance
        mock_recorder = mock_markets_recorder_class.get_instance.return_value
        
        # Create a mock position object (since we mocked the class)
        # We need to structure it so it looks like the real Position object to the code
        db_position = MagicMock()
        db_position.controller_id = "test_controller"
        db_position.connector_name = "binance"
        db_position.trading_pair = "ETH-USDT"
        db_position.side = "BUY"
        db_position.amount = Decimal("1")
        db_position.breakeven_price = Decimal("1000")
        db_position.unrealized_pnl_quote = Decimal("50")
        db_position.cum_fees_quote = Decimal("5")
        db_position.volume_traded_quote = Decimal("1000")
        
        # Mock methods to return our fake data
        mock_recorder.get_all_executors.return_value = []
        mock_recorder.get_all_positions.return_value = [db_position]
        
        # Initialize orchestrator
        # This calls _initialize_cached_performance -> get_all_positions
        orchestrator = ExecutorOrchestrator(strategy=self.mock_strategy)
        
        # Assert positions ARE loaded (Current behavior causing the issue)
        # The key check is that it DID read from DB and populated positions_held
        if "test_controller" in orchestrator.positions_held and len(orchestrator.positions_held["test_controller"]) > 0:
            print("\nSUCCESS: Reproduction confirmed - Position was loaded from DB")
        else:
            self.fail("Reproduction failed - Position was NOT loaded from DB")
