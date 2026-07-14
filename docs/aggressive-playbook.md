# 激进线选币与仓位管理

## 目标

激进线不是放弃风控，而是在趋势和成交确认充分时，把完整风险预算用于少数赔率较高的机会；连续亏损后则自动切换到恢复试探模式。

## 选币规则

1. 仅真仓 `trend_following` 与 `breakout`，避免在震荡区高频均值回归付出手续费。
2. 正常激进门槛：评分至少 68、成交量至少近期均量的 0.85 倍；多周期冲突、BTC/ETH 环境反向、主动成交背离和远离保护结构仍拒绝。
3. 连续亏损达到 5 次：不再使用普通激进门槛，只允许评分至少 82、量能至少 1.05 倍的趋势/突破候选；与连续亏损数量风控叠加后，实际风险约为正常首仓的 25%。
4. 连续亏损达到 8 次或日亏损上限：停止新开仓，平仓和风控操作仍可执行。

## 仓位规则

1. 首仓为目标风险的 60%，先走出 1R 并把止损移至保本；价格继续达到至少 1.1R 且同向评分至少 78 后，再按 25%、15% 顺势加仓。
2. 禁止默认亏损补仓和马丁格尔。
3. 1R 先移到保本；1.75R 减仓 20%；3R 再减仓 25%；剩余仓位采用 2.5 ATR 跟踪止损。
4. 18 小时未达到 0.25R 且评分不足 72，执行时间退出。强平安全垫、反向高分信号与确认后的止损仍优先处理。

## 研究依据

- Hurst, Ooi, Pedersen, *A Century of Evidence on Trend-Following Investing* (2017): https://fairmodel.econ.yale.edu/ec439/hurst.pdf
- Moreira and Muir, *Volatility Managed Portfolios*, NBER Working Paper 22208: https://www.nber.org/papers/w22208
- Lopez de Prado, *The Volume Clock: Insights into the High Frequency Paradigm*, DOI 10.3905/jpm.2012.39.1.019

这些研究支持趋势跟随与波动缩放的组合，不构成收益保证。任何参数变更仍应在模拟、影子记录和样本外验证后持续复核。
