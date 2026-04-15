"""
engine/providers/futu_client.py — 富途 OpenAPI 封装 + LiveQuoteEnricher

职责: 封装富途 OpenAPI TCP 长连接调用，提供实时期权报价；
      LiveQuoteEnricher 负责将 bid/ask 填充进 StrategyCandidate.legs。
依赖: futu-api (可选，未安装时降级), engine.models.strategy
被依赖: engine.pipeline (futu.enabled=true 时)
"""

from __future__ import annotations

import logging
from datetime import date

from engine.models.strategy import StrategyCandidate, StrategyLeg

logger = logging.getLogger(__name__)


class FutuClientError(Exception):
    """富途客户端调用失败"""


class FutuClient:
    """富途 OpenAPI 封装，提供实时期权报价。

    需要本地运行 FutuOpenD 网关。连接失败时所有方法返回空列表（降级）。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        self._host = host
        self._port = port

    def get_option_chain(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """获取期权链（所有 expiry × strike）。

        Args:
            symbol:     富途格式标的代码，如 "US.AAPL"
            start_date: 起始到期日 "YYYY-MM-DD"
            end_date:   终止到期日 "YYYY-MM-DD"

        Returns:
            list[dict] — 每行为一个期权合约记录；连接失败时返回 []。
        """
        try:
            from futu import OpenQuoteContext  # type: ignore[import]
        except ImportError:
            logger.warning("futu-api 未安装，get_option_chain 返回空列表")
            return []

        ctx = OpenQuoteContext(host=self._host, port=self._port)
        try:
            ret, data = ctx.get_option_chain(
                code=symbol,
                start=start_date,
                end=end_date,
            )
            if ret != 0:
                logger.error("Futu get_option_chain error: %s", data)
                return []
            return data.to_dict("records")
        except Exception as exc:
            logger.error("Futu get_option_chain exception: %s", exc)
            return []
        finally:
            ctx.close()

    def get_realtime_quotes(
        self,
        option_codes: list[str],
    ) -> list[dict]:
        """批量获取期权合约实时快照（bid/ask/last/volume/OI）。

        Args:
            option_codes: 富途期权代码列表，如 ["US.AAPL240419C185000"]

        Returns:
            list[dict] — 每行含 code/bid_price/ask_price 等；连接失败时返回 []。
        """
        if not option_codes:
            return []

        try:
            from futu import OpenQuoteContext  # type: ignore[import]
        except ImportError:
            logger.warning("futu-api 未安装，get_realtime_quotes 返回空列表")
            return []

        ctx = OpenQuoteContext(host=self._host, port=self._port)
        try:
            ret, data = ctx.get_market_snapshot(option_codes)
            if ret != 0:
                logger.error("Futu get_market_snapshot error: %s", data)
                return []
            return data.to_dict("records")
        except Exception as exc:
            logger.error("Futu get_market_snapshot exception: %s", exc)
            return []
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# LiveQuoteEnricher
# ---------------------------------------------------------------------------

# 富途期权代码格式: US.{SYMBOL}{YYMMDD}{C/P}{STRIKE8位，含3位小数点}
# 例: US.AAPL240419C00185000  (strike=185.0 → 00185000)
_FUTU_MARKET_PREFIX = "US"


class LiveQuoteEnricher:
    """使用富途实时报价填充 StrategyCandidate.legs 的 bid/ask。"""

    def __init__(self, futu_client: FutuClient) -> None:
        self._futu = futu_client

    def enrich(
        self,
        strategies: list[StrategyCandidate],
        symbol: str,
    ) -> list[StrategyCandidate]:
        """用富途实时报价填充 bid/ask，返回新的 StrategyCandidate 列表。

        因 StrategyLeg 为 frozen Pydantic 模型，使用 model_copy 创建新对象。
        若富途不可用（返回空列表），原策略列表原样返回。

        Args:
            strategies: 待填充的策略候选列表
            symbol:     纯标的代码，如 "AAPL"

        Returns:
            填充了 bid/ask 的新策略列表（enrichment 失败时等同于原列表）。
        """
        option_codes: list[str] = []
        for strategy in strategies:
            for leg in strategy.legs:
                code = self._build_futu_option_code(symbol, leg)
                option_codes.append(code)

        if not option_codes:
            return strategies

        try:
            quotes = self._futu.get_realtime_quotes(list(set(option_codes)))
        except Exception as exc:
            logger.error("LiveQuoteEnricher.enrich failed: %s", exc)
            return strategies

        if not quotes:
            return strategies

        quote_map: dict[str, dict] = {q["code"]: q for q in quotes}

        enriched_strategies: list[StrategyCandidate] = []
        for strategy in strategies:
            new_legs = [
                self._enrich_leg(symbol, leg, quote_map)
                for leg in strategy.legs
            ]
            enriched_strategies.append(
                strategy.model_copy(update={"legs": new_legs})
            )
        return enriched_strategies

    # ------------------------------------------------------------------
    # 私有辅助
    # ------------------------------------------------------------------

    def _enrich_leg(
        self,
        symbol: str,
        leg: StrategyLeg,
        quote_map: dict[str, dict],
    ) -> StrategyLeg:
        """返回填充了 bid/ask 的新 StrategyLeg（若无报价则原样返回）。"""
        code = self._build_futu_option_code(symbol, leg)
        if code not in quote_map:
            return leg
        q = quote_map[code]
        bid = q.get("bid_price")
        ask = q.get("ask_price")
        if bid is None and ask is None:
            return leg
        return leg.model_copy(update={"bid": bid, "ask": ask})

    @staticmethod
    def _build_futu_option_code(symbol: str, leg: StrategyLeg) -> str:
        """将内部 StrategyLeg 转换为富途期权代码格式。

        富途格式: US.{SYMBOL}{YYMMDD}{C/P}{8位strike，右对齐，小数点后3位×1000}
        示例: strike=185.0, expiry=2024-04-19, call → US.AAPL240419C00185000
        """
        expiry: date = leg.expiry
        yy = expiry.strftime("%y")
        mm = expiry.strftime("%m")
        dd = expiry.strftime("%d")
        cp = "C" if leg.option_type == "call" else "P"
        # 富途 strike 编码: 整数部分×1000，8位右补零
        strike_int = round(leg.strike * 1000)
        strike_str = f"{strike_int:08d}"
        return f"{_FUTU_MARKET_PREFIX}.{symbol.upper()}{yy}{mm}{dd}{cp}{strike_str}"
