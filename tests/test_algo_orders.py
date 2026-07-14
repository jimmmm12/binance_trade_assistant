from trade_assistant.broker import LIVE_CONFIRMATION, place_order


class _Client:
    def __init__(self) -> None:
        self.path = ""
        self.payload = {}

    def signed_post(self, market, path, payload):
        self.path = path
        self.payload = payload
        return {"algoId": "123"}


def test_futures_stop_market_uses_algo_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_ENABLE_LIVE_TRADING", "true")
    client = _Client()

    result = place_order(
        client,
        "futures",
        {"symbol": "UNIUSDT", "side": "SELL", "type": "STOP_MARKET", "quantity": "1", "stopPrice": 10, "newClientOrderId": "LOCAL_STOP"},
        True,
        LIVE_CONFIRMATION,
    )

    assert client.path == "/fapi/v1/algoOrder"
    assert client.payload["algoType"] == "CONDITIONAL"
    assert client.payload["triggerPrice"] == 10
    assert client.payload["newClientAlgoId"] == "LOCAL_STOP"
    assert result["clientOrderId"] == "LOCAL_STOP"
