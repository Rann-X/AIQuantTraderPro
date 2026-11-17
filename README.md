# AI Quant Trader Pro 1.0

Windows / Python 3.11+ 的OKX短线合约研究与模拟终端。Streamlit + Plotly深色七页面，FastAPI本机只读接口。
本版没有真实下单、账户登录、私钥签名或真实资金传输代码，只使用公开行情API。

## Windows快速开始

1. 安装[Python 3.11+](https://www.python.org/downloads/windows/)并启用PATH。本次完整验收为Windows x64 / Python 3.14.3。
2. 将发行ZIP完整解压到可写目录，双击`install.bat`安装依赖并运行测试，需要联网访问Python包索引。
3. 双击`start.bat`，打开[本机终端](http://127.0.0.1:8501)。首次行情初始化约30秒。
4. 停止时运行`stop.ps1`。关闭浏览器不会停止行情与模拟监控。

PowerShell等价命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop.ps1
```

这是Windows源码安装版，需Python；不是独立EXE。包含完整源码、脚本、配置、测试、真实历史数据和报告，不携带用户账本或密钥。
启动器管理行情、API、模拟监控、解释通知和终端五个进程，仅监听127.0.0.1:8501与8765，遇到端口占用不会结束其他软件。

## 文档与报告

- [完整操作手册](docs/USER_MANUAL.md)
- [API连接与数据约定](docs/API.md)
- [策略和指标定义](docs/STRATEGY.md)
- [风险控制与模拟交易](docs/RISK_AND_PAPER.md)
- [本地/兼容模型解释与通知](docs/ASSISTANT.md)
- [真实回测报告](reports/BACKTEST.md)
- [六阶段验收](docs/PLAN.md)
- [验证结果与限制](reports/ACCEPTANCE.md)
- [后续扩展建议](docs/EXTENSIONS.md)

模拟账户首次需填写人民币每USDT换算率、来源和观察时间，不假定实时汇率。重启或安装不会重置账户。
用户选择的兼容API模型尚未部署：本地事实解释可直接使用；部署后配置环境变量即可接入，密钥不写配置文件。
第一版限制第一保证金阶梯逐仓；资金费和强平、历史OHLC成交均有明确模型边界，详见风险与回测文档。

## 开发检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check quant tests app.py
.\.venv\Scripts\python.exe -m quant.cli probe --seconds 75
.\.venv\Scripts\python.exe -m quant.history --days 7 --warmup-days 2
.\.venv\Scripts\python.exe -m quant.backtest --cny-per-usdt 7
```

最后一个命令中的7是明确的回测情景假设，不是实时或历史汇率。不会为了生成交易记录放宽规则。
