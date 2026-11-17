import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from quant.analysis import analyze_market
from quant.backtest import Assumptions, run_report
from quant.collector import market_overview
from quant.config import BAR_MS, Settings, load_settings
from quant.explain import explain_signal, review_backtest, review_trades
from quant.history import download
from quant.indicators import indicators
from quant.llm import explain_with_model
from quant.notifications import native_toast
from quant.okx import PublicREST
from quant.paper import PaperAccount, encode, fresh_market
from quant.paper_service import market_inputs, reconcile_funding
from quant.preferences import save_settings
from quant.risk import Order, Policy, assess, pnl
from quant.store import Store
from quant.strategy import StrategyParameters

PAGES = ["市场总览", "行情与指标", "AI分析", "交易模拟", "策略回测", "风险管理", "系统设置"]


def signal_for(store, settings, symbol):
    return analyze_market(store, settings, symbol)


def health_banner(store):
    status = store.statuses().get("collector", {})
    now = int(time.time() * 1000)
    alive = 0 <= now - status.get("time_ms", 0) < 10_000
    if alive and status.get("ready"):
        st.success("OKX公开行情在线 · 仅模拟资金 · 正式信号仅使用已完成K线")
    else:
        st.warning(
            "采集器未就绪或行情过期。可查看历史数据；模拟执行会拒绝过期行情。请启动完整终端或start-market.ps1。"
        )
    st.caption(f"本机时间：{datetime.now(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M:%S} 北京时间")


@st.fragment(run_every=5)
def overview(settings):
    store = Store(settings.database)
    try:
        health_banner(store)
        rows = []
        for market in market_overview(store, settings):
            signal = signal_for(store, settings, market["symbol"])
            ticker = market["snapshots"].get("tickers", {})
            age = (time.time() * 1000 - ticker.get("exchange_ts", 0)) / 1000 if ticker else None
            rows.append(
                {
                    "币种": market["symbol"],
                    "成交价": market["last"],
                    "24h变化%": round(float(market["change_24h_pct"]), 2)
                    if market["change_24h_pct"] is not None
                    else None,
                    "趋势": signal["trend"],
                    "规则状态": signal["status"],
                    "数据年龄秒": round(age, 1) if age is not None else None,
                    "风险": "高波动"
                    if signal.get("observations", {}).get("high_volatility")
                    else "20倍杠杆高风险",
                }
            )
        for col, row in zip(st.columns(len(rows)), rows):
            col.metric(
                row["币种"].split("-")[0],
                row["成交价"] or "无行情",
                f"{float(row['24h变化%']):+.2f}%" if row["24h变化%"] is not None else None,
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("价格与数据年龄来自OKX；趋势、候选和风险来自独立规则引擎。数据过期不会产生新候选。")
    finally:
        store.close()


@st.fragment(run_every=5)
def market_chart(settings, symbol, bar):
    store = Store(settings.database)
    try:
        rows = store.candles(symbol, bar, 300)
        if not rows:
            st.info("尚无该周期行情。启动采集器后将显示真实K线。")
            return
        frame = indicators(rows, as_of_ms=int(time.time() * 1000))
        if frame.empty:
            st.warning("尚无已完成K线。")
            return
        frame["time"] = pd.to_datetime(frame.ts, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
        fig = make_subplots(
            rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.025, row_heights=[0.55, 0.15, 0.15, 0.15]
        )
        fig.add_trace(
            go.Candlestick(
                x=frame.time,
                open=frame.open,
                high=frame.high,
                low=frame.low,
                close=frame.close,
                name="确认K线",
                increasing_line_color="#2ee6b7",
                decreasing_line_color="#fa6b87",
            ),
            row=1,
            col=1,
        )
        forming = [c for c in rows if not c.confirmed]
        if forming:
            fig.add_trace(
                go.Candlestick(
                    x=pd.to_datetime([c.ts for c in forming], unit="ms", utc=True).tz_convert(
                        "Asia/Shanghai"
                    ),
                    open=[float(c.open) for c in forming],
                    high=[float(c.high) for c in forming],
                    low=[float(c.low) for c in forming],
                    close=[float(c.close) for c in forming],
                    name="未收盘（仅展示）",
                    opacity=0.45,
                ),
                row=1,
                col=1,
            )
        for key in (
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "ema9",
            "ema21",
            "bb_upper",
            "bb_lower",
            "vwap",
            "support",
            "resistance",
        ):
            fig.add_trace(
                go.Scatter(
                    x=frame.time,
                    y=frame[key],
                    name=key,
                    mode="lines",
                    line_width=1,
                    visible=True if key in ("ema9", "ema21", "bb_upper", "bb_lower") else "legendonly",
                ),
                row=1,
                col=1,
            )
        fig.add_trace(
            go.Bar(x=frame.time, y=frame.volume, name="成交张数", marker_color="#527fac"), row=2, col=1
        )
        fig.add_trace(
            go.Scatter(x=frame.time, y=frame.rsi14, name="RSI14", line_color="#bb9aff"), row=3, col=1
        )
        fig.add_hline(y=70, row=3, col=1, line_dash="dot")
        fig.add_hline(y=30, row=3, col=1, line_dash="dot")
        for key in ("macd", "macd_signal", "macd_hist"):
            fig.add_trace(go.Scatter(x=frame.time, y=frame[key], name=key), row=4, col=1)
        fig.update_layout(
            template="plotly_dark",
            height=780,
            margin=dict(l=10, r=10, t=25, b=10),
            xaxis_rangeslider_visible=False,
            paper_bgcolor="#090f1c",
            plot_bgcolor="#090f1c",
        )
        st.plotly_chart(fig, width="stretch")
        age = (time.time() * 1000 - rows[-1].ts - BAR_MS[bar]) / 1000
        st.caption(
            f"{symbol} · {bar} · 北京时间 · 最新存储K线确认={rows[-1].confirmed} · VWAP为载入窗口内日锚定值"
        )
        if age > BAR_MS[bar] / 1000:
            st.warning("此图为过期的历史行情。")
        st.dataframe(frame.tail(8).drop(columns="time"), hide_index=True, width="stretch")
        snapshots = store.snapshots(symbol)
        st.json(
            {k: v for k, v in snapshots.items() if k in ("mark-price", "funding-rate", "books5")},
            expanded=False,
        )
    finally:
        store.close()


def ai_page(settings, symbol):
    store = Store(settings.database)
    try:
        signal = signal_for(store, settings, symbol)
        st.subheader(f"{symbol} · {signal['status']}")
        st.write(explain_signal(signal)["text"])
        st.json(signal, expanded=False)
        if st.button(
            "调用兼容模型整理解释", help="需设置QUANT_LLM_BASE_URL、QUANT_LLM_MODEL；未部署时本地回退"
        ):
            with st.spinner("模型仅整理已验证事实，不参与下单…"):
                st.session_state.model_explanation = explain_with_model(signal)
        result = st.session_state.get("model_explanation")
        if result and result["symbol"] == symbol:
            st.info(f"解释引擎：{result['engine']} · 状态：{result.get('llm_status', 'local')}")
            st.caption(f"解释快照UTC毫秒：{result['as_of_ms']}")
            st.write(result["text"])
        report_path = Path("reports/backtest.json")
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if symbol in report["results"]:
                with st.expander("真实历史策略表现复盘"):
                    st.write("样本内：" + review_backtest(report["results"][symbol]["in_sample"]))
                    st.write("样本外：" + review_backtest(report["results"][symbol]["out_of_sample"]))
    finally:
        store.close()


def quote_account(account, state, order, markets, now):
    equity = account.valuation(state, markets, now)
    account.roll_day(state, equity, now)
    policy = Policy(**state["policy"])
    instrument, tiers = fresh_market(markets[order.symbol], now)
    return assess(
        order,
        instrument,
        tiers,
        markets[order.symbol]["book"],
        policy,
        equity=max(D(0), equity),
        free_cash=state["cash"],
        daily_budget=max(D(0), D(state["day_start"]) * policy.daily_limit - D(state["daily_loss"])),
        reserved=sum(
            (max(D(0), D(p["planned_loss"]) - D(p["entry_fee"])) for p in state["positions"].values()), D(0)
        ),
    )


def paper_page(settings, symbol):
    account, store = PaperAccount("data/paper.sqlite3"), Store(settings.database)
    try:
        try:
            state = account.read()
        except ValueError:
            st.info("独立虚拟账户尚未初始化。100元人民币需按你选定的真实换算率配置。")
            with st.form("paper_initialize"):
                cny = st.number_input("初始人民币", min_value=1.0, value=100.0)
                rate = st.text_input("人民币每USDT", placeholder="输入换算率")
                source = st.text_input("换算率来源", placeholder="来源说明及观察时间")
                rate_time = st.text_input("汇率观察时间（UTC毫秒）", value=str(int(time.time() * 1000)))
                if st.form_submit_button("初始化虚拟账户"):
                    account.initialize(
                        cny=str(cny),
                        cny_per_usdt=rate,
                        rate_source=source,
                        rate_time_ms=int(rate_time),
                        now=int(time.time() * 1000),
                        policy=Policy(**settings.risk),
                    )
                    st.rerun()
            return
        st.success("PAPER ONLY · 本地虚拟资金账户")
        columns = st.columns(3)
        columns[0].metric("可用虚拟USDT", f"{D(state['cash']):.6f}")
        columns[1].metric("累计已使用日损失预算", f"{D(state['daily_loss']):.6f}")
        try:
            markets = market_inputs(store, list(state["positions"]))
            equity = account.valuation(state, markets, int(time.time() * 1000))
            columns[2].metric("标记价权益USDT", f"{equity:.6f}")
        except (ValueError, KeyError) as exc:
            columns[2].metric("标记价权益", "无法估值")
            st.warning(str(exc))
        st.caption(f"换算记录：{state['conversion']}；交易日：{state['day']}（北京时间）")
        worker = store.statuses().get("paper_worker", {})
        if time.time() * 1000 - worker.get("time_ms", 0) > 10_000 or not worker.get("ok"):
            st.warning("模拟监控进程未就绪。自动止盈止损需要start-paper.ps1或完整终端启动器。")
        st.dataframe(pd.DataFrame(state["positions"].values()), hide_index=True, width="stretch")
        with st.form("paper_order"):
            side = st.selectbox("模拟方向", ["long", "short"])
            quantity = st.text_input("合约张数", value="", placeholder="须满足合约最小张数与步长")
            stop = st.text_input("止损价格")
            target = st.text_input("止盈价格")
            preview, submit = st.columns(2)
            quote_clicked = preview.form_submit_button("预算预览")
            open_clicked = submit.form_submit_button("提交模拟开仓")
            if quote_clicked or open_clicked:
                order = Order(symbol=symbol, side=side, quantity=quantity, stop=stop, target=target)
                markets = market_inputs(store, sorted(set(state["positions"]) | {symbol}))
                now = int(time.time() * 1000)
                if open_clicked:
                    result = account.open(order, markets, now=now, request_id=str(uuid4()))
                    st.success("模拟开仓已成交（无真实订单）")
                else:
                    result = quote_account(account, state, order, markets, now)
                st.json(json.loads(encode(result)))
        if state["positions"]:
            selected = st.selectbox("待平仓模拟持仓", list(state["positions"]))
            if st.button("模拟平仓"):

                async def settle():
                    rest = PublicREST(settings)
                    try:
                        await reconcile_funding(
                            account,
                            market_inputs(store, list(state["positions"])),
                            rest,
                            int(time.time() * 1000),
                        )
                    finally:
                        await rest.close()

                asyncio.run(settle())
                trade = account.close_position(
                    selected, market_inputs(store, list(state["positions"])), now=int(time.time() * 1000)
                )
                st.success("模拟平仓已成交")
                st.json(trade)
        journal = account.history(100000)
        st.subheader("模拟复盘")
        st.json(review_trades(journal))
        st.dataframe(
            pd.DataFrame(
                [
                    {"时间": r["ts"], "事件": r["kind"], "详情": json.dumps(r["data"], ensure_ascii=False)}
                    for r in journal[:200]
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.download_button("导出模拟流水JSON", encode(journal), "paper-journal.json", "application/json")
    finally:
        account.close()
        store.close()


def backtest_page(settings):
    st.caption("真实历史数据 · 固定参数按时间拆分70%样本内 / 30%样本外 · 各币种独立账户")
    with st.expander("下载与运行回测", expanded=not Path("reports/backtest.json").exists()):
        days = st.number_input("评估天数（另加2天预热）", min_value=1, max_value=60, value=7)
        fx = st.text_input("回测人民币每USDT（明确假设情景）")
        spread = st.number_input(
            "单边盘口总点差率假设", min_value=0.0, max_value=0.01, value=0.0002, format="%.5f"
        )
        participation = st.number_input(
            "前一分钟成交量参与上限", min_value=0.001, max_value=0.05, value=0.01, format="%.3f"
        )
        if st.button("下载OKX真实历史数据"):
            end = int(time.time() * 1000) // 86_400_000 * 86_400_000
            with st.spinner("下载价格、标记价与实际资金费，可能需要数分钟…"):
                asyncio.run(
                    download(
                        settings, start=end - (int(days) + 2) * 86_400_000, end=end, directory="data/history"
                    )
                )
            st.success("历史数据下载并检查完成")
        if st.button("运行固定参数样本内外回测"):
            if not fx:
                st.error("请填写明确的换算情景；不虚构历史汇率。")
            else:
                with st.spinner("计算回测与成本，参数不做自动寻优…"):
                    run_report(
                        "data/history",
                        "reports",
                        cny_per_usdt=fx,
                        parameters=StrategyParameters(**settings.strategy),
                        policy=Policy(**settings.risk),
                        assumptions=Assumptions(
                            spread_rate=str(spread), volume_participation=str(participation)
                        ),
                    )
                st.success("回测报告已保存")
    path = Path("reports/backtest.json")
    if not path.exists():
        st.info("尚无回测报告。请下载真实数据后运行。")
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    symbol = st.selectbox("回测报告币种", list(report["results"]))
    sample = st.radio(
        "评估区间",
        ["in_sample", "out_of_sample"],
        format_func=lambda x: "样本内" if x == "in_sample" else "样本外",
        horizontal=True,
    )
    result = report["results"][symbol][sample]
    m = result["metrics"]
    cols = st.columns(4)
    cols[0].metric("评估交易数", m["trades"])
    cols[1].metric("净损益USDT", f"{D(m['net_pnl']):+.6f}")
    cols[2].metric("胜率", f"{m['win_rate']:.1%}" if m["win_rate"] is not None else "无成交")
    cols[3].metric("最大回撤", f"{D(m['max_drawdown_pct']):.2f}%")
    with st.expander("完整统计指标"):
        st.json(m)
    curve = pd.DataFrame(result["equity_curve"])
    curve["time"] = pd.to_datetime(curve.ts, unit="ms", utc=True)
    fig = go.Figure(
        go.Scatter(x=curve.time, y=curve.equity.astype(float), name="标记价权益USDT", fill="tozeroy")
    )
    fig.update_layout(template="plotly_dark", height=380, xaxis_title="UTC", yaxis_title="独立模拟账户USDT")
    st.plotly_chart(fig, width="stretch")
    st.write("回测成交记录")
    if result["trades"]:
        st.dataframe(pd.DataFrame(result["trades"]), hide_index=True, width="stretch")
    else:
        st.info("此区间没有通过规则与风险审核的成交，报告保留零交易结果。")
    with st.expander("成本、拒绝原因及数据假设"):
        st.json(
            {
                "fx": report["fx"],
                "parameters": report["parameters"],
                "policy": report["policy"],
                "assumptions": report["execution_assumptions"],
                "rejections": result["rejections"],
                "limitations": report["limitations"],
            }
        )
    st.download_button("下载完整回测JSON", path.read_bytes(), "backtest.json", "application/json")
    if Path("reports/BACKTEST.md").exists():
        st.download_button(
            "下载回测报告", Path("reports/BACKTEST.md").read_bytes(), "BACKTEST.md", "text/markdown"
        )


def risk_page(settings, symbol):
    st.caption("20倍为参考杠杆；计划止损不保证实际损失上限。按OKX合约乘数、最小张数与阶梯规则估算。")
    store = Store(settings.database)
    try:
        cny = st.number_input("参考本金人民币", min_value=1.0, value=100.0)
        fx = st.text_input("换算情景：人民币每USDT", key="risk_fx")
        quantity = st.text_input("测算合约张数", key="risk_qty")
        stop, target = st.columns(2)
        stop_value = stop.text_input("计划止损", key="risk_stop")
        target_value = target.text_input("计划止盈", key="risk_target")
        side = st.selectbox("测算方向", ["long", "short"], key="risk_side")
        if st.button("计算仓位与风险"):
            capital = D(str(cny)) / D(fx)
            market = market_inputs(store, [symbol])[symbol]
            inst, tiers = fresh_market(market, int(time.time() * 1000))
            policy = Policy(**settings.risk)
            order = Order(symbol=symbol, side=side, quantity=quantity, stop=stop_value, target=target_value)
            quote = assess(
                order,
                inst,
                tiers,
                market["book"],
                policy,
                equity=capital,
                free_cash=capital,
                daily_budget=capital * policy.daily_limit,
            )
            st.json(json.loads(encode(quote)))
            deltas = [D(str(v)) for v in (-2, -1, -0.5, 0, 0.5, 1, 2)]
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "价格变化%": str(v),
                            "毛损益USDT": str(
                                pnl(
                                    inst, order.quantity, quote["entry"], quote["entry"] * (1 + v / 100), side
                                )
                            ),
                        }
                        for v in deltas
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        st.json(
            {k: v for k, v in store.statuses().items() if k in (f"instrument:{symbol}", f"tiers:{symbol}")},
            expanded=False,
        )
    finally:
        store.close()


def settings_page(settings):
    st.write(
        "配置修改会校验并持久保存。采集与后台进程需重启后加载新配置；已创建模拟账户保留其初始化风控参数。"
    )
    with st.form("settings_form"):
        symbols = st.multiselect(
            "监控合约", [f"{s}-USDT-SWAP" for s in ("BTC", "ETH", "SOL", "UNI")], default=settings.symbols
        )
        bars = st.multiselect("采集周期", list(BAR_MS), default=settings.bars)
        st.caption("正式多周期策略需要1m、5m、15m齐备；移除周期后策略可能持续暂不交易。")
        strategy = st.text_area(
            "策略参数JSON",
            json.dumps(StrategyParameters(**settings.strategy).model_dump(), indent=2),
            height=240,
        )
        risk = st.text_area(
            "风险参数JSON", json.dumps(Policy(**settings.risk).model_dump(mode="json"), indent=2), height=220
        )
        notification = st.text_area("通知参数JSON", json.dumps(settings.notifications, indent=2), height=160)
        if st.form_submit_button("校验并保存配置"):
            values = settings.model_dump(mode="json")
            values.update(
                symbols=symbols,
                bars=bars,
                strategy=json.loads(strategy),
                risk=json.loads(risk),
                notifications=json.loads(notification),
            )
            save_settings(Settings(**values))
            st.success("配置已保存。重启完整终端后应用到后台进程。")
    st.caption(
        "兼容模型仅通过环境变量设置QUANT_LLM_BASE_URL、QUANT_LLM_MODEL、QUANT_LLM_API_KEY；界面不保存或显示密钥。"
    )
    if st.button("发送Windows系统测试通知"):
        native_toast("AI Quant 系统测试", "这是系统通知测试，不代表交易机会。")
        st.success("Windows通知API已接受请求。")


def main():
    st.set_page_config(page_title="AI Quant Trader Pro", page_icon="📊", layout="wide")
    st.title("AI Quant Trader Pro")
    st.caption("OKX USDT 永续 · 多周期研究终端 · 真实行情 / 虚拟资金")
    settings = load_settings()
    page = st.sidebar.radio("功能导航", PAGES, key="page")
    symbol = st.sidebar.selectbox("交易合约", settings.symbols, key="symbol")
    bar = st.sidebar.selectbox("K线周期", settings.bars, key="bar")
    st.sidebar.caption("第一版无真实下单接口。模型不能控制资金。")
    st.sidebar.button("刷新当前页面")
    try:
        if page == PAGES[0]:
            overview(settings)
        elif page == PAGES[1]:
            market_chart(settings, symbol, bar)
        elif page == PAGES[2]:
            ai_page(settings, symbol)
        elif page == PAGES[3]:
            paper_page(settings, symbol)
        elif page == PAGES[4]:
            backtest_page(settings)
        elif page == PAGES[5]:
            risk_page(settings, symbol)
        else:
            settings_page(settings)
    except (
        ValueError,
        KeyError,
        ArithmeticError,
        OSError,
        RuntimeError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        logging.getLogger(__name__).warning("UI operation refused or failed: %s", exc)
        st.error(f"操作未完成：{exc}")
        st.caption("未替换为演示数据；请修正输入或恢复真实数据连接后重试。")
