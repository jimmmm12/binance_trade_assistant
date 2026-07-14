# 真下单说明

这个项目已经有真下单能力，但默认关闭。

真下单模板是：

```text
run_order_live_TEMPLATE.bat
```

## 使用前必须知道

- 这个文件可能真实买入或卖出。
- 不要用带提现权限的 API Key。
- API Key 只开交易权限，不开提现权限。
- 新手先用 `run_order_dry_run_example.bat` 模拟。
- 第一次真下单只用很小金额测试。
- 合约下单前必须确认逐仓/全仓、杠杆、持仓模式。

## 真下单文件怎么改

右键 `run_order_live_TEMPLATE.bat`，选择“编辑”。

先改 API：

```bat
set BINANCE_API_KEY=PUT_YOUR_API_KEY_HERE
set BINANCE_API_SECRET=PUT_YOUR_API_SECRET_HERE
```

再改订单：

```bat
python -m trade_assistant.main order --market spot --symbol UNIUSDT --side BUY --quantity 1 --type MARKET --allow-live --confirm 确认下单
```

参数含义：

```text
--market spot       现货
--market futures    U本位合约
--symbol UNIUSDT    交易对
--side BUY          买入/开多/平空
--side SELL         卖出/开空/平多
--quantity 1        数量
--type MARKET       市价单
--type LIMIT        限价单
```

限价单示例：

```bat
python -m trade_assistant.main order --market spot --symbol UNIUSDT --side BUY --quantity 1 --type LIMIT --price 3.20 --allow-live --confirm 确认下单
```

## 三重开关

真下单必须同时满足：

```text
1. 设置 BINANCE_API_KEY 和 BINANCE_API_SECRET
2. 设置 BINANCE_ENABLE_LIVE_TRADING=true
3. 命令里带 --allow-live --confirm 确认下单
```

少一个条件，程序就只会 dry-run，不会真下单。

## 强烈建议

先不要直接用真下单模板。

推荐顺序：

```text
1. run_scan_futures.bat 扫描机会
2. run_plan_example.bat 计算风险
3. run_order_dry_run_example.bat 模拟下单
4. 小金额真下单测试
```

