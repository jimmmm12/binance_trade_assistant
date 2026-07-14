# Binance 交易系统内核说明

## 设计原则

- 策略只产生信号，不直接调用 Binance 下单接口。
- 所有开仓必须经过 `RiskManager`，减仓和平仓保留独立通道。
- 所有订单由 `OrderManager` 分配 `clientOrderId`、落库、提交、查询和更新状态。
- 真仓账户、持仓和活动订单启动时以 Binance 为准；本地差异会记录并修正。
- 订单发送成功不等于开仓成功，只有 Binance 返回 `FILLED` 才进入已开仓状态。

## 内核目录

```text
trade_assistant/trading_system/
├── core/events.py              统一事件总线
├── data/market_data.py         行情、K线、盘口与数据校验
├── data/user_data.py           Binance User Data Stream
├── state/models.py             TradingState
├── state/manager.py            状态持久化、恢复和交易所对账
├── risk/manager.py             最高权限开仓风控和急停
├── execution/engine.py         Binance 执行适配
├── execution/order_manager.py  订单生命周期、幂等和部分成交
├── strategy/base.py            策略插件协议和注册表
├── strategy/builtin.py         趋势、突破、均值回归插件
├── strategy/regime.py          市场状态识别
├── research/performance.py     胜率、盈亏、利润因子和回撤
├── research/optimizer.py       离线参数网格搜索
├── monitoring/metrics.py       系统、网络、订单、风险指标
├── storage/database.py         SQLite 状态、订单、交易、快照和审计
└── runtime.py                  桌面软件运行时编排
```

## 真仓订单流程

```text
ScoredSignal
  -> Market Regime / Strategy Router
  -> TradePlan
  -> RiskManager
  -> OrderManager 先落库
  -> BinanceExecutionEngine
  -> Binance
  -> REST 查询或 User Data Stream 回报
  -> StateManager
  -> SQLite 快照、订单事件、交易复盘
  -> 桌面监控
```

网络超时后不会立即重发订单。系统先使用同一个 `clientOrderId` 查询 Binance；仍无法确认时将订单标记为 `UNKNOWN`，并锁定新开仓，避免重复成交。

## 风控顺序

开仓依次检查：

1. 急停状态。
2. API、实时行情、账户同步和当日盈亏数据是否可用。
3. 每日最大亏损和连续亏损次数。
4. 单笔风险、系统最高杠杆。
5. 总风险敞口和单币风险敞口。
6. 原计划强平安全垫、ATR、资金费率和质量评分。

API 或行情异常时禁止开仓；减仓和平仓不受日亏损、评分和敞口限制，但 API 本身不可用时无法发送订单。

## 自动仓位策略

自动交易不再按固定账户比例或固定数量开仓。当前链路是：

```text
Market Data
  -> SignalScorer 0-100分
  -> automation_policy 动态仓位决策
  -> TradePlan 风险金额/止损距离算数量
  -> Risk Review 强平安全垫和资金分池
  -> Auto Trader 分批建仓或持仓管理
```

核心模块：

- `trade_assistant/automation_policy.py`：自动仓位配置、评分档位、ATR波动调整、首仓/加仓阶段、连亏保护、时间止损和生命周期状态。
- `trade_assistant/gui/services.py`：选择信号或自动生成计划时，按动态风险填入风险比例、止损、目标和杠杆。
- `trade_assistant/auto_trader.py`：自动循环统一使用动态风险，不允许绕过评分、波动、资金分池和单币敞口限制。
- `trade_assistant/position_manager.py`：持仓状态机，管理 `INITIAL / PROFIT_HOLD / ADD_POSITION / REDUCE_POSITION / TRAILING / EXIT`。

默认开仓规则：

- 90分以上：目标风险 100%，首仓只执行 40%。
- 80-90分：目标风险 70%，首仓只执行 40%。
- 70-80分：目标风险 40%，首仓只执行 40%。
- 70分以下：不允许自动开仓。

实际仓位仍然由风险金额计算：

```text
风险金额 = 账户权益 * 动态风险比例
下单数量 = 风险金额 / |入场价 - 止损价|
```

因此高波动币会因为 ATR 止损距离变大而自然降低数量，同时 `automation_positioning` 还会再降低风险比例。

持仓管理规则：

- 首次建仓为 `INITIAL/初始试探仓`。
- 只有浮盈达到至少 1R、评分重新达到加仓阈值、趋势仍支持原方向时，才允许顺势加仓。
- 默认禁止亏损加仓；可在配置里显式开启严格小比例补仓，但仍要满足评分、趋势和风险限制。
- 评分明显下降、反向强信号、ATR异常放大或强平安全垫变差时，优先动态减仓。
- 到达配置的 R 倍数后按比例分批止盈，剩余仓位通过 ATR 移动止损跟踪。
- 持仓超过时间止损阈值仍未达到最低 R 收益，且评分不足，会主动退出。

所有参数集中在 `config/settings.json` 的 `automation_positioning` 中，包括单笔最大风险、评分档位、首仓比例、加仓次数、分批止盈比例、ATR倍数、时间止损、单币敞口和连续亏损保护。

## 状态恢复

软件启动会读取 `data/trading_system.db`。开启真实仓或自动真仓后，系统查询 Binance 账户、仓位和活动订单，再与本地状态比较。数量、方向或入场价不同，统一以 Binance 为准，并把差异写入快照和界面日志。

自动交易不会在软件重启后自行恢复真仓执行。界面会显示上次状态，用户仍需重新选择执行方式并启动，这是防止无人确认恢复真实下单的安全边界。

## 保护单

Order Manager 支持 `STOP_MARKET`、`TAKE_PROFIT_MARKET`、`Reduce Only` 和期货 `Post Only (GTX)`。设置页可选择“成交后提交止损和分批止盈单”。该开关默认关闭，启用前应先使用 Binance 测试环境或极小仓位验证账户的持仓模式和条件单接口兼容性。

## 本地存储

- `managed_orders`：订单当前状态。
- `order_events`：提交、查询、成交、失败和不确定状态的完整轨迹。
- `trades`：开仓、分批平仓、盈亏和持有时间。
- `snapshots`：账户与持仓对账快照。
- `risk_events`：每次允许、拒绝和急停原因。
- `system_state`：最新 TradingState、自动状态和保护单状态。

SQLite 是纯本地桌面版的唯一持久化依赖。当前版本不引入 Redis，避免为了单机运行增加额外服务；以后拆分成多进程或多机器时再增加缓存层。

参数优化器仅用于离线研究，需要由回测函数提供交易次数、平均 R 和最大回撤。它不会直接修改实盘配置，也不会在自动交易运行时边交易边调参。
