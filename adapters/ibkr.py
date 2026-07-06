"""Paper-only IBKR futures adapter built on ``ib_async``.

Official references checked 2026-07-06:
- Contracts: https://ibkrcampus.com/campus/ibkr-api-page/contracts/
- Orders, positions, account summary, connectivity codes:
  https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/
- Order types: https://ibkrcampus.com/campus/ibkr-api-page/order-types/
- Paper IB Gateway default port 4002:
  https://ibkrcampus.com/campus/ibkr-api-page/excel-rtd/
- ib_async 2.1 API: https://ib-api-reloaded.github.io/ib_async/api.html
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, TypeVar, cast

from ib_async import IB, Future, LimitOrder, MarketOrder, StopOrder
from ib_async.contract import Contract
from ib_async.order import Order, Trade

from core.events import Bar, OrderAck, OrderRequest, OrderStatus, OrderType, Side
from core.risk import Approved
from core.state import StateStore

ZERO = Decimal("0")
ACTIVE_ORDER_STATES = frozenset(
    {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted", "ValidationError", "ApiUpdate"}
)
REJECTED_ORDER_STATES = frozenset({"Inactive", "Cancelled", "ApiCancelled"})
STOP_REF_PREFIX = "CATSTOP:"
T = TypeVar("T")


class PaperAccountRequired(PermissionError):
    pass


class ExecutionWindowClosed(RuntimeError):
    pass


class ContractQualificationError(RuntimeError):
    pass


class StopCoverageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FuturesSpec:
    symbol: str
    exchange: str
    currency: str
    multiplier: Decimal
    tick_size: Decimal

    def __post_init__(self) -> None:
        values = (self.multiplier, self.tick_size)
        if any(not value.is_finite() or value <= ZERO for value in values):
            raise ValueError("futures multiplier and tick must be positive finite Decimals")


FUTURES_SPECS: Mapping[str, FuturesSpec] = {
    "MES": FuturesSpec("MES", "CME", "USD", Decimal("5"), Decimal("0.25")),
    "MNQ": FuturesSpec("MNQ", "CME", "USD", Decimal("2"), Decimal("0.25")),
    "M2K": FuturesSpec("M2K", "CME", "USD", Decimal("5"), Decimal("0.10")),
    "MYM": FuturesSpec("MYM", "CBOT", "USD", Decimal("0.5"), Decimal("1")),
    "MGC": FuturesSpec("MGC", "COMEX", "USD", Decimal("10"), Decimal("0.10")),
    "MHG": FuturesSpec("MHG", "COMEX", "USD", Decimal("2500"), Decimal("0.0005")),
    "MCL": FuturesSpec("MCL", "NYMEX", "USD", Decimal("100"), Decimal("0.01")),
    "MNG": FuturesSpec("MNG", "NYMEX", "USD", Decimal("1000"), Decimal("0.001")),
    "M6E": FuturesSpec("M6E", "CME", "USD", Decimal("12500"), Decimal("0.0001")),
    "M6B": FuturesSpec("M6B", "CME", "USD", Decimal("6250"), Decimal("0.0001")),
    "M6A": FuturesSpec("M6A", "CME", "USD", Decimal("10000"), Decimal("0.0001")),
}


@dataclass(frozen=True, slots=True)
class ExecutionWindow:
    start: time = time(14, 30, tzinfo=UTC)
    end: time = time(14, 35, tzinfo=UTC)

    def contains(self, value: datetime) -> bool:
        observed = _aware(value).astimezone(UTC).timetz()
        return self.start <= observed < self.end


@dataclass(frozen=True, slots=True)
class IBKRConfig:
    account: str
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 21
    connect_timeout_seconds: int = 10
    limit_timeout_seconds: int = 30
    restart_retries: int = 3
    execution_window: ExecutionWindow = ExecutionWindow()

    def __post_init__(self) -> None:
        if not self.account.startswith("DU"):
            raise PaperAccountRequired("IBKR adapter requires a DU-prefixed paper account")
        if self.port <= 0 or self.client_id < 0:
            raise ValueError("IBKR port/client ID are invalid")
        if self.connect_timeout_seconds <= 0 or self.limit_timeout_seconds <= 0:
            raise ValueError("IBKR timeouts must be positive")
        if self.restart_retries < 1:
            raise ValueError("IBKR restart retries must be positive")

    @classmethod
    def from_environment(cls) -> IBKRConfig:
        try:
            account = os.environ["IBKR_ACCOUNT"]
        except KeyError:
            raise RuntimeError("missing required environment variable IBKR_ACCOUNT") from None
        return cls(
            account=account,
            host=os.environ.get("IBKR_HOST", "127.0.0.1"),
            port=int(os.environ.get("IBKR_PORT", "4002")),
            client_id=int(os.environ.get("IBKR_CLIENT_ID", "21")),
        )


@dataclass(frozen=True, slots=True)
class IBKRPosition:
    account: str
    symbol: str
    contract_month: str
    local_symbol: str
    con_id: int
    quantity: Decimal
    average_cost: Decimal


@dataclass(frozen=True, slots=True)
class IBKRAccountValue:
    account: str
    tag: str
    value: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class StopCheck:
    symbol: str
    contract_month: str
    con_id: int
    position_quantity: Decimal
    covered_quantity: Decimal
    expected_action: str
    valid: bool
    reason: str


@dataclass(frozen=True, slots=True)
class StopVerificationReport:
    generated_at: datetime
    account: str
    checks: tuple[StopCheck, ...]

    @property
    def all_covered(self) -> bool:
        return all(item.valid for item in self.checks)


def average_true_range(bars: Sequence[Bar], period: int = 20) -> Decimal:
    """Return the simple ATR over the final ``period`` true ranges."""

    if period < 1 or len(bars) < period + 1:
        raise ValueError("ATR requires period + 1 bars")
    ranges: list[Decimal] = []
    for previous, current in pairwise(bars):
        if current.symbol != previous.symbol:
            raise ValueError("ATR bars must share a symbol")
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    selected = ranges[-period:]
    return sum(selected, ZERO) / Decimal(period)


def catastrophe_stop_request(
    *,
    symbol: str,
    contract_month: str,
    position_quantity: Decimal,
    reference_price: Decimal,
    bars: Sequence[Bar],
    created_at: datetime,
) -> OrderRequest:
    """Create the exact 3xATR(20) exit request that must pass RiskManager."""

    spec = _spec(symbol)
    _validate_contract_month(contract_month)
    if not position_quantity.is_finite() or position_quantity == ZERO:
        raise ValueError("catastrophe stop requires a nonzero finite position")
    if not reference_price.is_finite() or reference_price <= ZERO:
        raise ValueError("reference price must be positive and finite")
    atr = average_true_range(bars, 20)
    if atr <= ZERO:
        raise ValueError("ATR must be positive")
    side = Side.SELL if position_quantity > ZERO else Side.BUY
    raw_stop = (
        reference_price - Decimal("3") * atr
        if side is Side.SELL
        else reference_price + Decimal("3") * atr
    )
    rounding = ROUND_FLOOR if side is Side.SELL else ROUND_CEILING
    stop_price = (raw_stop / spec.tick_size).to_integral_value(rounding=rounding) * spec.tick_size
    if stop_price <= ZERO:
        raise ValueError("catastrophe stop must be positive")
    return OrderRequest(
        client_order_id=f"catstop:{symbol}:{contract_month}:{created_at.date().isoformat()}",
        symbol=symbol,
        side=side,
        quantity=abs(position_quantity),
        order_type=OrderType.STOP,
        created_at=_aware(created_at),
        stop_price=stop_price,
        strategy_id="futures_trend",
        expected_price=reference_price,
        is_exit=True,
    )


class IBKRAdapter:
    """Fail-closed IB Gateway adapter restricted to paper accounts."""

    def __init__(
        self,
        config: IBKRConfig,
        state: StateStore,
        *,
        ib: IB | Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._state = state
        self._ib = ib or IB()
        self._clock = clock
        self._sleep = sleep
        self._contracts: dict[tuple[str, str], Contract] = {}
        self._connectivity_lost = False
        self._market_data_lost = False
        error_event = getattr(self._ib, "errorEvent", None)
        if error_event is not None:
            error_event += self._on_error

    async def connect(self) -> None:
        if self._is_connected():
            self._verify_paper_accounts()
            return
        await self._ib.connectAsync(
            self.config.host,
            self.config.port,
            clientId=self.config.client_id,
            timeout=self.config.connect_timeout_seconds,
            readonly=False,
            account=self.config.account,
            raiseSyncErrors=True,
        )
        self._verify_paper_accounts()
        self._connectivity_lost = False

    async def close(self) -> None:
        if self._is_connected():
            self._ib.disconnect()

    async def ensure_connected(self) -> None:
        if self._is_connected() and not self._connectivity_lost:
            return
        last_error: Exception | None = None
        for attempt in range(self.config.restart_retries):
            try:
                if self._is_connected():
                    self._ib.disconnect()
                await self.connect()
                return
            except Exception as error:
                last_error = error
                if attempt + 1 < self.config.restart_retries:
                    await self._sleep(float(2**attempt))
        raise ConnectionError("IB Gateway paper reconnect failed") from last_error

    async def qualify_contract(self, symbol: str, contract_month: str) -> Contract:
        await self.ensure_connected()
        key = (symbol, contract_month)
        cached = self._contracts.get(key)
        if cached is not None:
            return cached
        spec = _spec(symbol)
        _validate_contract_month(contract_month)
        candidate = Future(
            symbol=symbol,
            lastTradeDateOrContractMonth=contract_month,
            exchange=spec.exchange,
            multiplier=str(spec.multiplier),
            currency=spec.currency,
        )
        result = await self._ib.qualifyContractsAsync(candidate)
        if len(result) != 1 or result[0] is None or isinstance(result[0], list):
            raise ContractQualificationError(
                f"IBKR did not uniquely qualify {symbol} {contract_month}"
            )
        contract = result[0]
        identity_matches = (
            contract.secType == "FUT"
            and contract.symbol == symbol
            and _contract_month(contract.lastTradeDateOrContractMonth) == contract_month
            and contract.currency == spec.currency
            and contract.exchange == spec.exchange
            and Decimal(str(contract.multiplier)) == spec.multiplier
            and int(contract.conId) > 0
        )
        if not identity_matches:
            raise ContractQualificationError("IBKR returned a mismatched futures contract")
        self._contracts[key] = contract
        return contract

    async def qualify_contracts(
        self,
        contract_months: Mapping[str, str],
    ) -> Mapping[str, Contract]:
        if set(contract_months) != set(FUTURES_SPECS):
            missing = sorted(set(FUTURES_SPECS) - set(contract_months))
            extra = sorted(set(contract_months) - set(FUTURES_SPECS))
            raise ValueError(f"contract month map mismatch; missing={missing}, extra={extra}")
        qualified: dict[str, Contract] = {}
        for symbol, contract_month in contract_months.items():
            qualified[symbol] = await self.qualify_contract(symbol, contract_month)
        return qualified

    async def submit_order(self, approved: Approved, contract_month: str) -> OrderAck:
        trade = await self._submit_trade(approved, contract_month)
        return self._ack(approved.order.client_order_id, trade)

    async def submit_limit_with_market_fallback(
        self,
        limit_approved: Approved,
        market_approved: Approved,
        contract_month: str,
    ) -> OrderAck:
        """Try an approved limit, then a separately approved market order in-window."""

        limit_request = limit_approved.order
        market_request = market_approved.order
        self._validate_fallback_pair(limit_request, market_request)
        if not self.config.execution_window.contains(self._clock()):
            raise ExecutionWindowClosed("outside fixed IBKR futures execution window")

        limit_trade = await self._restart_retry(
            lambda: self._submit_trade(limit_approved, contract_month)
        )
        deadline = self._clock() + timedelta(seconds=self.config.limit_timeout_seconds)
        while self._clock() < deadline and not limit_trade.isDone():
            await self._sleep(0.25)
        if limit_trade.isDone():
            return self._ack(limit_request.client_order_id, limit_trade)

        self._ib.cancelOrder(limit_trade.order)
        await self._sleep(0.25)
        if limit_trade.orderStatus.status == "Filled":
            return self._ack(limit_request.client_order_id, limit_trade)
        if limit_trade.orderStatus.status not in {"Cancelled", "ApiCancelled"}:
            raise RuntimeError("limit cancellation was not confirmed; refusing market fallback")
        if not self.config.execution_window.contains(self._clock()):
            raise ExecutionWindowClosed("market fallback window closed after limit cancellation")
        market_trade = await self._restart_retry(
            lambda: self._submit_trade(market_approved, contract_month)
        )
        return self._ack(market_request.client_order_id, market_trade)

    async def submit_catastrophe_stop(
        self,
        approved: Approved,
        contract_month: str,
    ) -> OrderAck:
        order = approved.order
        if order.order_type is not OrderType.STOP or not order.is_exit:
            raise ValueError("catastrophe stop requires an approved risk-reducing STOP order")
        return await self.submit_order(approved, contract_month)

    async def positions(self) -> tuple[IBKRPosition, ...]:
        await self.ensure_connected()
        raw_positions = await self._ib.reqPositionsAsync()
        output: list[IBKRPosition] = []
        for item in raw_positions:
            contract = item.contract
            if contract.secType != "FUT" or contract.symbol not in FUTURES_SPECS:
                continue
            if item.account != self.config.account:
                continue
            output.append(
                IBKRPosition(
                    item.account,
                    contract.symbol,
                    _contract_month(contract.lastTradeDateOrContractMonth),
                    contract.localSymbol,
                    int(contract.conId),
                    Decimal(str(item.position)),
                    Decimal(str(item.avgCost)),
                )
            )
        return tuple(output)

    async def account_values(self) -> tuple[IBKRAccountValue, ...]:
        await self.ensure_connected()
        values = await self._ib.accountSummaryAsync(self.config.account)
        output: list[IBKRAccountValue] = []
        for item in values:
            if item.account != self.config.account:
                continue
            try:
                value = Decimal(item.value)
            except Exception:
                continue
            if value.is_finite():
                output.append(IBKRAccountValue(item.account, item.tag, value, item.currency))
        return tuple(output)

    async def verify_catastrophe_stops(self) -> StopVerificationReport:
        await self.ensure_connected()
        positions = tuple(item for item in await self.positions() if item.quantity != ZERO)
        await self._ib.reqOpenOrdersAsync()
        trades = tuple(self._ib.openTrades())
        checks: list[StopCheck] = []
        for position in positions:
            expected_action = "SELL" if position.quantity > ZERO else "BUY"
            matching = [
                trade
                for trade in trades
                if int(trade.contract.conId) == position.con_id
                and trade.order.orderType == "STP"
                and str(trade.order.orderRef).startswith(STOP_REF_PREFIX)
                and trade.orderStatus.status in ACTIVE_ORDER_STATES
            ]
            covered = sum(
                (Decimal(str(trade.order.totalQuantity)) for trade in matching),
                ZERO,
            )
            actions_valid = all(trade.order.action == expected_action for trade in matching)
            tif_valid = all(trade.order.tif == "GTC" and trade.order.transmit for trade in matching)
            price_valid = all(Decimal(str(trade.order.auxPrice)) > ZERO for trade in matching)
            quantity_valid = covered == abs(position.quantity)
            valid = (
                bool(matching)
                and actions_valid
                and tif_valid
                and price_valid
                and quantity_valid
            )
            reasons: list[str] = []
            if not matching:
                reasons.append("missing_stop")
            if not actions_valid:
                reasons.append("wrong_action")
            if not tif_valid:
                reasons.append("not_broker_resident_gtc")
            if not price_valid:
                reasons.append("invalid_stop_price")
            if not quantity_valid:
                reasons.append("quantity_mismatch")
            checks.append(
                StopCheck(
                    position.symbol,
                    position.contract_month,
                    position.con_id,
                    position.quantity,
                    covered,
                    expected_action,
                    valid,
                    "ok" if valid else ",".join(reasons),
                )
            )
        return StopVerificationReport(self._clock(), self.config.account, tuple(checks))

    async def write_stop_verification(self, path: Path) -> StopVerificationReport:
        report = await self.verify_catastrophe_stops()
        document = {
            "generated_at": report.generated_at.isoformat(),
            "account": "paper",
            "all_covered": report.all_covered,
            "checks": [
                {
                    **asdict(item),
                    "position_quantity": str(item.position_quantity),
                    "covered_quantity": str(item.covered_quantity),
                }
                for item in report.checks
            ],
        }
        _atomic_json(path, document)
        if not report.all_covered:
            raise StopCoverageError("one or more IBKR futures positions lack catastrophe stops")
        return report

    def handle_connectivity_error(self, code: int) -> None:
        if code in {1100, 1300}:
            self._connectivity_lost = True
        elif code == 1101:
            self._connectivity_lost = False
            self._market_data_lost = True
        elif code == 1102:
            self._connectivity_lost = False
            self._market_data_lost = False

    async def _submit_trade(self, approved: Approved, contract_month: str) -> Trade:
        await self.ensure_connected()
        request = approved.order
        if request.symbol not in FUTURES_SPECS:
            raise ValueError(f"unsupported IBKR micro future {request.symbol!r}")
        contract = await self.qualify_contract(request.symbol, contract_month)
        prior = self._load_submission(request.client_order_id)
        if prior is not None and prior.get("order_id") is not None:
            existing = await self._find_trade(request.client_order_id)
            if existing is not None:
                return existing
            raise RuntimeError("persisted IBKR order is no longer discoverable; refusing duplicate")
        if prior is None:
            self._persist_submission(
                request.client_order_id,
                {"status": "PREPARED", "contract_month": contract_month},
            )
        existing = await self._find_trade(request.client_order_id)
        if existing is not None:
            return existing
        ib_order = self._ib_order(request, contract)
        trade = self._ib.placeOrder(contract, ib_order)
        self._persist_submission(
            request.client_order_id,
            {
                "status": trade.orderStatus.status,
                "contract_month": contract_month,
                "order_id": int(trade.order.orderId),
            },
        )
        return trade

    async def _find_trade(self, client_order_id: str) -> Trade | None:
        open_trades = await self._ib.reqOpenOrdersAsync()
        completed = await self._ib.reqCompletedOrdersAsync(True)
        for trade in (*open_trades, *completed, *self._ib.trades()):
            if trade.order.orderRef == client_order_id:
                return cast(Trade, trade)
        return None

    def _ib_order(self, request: OrderRequest, contract: Contract) -> Order:
        common = {
            "account": self.config.account,
            "orderRef": request.client_order_id,
            "transmit": True,
        }
        action = request.side.value
        if request.order_type is OrderType.LIMIT:
            if request.limit_price is None:
                raise ValueError("approved IBKR limit order has no limit price")
            return LimitOrder(
                action,
                request.quantity,  # type: ignore[arg-type]  # ib_async accepts Decimal at runtime
                request.limit_price,  # type: ignore[arg-type]  # preserve Decimal order math
                tif="DAY",
                **common,
            )
        if request.order_type is OrderType.MARKET:
            return MarketOrder(
                action,
                request.quantity,  # type: ignore[arg-type]  # ib_async accepts Decimal at runtime
                tif="DAY",
                **common,
            )
        if request.order_type is OrderType.STOP:
            if request.stop_price is None:
                raise ValueError("approved IBKR stop order has no stop price")
            return StopOrder(
                action,
                request.quantity,  # type: ignore[arg-type]  # ib_async accepts Decimal at runtime
                request.stop_price,  # type: ignore[arg-type]  # preserve Decimal order math
                tif="GTC",
                outsideRth=True,
                orderRef=f"{STOP_REF_PREFIX}{request.client_order_id}:{int(contract.conId)}",
                account=self.config.account,
                transmit=True,
            )
        raise ValueError(f"unsupported IBKR order type {request.order_type.value}")

    async def _restart_retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        last_error: Exception | None = None
        for attempt in range(self.config.restart_retries):
            if not self.config.execution_window.contains(self._clock()):
                raise ExecutionWindowClosed("IBKR retry left the fixed execution window")
            try:
                return await operation()
            except (ConnectionError, OSError) as error:
                last_error = error
                self._connectivity_lost = True
                await self.ensure_connected()
                if attempt + 1 < self.config.restart_retries:
                    await self._sleep(float(2**attempt))
        raise ConnectionError("IBKR order-window retry exhausted") from last_error

    @staticmethod
    def _validate_fallback_pair(limit: OrderRequest, market: OrderRequest) -> None:
        if limit.order_type is not OrderType.LIMIT or market.order_type is not OrderType.MARKET:
            raise ValueError("fallback requires separate approved LIMIT and MARKET orders")
        if (
            limit.client_order_id == market.client_order_id
            or limit.symbol != market.symbol
            or limit.side is not market.side
            or limit.quantity != market.quantity
        ):
            raise ValueError("limit and fallback approvals do not describe the same trade")

    def _ack(self, client_order_id: str, trade: Trade) -> OrderAck:
        status_text = trade.orderStatus.status
        status = (
            OrderStatus.REJECTED
            if status_text in REJECTED_ORDER_STATES
            else OrderStatus.ACCEPTED
            if status_text not in {"PendingSubmit", "ApiPending"}
            else OrderStatus.PENDING
        )
        return OrderAck(
            client_order_id,
            str(trade.order.orderId),
            status,
            self._clock(),
            reason=status_text if status is OrderStatus.REJECTED else None,
        )

    def _verify_paper_accounts(self) -> None:
        accounts = tuple(self._ib.managedAccounts())
        if self.config.account not in accounts or not accounts:
            self._ib.disconnect()
            raise PaperAccountRequired("configured IBKR paper account is not managed by Gateway")
        if any(not account.startswith("DU") for account in accounts):
            self._ib.disconnect()
            raise PaperAccountRequired("Gateway exposes a non-paper IBKR account; refusing startup")

    def _is_connected(self) -> bool:
        return bool(self._ib.isConnected())

    def _on_error(self, *args: object) -> None:
        if len(args) >= 2 and isinstance(args[1], int):
            self.handle_connectivity_error(args[1])

    @property
    def _submission_prefix(self) -> str:
        return "ibkr:submission:"

    def _load_submission(self, client_order_id: str) -> dict[str, object] | None:
        raw = self._state.get(self._submission_prefix + client_order_id)
        return None if raw is None else dict(json.loads(raw))

    def _persist_submission(self, client_order_id: str, value: Mapping[str, object]) -> None:
        self._state.set(
            self._submission_prefix + client_order_id,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        )


def _spec(symbol: str) -> FuturesSpec:
    try:
        return FUTURES_SPECS[symbol]
    except KeyError:
        raise ValueError(f"unsupported futures symbol {symbol!r}") from None


def _validate_contract_month(value: str) -> None:
    if len(value) != 6 or not value.isdigit() or int(value[4:]) not in range(1, 13):
        raise ValueError("contract month must use YYYYMM")


def _contract_month(value: str) -> str:
    if len(value) < 6:
        raise ValueError("IBKR futures contract has no valid contract month")
    month = value[:6]
    _validate_contract_month(month)
    return month


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("IBKR timestamps must be timezone-aware")
    return value


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def _main(command: str) -> None:
    config = IBKRConfig.from_environment()
    state = StateStore(Path(os.environ.get("TRADING_STATE_DB", "var/state/trading.db")))
    adapter = IBKRAdapter(config, state)
    try:
        await adapter.connect()
        if command == "verify-stops":
            report_path = Path(
                os.environ.get(
                    "IBKR_STOP_REPORT",
                    f"var/reports/ibkr-stop-verification-{datetime.now(UTC).date()}.json",
                )
            )
            await adapter.write_stop_verification(report_path)
        elif command == "account":
            await adapter.account_values()
    finally:
        await adapter.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="IBKR paper futures operations")
    parser.add_argument("command", choices=("verify-stops", "account"))
    args = parser.parse_args()
    asyncio.run(_main(args.command))


if __name__ == "__main__":
    main()
