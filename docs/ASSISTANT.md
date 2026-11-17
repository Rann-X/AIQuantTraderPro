# 行情解释与通知

`python -m quant.assistant_cli explain`读取实际确认K线，输出趋势、指标冲突、成交量/波动风险、触发及失效条件。
缺数据或过期显示暂不交易，不创造参考价格。只读接口为`/explanation/{symbol}`。
默认engine=grounded_rules。已实现兼容Chat Completions API解释组织层；用户尚未部署模型，因此默认本地回退。
设置QUANT_LLM_BASE_URL（含/v1）、QUANT_LLM_MODEL及可选的QUANT_LLM_API_KEY环境变量后，可用AI页按钮或assistant_cli explain --llm调用。
兼容接口使用结构化事实ID，未知事实/自由编造的数字不会渲染；支持json_schema，兼容服务不支持时尝试json_object后仍做本地校验。
模型只能选择和排序本地事实，所有风险文字都保留，没有工具或订单能力。远程API需HTTPS，本机允许HTTP；密钥仅从环境变量加载。
已验证请求格式、事实校验、拒绝及回退；没有部署真实模型，未声称已完成某个特定模型的线上推理验证。

`python -m quant.assistant_cli review --paper-database data/paper.sqlite3`分析实际账本已平仓交易，
无记录时胜率为空，不虚构盈利。输出净损益、费用、资金费及样本限制；不将手工模拟成交当作策略业绩。

运行`start-assistant.ps1`持续分析并提醒。通知仅发送有效期内的规则候选，包含币种、方向、当前实时价、收盘参考价、触发条件、止盈止损、风险等级和北京时间。
通知不等于预算审核通过，明确提示20倍参考杠杆风险。默认同币种300秒冷却、全局10秒最小间隔、10秒分析一次，config.toml的notifications节可调。
SQLite持久去重防止重启重复提醒。Windows通知API失败不会将通知记成已发送，下一分析轮次可重试。

`python -m quant.assistant_cli test-toast`会发送“系统测试，不代表交易机会”的通知。
Windows可能因勿扰模式、通知权限或系统策略不显示横幅；API返回成功只表明已接受通知请求。
采集与分析进程必须持续运行。当前通知只写到用户本机Windows，不调用短信/邮件/聊天服务。
