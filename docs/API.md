# API连接与数据约定

参考[OKX官方v5文档](https://www.okx.com/docs-v5/en/)。只访问公开GET与公开行情WS，无密钥、登录、签名或真实资金接口。

默认REST https://openapi.okx.com，可用QUANT_REST_URL切换允许的https://www.okx.com。
公共WS wss://ws.okx.com/ws/v5/public，订阅tickers、books5、mark-price、funding-rate；
业务WS wss://ws.okx.com/ws/v5/business，订阅candle1m/5m/15m。QUANT_PUBLIC_WS与QUANT_BUSINESS_WS可切换允许的官方8443端点。
HTTP(S)_PROXY遵从httpx/websockets配置，不关闭证书校验。

| 数据 | REST端点 |
|---|---|
| 时钟/合约/逐仓阶梯 | /api/v5/public/time、/api/v5/public/instruments、/api/v5/public/position-tiers |
| 成交价/盘口/当前K线 | /api/v5/market/ticker、/api/v5/market/books、/api/v5/market/candles |
| 标记价/费率 | /api/v5/public/mark-price、/api/v5/public/funding-rate |
| 历史价格/标记K线/实际费率 | /api/v5/market/history-candles、/api/v5/market/history-mark-price-candles、/api/v5/public/funding-rate-history |

REST全局串行间隔≥0.55秒，429/5xx/50011/50040指数退避；其他错误记录并上报。WS20秒无消息发文本ping，10秒无响应重连。
重连重新订阅并对账；后台每分钟对账K线，每小时刷新合约与阶梯。
books5是完整五档快照，不做增量合并或冒充增量CRC校验；检查排序、正数和价差。
K线ts为UTC毫秒开盘时刻，confirm=1才进入策略。未确认更新不能覆盖已确认记录。
vol为张、volCcy为基础币、volCcyQuote为USDT。24h变化=last/open24h−1；预测资金费不当作实际结算费。
历史after向更早时间分页，按ts去重排序并查缺口；下载文件有SHA256清单。长期停机超过最近300根窗口的早期缺口需历史流程补齐。

本机只读API http://127.0.0.1:8765/docs：

| 路由 | 返回 |
|---|---|
| GET /health | 心跳、连接订阅、行情时效、K线缺口；未就绪503 |
| GET /markets | 实时价、24h变化和公开快照 |
| GET /candles/{symbol}/{bar} | 默认确认K线，limit 1–5000 |
| GET /instruments | 合约快照 |
| GET /analysis/{symbol} | 规则状态、实时current_price、独立reference_close、触发/失效和风险 |
| GET /explanation/{symbol} | 同一数据的规则解释 |

没有下单、转账或账户登录路由。模拟账本操作只在本机CLI/终端中执行。端口不默认暴露到局域网。
