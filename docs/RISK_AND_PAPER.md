# 风险计算与隔离模拟账户

本模块仅操作本机SQLite虚拟账本，无登录、签名或真实订单接口。行情库为data/market.sqlite3，默认模拟账本为data/paper.sqlite3。
模拟账户初始化需要明确的人民币/USDT换算率、来源和时间，金额=100元/该换算率。未提供时拒绝初始化。
程序不会查询真实余额，也不会把USDT与美元或人民币按固定值当作实时汇率。

## 已实现规则

- 全部仓位、费用、损益与余额使用Decimal。基础币数量=张数×ctVal×ctMult；按lotSz/minSz/maxMktSz验证数量，止损止盈按tickSz对齐。
- 默认20倍、单次计划损失最多当前权益2%、每日损失预算最多日初权益5%。交易日按北京时间。预算比例只能调低。
- 计划损失包含止损价差、入场实际盘口/滑点、出场假设点差/滑点、双边费用和预留资金费；同时预留所有在持仓位的计划风险。
- 每日已使用损失按费用、负向已实现毛损失及支付资金费保守累计，不用当日盈利抵销。达到预算停止新开仓，允许退出。
- 每笔开仓前复核最低张数、可用保证金与费用、净收益风险比、当前资金费与预留、规则及行情时效。失败即放弃交易。
- 模拟V1仅允许官方第一仓位阶梯，以匹配小额资金用途。更大阶梯拒绝开仓，尚未实现交易所逐级减仓流程，不能以完全平仓冒充。
- 入场与出场逐档读取books5真实可见张数，买入取卖盘、卖出取买盘，加不利滑点并按tick取整。采用全部成交或全部拒绝；没有足够深度不会外推流动性。
- 每个币种最多一个逐仓持仓，支持多/空、手动平仓、自动止盈止损、资金费、强平模拟、持仓恢复与持久交易记录。
- 开仓请求ID用于去重。账本写入和流水在同一BEGIN IMMEDIATE事务提交，错误回滚，已存在账户不允许隐式重置。

## 官方规则与估算范围

依据[OKX逐仓规则](https://www.okx.com/en-gb/help/vii-introduction-to-the-isolated-mode-of-single-multi-currency-portfolio-margin)，
当逐仓保证金加未实现损益不再覆盖维持保证金及相关费用时，模型触发强平。输入来自[公开仓位阶梯API](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-position-tiers)。
模型以基础币数量B、入场价E、逐仓余额M、该仓位阶梯维持率r及费率f求解：

```
多仓 P = (B*E-M)/(B*(1-r-f))
空仓 P = (B*E+M)/(B*(1+r+f))
```

该值明确标为估算，只适用于本地单向逐仓USDT线性模型。资金费支付会改变M并重新计算；触发判断使用标记价，止盈止损使用最新成交价。
模拟强平优先于止盈止损，平仓仍受可见盘口深度约束，另计该第一阶梯维持保证金对应的清算费用。
模型为隔离账户设置负余额缺口记录，不让该缺口自动侵占其他逐仓保证金；流水同时保留交易经济损益与模拟账户实际损益。
实际OKX费用、账户模式、交易所处置及最终成交可能不同，模型不能保证预测真实账户强平价格。

资金费使用历史接口realizedRate，绝不把预测fundingRate当实际结算。结算时间的标记价采用该分钟历史标记K线开盘价，
是采样近似，流水记录来源。缺少已发布的实际费率或该标记K线时等待数据，阻止在费用尚未对账时退出。
资金费流水按持仓与结算时间去重。长期离线超过官方历史保留期限时需先处理缺失数据，不会伪造零费用。

## 使用

先启动start-market.ps1保持行情和规则更新。初始化命令的三个换算参数必须填写实际选定值：

```powershell
.\.venv\Scripts\python.exe -m quant.paper_cli init --cny-per-usdt <人民币每USDT> --rate-source <来源说明> --rate-time-ms <观察时间UTC毫秒>
.\.venv\Scripts\python.exe -m quant.paper_cli status
.\.venv\Scripts\python.exe -m quant.paper_cli quote --symbol SOL-USDT-SWAP --side long --quantity <张数> --stop <价格> --target <价格>
.\.venv\Scripts\python.exe -m quant.paper_cli open --symbol SOL-USDT-SWAP --side long --quantity <张数> --stop <价格> --target <价格> --request-id <唯一编号>
powershell -ExecutionPolicy Bypass -File .\start-paper.ps1
.\.venv\Scripts\python.exe -m quant.paper_cli close --symbol SOL-USDT-SWAP
.\.venv\Scripts\python.exe -m quant.paper_cli history
```

尖括号参数是必填用户输入，不是可直接复制的市场价格。quote是金额预算预览，open还会核验当前资金费和账户状态，可能拒绝。
策略候选与手动模拟开仓独立，手动测试允许检验策略外的假设，不会将其记成规则引擎产生的信号。

## 运行边界

自动退出要求行情采集器和paper watch进程都在运行。暂停、休眠或断网期间不会虚构成交，恢复后只根据实际观察到的当前行情处理，
无法还原停机期间的逐笔触价路径。books5不含完整队列或隐藏深度，当前滑点/手续费是明确的配置假设。
单次计划损失不是保证上限，价格跳空、资金费变化和流动性不足可能让实际损失超过计划。
行情超过10秒、合约/阶梯超过24小时则停止模拟执行；规则在初始化获取，后台每小时自动刷新，刷新失败不会伪造新规则。
Streamlit交易模拟与风险管理页已接入同一模块；上列CLI仍可独立使用。当前为整笔仓位管理，不开放高阶梯逐级减仓。
