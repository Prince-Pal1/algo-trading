from fpdf import FPDF
from datetime import date

class PDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(80, 80, 80)
        self.cell(0, 8, "Algo Trading Knowledge Base", align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"Page {self.page_no()} | Last updated: {date.today().strftime('%B %d, %Y')}", align="C")

    def cover(self):
        self.add_page()
        self.ln(30)
        self.set_font("Helvetica", "B", 28)
        self.set_text_color(20, 60, 120)
        self.multi_cell(0, 14, "Algorithmic Trading\nKnowledge Base", align="C")
        self.ln(6)
        self.set_font("Helvetica", "", 14)
        self.set_text_color(80, 80, 80)
        self.cell(0, 10, "Architecture, Technology & Strategy Guide", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)
        self.set_font("Helvetica", "I", 11)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Created: {date.today().strftime('%B %d, %Y')} | Version 1.0", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(20)
        self.set_draw_color(20, 60, 120)
        self.set_line_width(0.8)
        self.line(30, self.get_y(), 180, self.get_y())
        self.ln(10)
        self.set_font("Helvetica", "", 10)
        self.set_text_color(100, 100, 100)
        toc = [
            "1. The Three Tiers of Algo Trading",
            "2. What HFT Firms Actually Use",
            "3. What Quant Hedge Funds Do (That You Can Mimic)",
            "4. How HNWIs Trade",
            "5. The Best Modular Architecture",
            "6. Best Tech Stack Options",
            "7. MetaTrader 5 - Where It Fits",
            "8. Multi-Agent AI Approach (2026)",
            "9. Can You Mimic Big Firms?",
            "10. Recommended Build Order",
            "11. Sources & Further Reading",
        ]
        for item in toc:
            self.cell(0, 7, item, new_x="LMARGIN", new_y="NEXT")

    def section_title(self, text):
        self.ln(5)
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(20, 60, 120)
        self.set_fill_color(235, 242, 255)
        self.cell(0, 9, f"  {text}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def sub_title(self, text):
        self.ln(3)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(40, 40, 120)
        self.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def body(self, text):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 6, text)
        self.ln(1)

    def bullet(self, items):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(40, 40, 40)
        for item in items:
            self.cell(8, 6, chr(149), new_x="RIGHT", new_y="LAST")
            self.multi_cell(182, 6, item)
        self.ln(1)

    def table(self, headers, rows, col_widths=None):
        if col_widths is None:
            col_widths = [190 // len(headers)] * len(headers)
        # Header row
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(20, 60, 120)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, h, border=1, fill=True, align="C")
        self.ln()
        # Data rows
        self.set_font("Helvetica", "", 9)
        fill = False
        for row in rows:
            self.set_fill_color(240, 245, 255) if fill else self.set_fill_color(255, 255, 255)
            self.set_text_color(40, 40, 40)
            for i, cell in enumerate(row):
                self.cell(col_widths[i], 6, cell, border=1, fill=True)
            self.ln()
            fill = not fill
        self.ln(3)

    def code_block(self, text):
        self.set_font("Courier", "", 8)
        self.set_fill_color(245, 245, 245)
        self.set_text_color(30, 30, 30)
        self.set_draw_color(200, 200, 200)
        self.multi_cell(0, 5, text, border=1, fill=True)
        self.set_draw_color(0, 0, 0)
        self.ln(2)


# -- Build PDF -----------------------------------------------------------------
pdf = PDF()
pdf.set_auto_page_break(auto=True, margin=15)
pdf.set_margins(10, 15, 10)

# Cover
pdf.cover()

# -- Section 1 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("1. The Three Tiers of Algo Trading")
pdf.body(
    "Understanding who you are competing with shapes everything about your strategy, "
    "tooling, and realistic expectations. There are three distinct tiers of algorithmic trading, "
    "each with different infrastructure, capital requirements, and achievable edges."
)
pdf.table(
    ["Tier", "Who", "Speed", "Language", "Reachable?"],
    [
        ["Tier 1 - HFT", "Jane Street, Citadel, Jump", "Nanoseconds", "C++, Rust, FPGA", "NO"],
        ["Tier 2 - Quant Funds", "Renaissance, Two Sigma, DE Shaw", "Milliseconds-Seconds", "Python, C++, R", "PARTIALLY"],
        ["Tier 3 - Systematic Retail", "You, prop traders, family offices", "Seconds-Minutes", "Python, MQL5, Pine", "YES"],
    ],
    [35, 55, 35, 42, 23]
)
pdf.body(
    "Key insight: You cannot beat HFT firms on speed. They have custom silicon, microwave "
    "towers between exchanges, and kernel-bypass networking. The winning strategy is to play "
    "a different game - longer timeframes, smarter signals, better risk management. "
    "Your edge is discipline and systematic thinking, not raw speed."
)

# -- Section 2 ----------------------------------------------------------------
pdf.section_title("2. What HFT Firms Actually Use (And Why You Cannot Copy It)")
pdf.table(
    ["Component", "Technology", "Why"],
    [
        ["Hardware", "FPGAs, custom ASICs", "Process orders in nanoseconds on silicon"],
        ["Network", "Microwave towers, fiber colocation", "Physically closer to exchange = faster"],
        ["Language", "C++/Rust, DPDK/RDMA kernel bypass", "No OS overhead - 1-5 microsecond latency"],
        ["Risk Engine", "FPGA-based", "Exposure checks in 50 nanoseconds"],
        ["Data Feed", "Direct Level 2 order book", "Raw feed, no broker middleman"],
        ["Cost", "$10M-$100M+ infrastructure", "This IS the moat"],
    ],
    [35, 65, 90]
)
pdf.body(
    "The real moat is infrastructure, not the algorithm. Renaissance's Medallion Fund "
    "returns 66% annually (before fees) - achieved through decades of proprietary data and "
    "PhD-level mathematics, not faster hardware. Their edge is statistical, not mechanical."
)

# -- Section 3 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("3. What Quant Hedge Funds Do (That You CAN Mimic)")
pdf.body("Firms like Two Sigma, D.E. Shaw, and AQR operate in a zone that is partially reachable:")
pdf.bullet([
    "Trade on statistical edges - mean reversion, momentum, pairs trading",
    "Use alternative data - satellite images, credit card flows, social sentiment",
    "Run ensembles - 50+ uncorrelated strategies, not just one",
    "Strict risk budgets - position sizing is a quantitative science, not gut feel",
    "Backtest ruthlessly - Monte Carlo, walk-forward, out-of-sample validation",
    "Use ML for signal generation, not full autonomy - humans stay in the loop",
])
pdf.sub_title("Their Core Formula")
pdf.code_block("Alpha (edge)  x  Leverage  x  Diversification  x  Risk Control  =  Returns")
pdf.body(
    "You can replicate the logic of this formula with Python, Alpaca, and TradingView data. "
    "The key is not to copy their infrastructure - it's to copy their thinking process: "
    "evidence-based, systematic, diversified, and risk-aware."
)

# -- Section 4 ----------------------------------------------------------------
pdf.section_title("4. How HNWIs (High Net Worth Individuals) Trade")
pdf.body(
    "People with $1M-$50M in trading capital occupy the space between retail and institutional. "
    "Understanding their setup reveals what is achievable with moderate capital."
)
pdf.bullet([
    "Hire a quant or use a family office quant team",
    "Subscribe to institutional data (Bloomberg $2K-$25K/month, Refinitiv similar)",
    "Use QuantConnect or Interactive Brokers with custom Python strategies",
    "Run multi-strategy portfolios: trend following + mean reversion + volatility selling",
    "Allocate 5-20% of total portfolio to systematic strategies",
    "Use risk overlays: hard drawdown limits, correlation monitoring, VaR calculations",
    "Above $5M: dedicated wealth advisors + institutional custody arrangements",
])
pdf.body(
    "Key threshold: above $500K, professional infrastructure costs are justified by returns. "
    "Below that, you build it yourself - which is exactly what we are doing."
)

# -- Section 5 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("5. The Best Modular Architecture (Our Target)")
pdf.body(
    "A modular architecture means each component can be built, tested, and replaced independently. "
    "This is how professional quant shops are built - no monolithic scripts."
)
pdf.code_block(
"""+----------------------------------------------------------+
|                    TRADING SYSTEM                        |
|                                                          |
|  DATA LAYER        -> live prices, OHLCV bars, news       |
|      v                                                   |
|  SIGNAL ENGINE     -> 'should I buy or sell?'             |
|      v                                                   |
|  RISK MANAGER      -> 'how much? what is my stop?'        |
|      v                                                   |
|  EXECUTION         -> place the order (Alpaca / MT5)      |
|      v                                                   |
|  PORTFOLIO TRACKER -> what do I own, current P&L          |
|      v                                                   |
|  SCHEDULER         -> run every N min, market hours only  |
|      v                                                   |
|  LOGGER            -> what happened and why               |
|                                                          |
|  BACKTESTER        -> did this work historically?         |
+----------------------------------------------------------+"""
)
pdf.sub_title("Two Signal Delivery Patterns")
pdf.table(
    ["Pattern", "How It Works", "Best For", "Downside"],
    [
        ["Polling", "Ask TradingView every N min for indicator values, evaluate, trade", "Learning, simple strategies", "Latency, TV must stay open"],
        ["Webhooks", "TV alert fires HTTP POST to local server, server trades instantly", "Production, speed, reliability", "Needs Express server setup"],
    ],
    [25, 75, 50, 40]
)

# -- Section 6 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("6. Best Tech Stack Options (2026)")
pdf.sub_title("Backtesting Frameworks")
pdf.table(
    ["Tool", "Best For", "Status 2026", "Speed"],
    [
        ["QuantConnect (LEAN)", "Full pipeline: research to backtest to live", "Best in class", "Fast"],
        ["VectorBT", "Ultra-fast vectorized strategy testing", "Actively maintained", "Fastest"],
        ["Backtrader", "Simple, quick strategy prototyping", "Aging but works", "Moderate"],
        ["Zipline", "Factor-based equity research", "Maintenance issues", "Moderate"],
    ],
    [45, 65, 42, 28]
)
pdf.body("Recommendation: QuantConnect for full pipeline. VectorBT for rapid iteration and backtesting speed.")

pdf.sub_title("Execution Platforms")
pdf.table(
    ["Platform", "Markets", "Cost", "API Quality"],
    [
        ["Alpaca", "US Stocks, ETFs, Crypto", "Free", "Excellent REST + WebSocket"],
        ["MetaTrader 5", "Forex, Futures, CFDs", "Broker dependent", "MQL5 native, Python bridge"],
        ["Interactive Brokers", "Everything globally", "Commission based", "Industry standard"],
        ["CCXT", "100+ crypto exchanges", "Free library", "Unified interface"],
    ],
    [35, 50, 35, 70]
)

pdf.sub_title("Signal & Intelligence Tools")
pdf.table(
    ["Tool", "What It Adds"],
    [
        ["TradingView MCP", "Chart state, indicator values, Pine Script execution"],
        ["Claude / LLM agents", "News sentiment, earnings analysis, natural language signals"],
        ["pandas-ta / TA-Lib", "150+ technical indicators in Python, vectorized"],
        ["Alpaca News API", "Real-time market news feed, free with account"],
        ["Alternative data", "Satellite imagery, credit card data, social sentiment (paid)"],
    ],
    [50, 140]
)

# -- Section 7 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("7. MetaTrader 5 - Where It Fits")
pdf.body(
    "MT5 is the dominant platform for forex and futures trading. It runs Expert Advisors (EAs) "
    "natively in MQL5 - a compiled, C++-like language that executes directly on the broker's server. "
    "Crucially, MT5 has an official Python bridge, allowing you to keep all strategy logic in Python "
    "while using MT5 purely for execution."
)
pdf.sub_title("MT5 Python Bridge Example")
pdf.code_block(
"""import MetaTrader5 as mt5

mt5.initialize()

# Place a market buy order
mt5.order_send({
    "action":   mt5.TRADE_ACTION_DEAL,
    "symbol":   "EURUSD",
    "volume":   0.1,
    "type":     mt5.ORDER_TYPE_BUY,
    "price":    mt5.symbol_info_tick("EURUSD").ask,
    "sl":       0.0,      # stop loss price
    "tp":       0.0,      # take profit price
    "comment":  "bot entry",
    "magic":    12345,    # strategy ID
})"""
)
pdf.sub_title("MT5 Strengths")
pdf.bullet([
    "24/5 forex and futures market access",
    "Strategy Tester with tick-by-tick backtesting and genetic optimization",
    "Runs EAs server-side - no need to keep your computer on",
    "Access to thousands of brokers globally",
    "Python bridge enables full Python strategy logic with MT5 execution",
])
pdf.sub_title("Best Integration Pattern")
pdf.code_block(
"""Python (your brain)  ->  MT5 Python bridge  ->  MT5 (execution)  ->  Broker
     Signal logic              mt5.order_send()      EA / terminal        Fill"""
)

# -- Section 8 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("8. Multi-Agent AI Approach (Cutting Edge 2026)")
pdf.body(
    "The most advanced retail approach in 2026 is multi-agent LLM systems - literally mimicking "
    "how a real trading desk operates, with specialized agents for each role. "
    "Open-source frameworks like TradingAgents (GitHub: TauricResearch/TradingAgents) implement this pattern."
)
pdf.code_block(
"""+------------------+  +-----------------+  +------------------+
|  Fundamental     |  |  Technical      |  |  Sentiment       |
|  Analyst Agent   |  |  Analyst Agent  |  |  Analyst Agent   |
|  (earnings, P/E) |  |  (RSI, EMA)     |  |  (news, social)  |
+--------+---------+  +--------+--------+  +--------+---------+
         +--------------------- v --------------------+
                         +--------------+
                         | Risk Manager |
                         | Agent        |
                         +------+-------+
                                v
                         +--------------+
                         |  Execution   |
                         |  Agent       |
                         |  (Alpaca/MT5)|
                         +--------------+"""
)
pdf.body(
    "With Claude as the AI backbone and your existing Alpaca + TradingView MCPs, you already "
    "have the infrastructure to build this. Each agent can be a Claude API call with a specific "
    "role and context, coordinated by an orchestrator that makes the final trade decision."
)

# -- Section 9 ----------------------------------------------------------------
pdf.section_title("9. Can You Mimic Big Firms?")
pdf.body("Partially - and that partial overlap is where real profit lives.")
pdf.table(
    ["What They Have", "Your Equivalent"],
    [
        ["Petabytes of historical data", "Alpaca free 5-year history + yfinance + Quandl"],
        ["Colocation at NYSE", "Irrelevant at 5-minute+ timeframes"],
        ["50 PhD quants", "Claude + Python + rigorous strategy logic"],
        ["Bloomberg terminal ($25K/yr)", "TradingView Pro ($60/mo)"],
        ["100 uncorrelated strategies", "Build 5-10 well-backtested ones"],
        ["Dedicated risk systems", "Python risk module + hard rules + monitoring"],
        ["Real-time alternative data", "Free news APIs + LLM sentiment analysis"],
    ],
    [80, 110]
)
pdf.body(
    "Your edge is not speed - it is systematic discipline (no emotions), evidence-based strategy "
    "(backtested), proper risk management (survive drawdowns), and continuous iteration "
    "(improve monthly). These are learnable and buildable. Renaissance makes 66% annually "
    "through better math on better data - not through faster chips."
)

# -- Section 10 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("10. Recommended Build Order")
pdf.sub_title("Phase 1 - Foundation (Current)")
pdf.bullet([
    "Alpaca MCP: execution layer - place, manage, cancel orders (DONE)",
    "TradingView MCP: analysis layer - read indicators, chart state (DONE)",
    "Market data module: fetch OHLCV bars from Alpaca data API",
    "Basic strategy engine: EMA crossover or RSI mean reversion to start",
])
pdf.sub_title("Phase 2 - Risk & Automation")
pdf.bullet([
    "Risk manager: position sizing (% of equity), stop loss, max drawdown halt",
    "Scheduler: run only during market hours (9:30-16:00 ET), cron-based",
    "Trade logger: SQLite database recording every signal, decision, and fill",
])
pdf.sub_title("Phase 3 - Intelligence")
pdf.bullet([
    "Backtester: VectorBT or QuantConnect for historical strategy validation",
    "Sentiment layer: Alpaca news API + Claude LLM for sentiment scoring",
    "MT5 bridge: add forex/futures exposure alongside US equities via Alpaca",
])
pdf.sub_title("Phase 4 - Scale")
pdf.bullet([
    "Multi-strategy portfolio: 5-10 uncorrelated strategies running in parallel",
    "Multi-agent system: Claude agents per analyst role (technical, fundamental, risk)",
    "Performance dashboard: live P&L, Sharpe ratio, drawdown, win rate",
    "Walk-forward optimization: monthly strategy parameter review",
])

# -- Section 11 ----------------------------------------------------------------
pdf.add_page()
pdf.section_title("11. Sources & Further Reading")
sources = [
    ("QuantVPS - Top 20 Trading Bot Strategies 2026", "https://www.quantvps.com/blog/trading-bot-strategies"),
    ("QuantVPS - HFT Platform Architecture 2026", "https://www.quantvps.com/blog/high-frequency-trading-platform"),
    ("QuantVPS - Top 12 Quant Trading Firms 2026", "https://www.quantvps.com/blog/top-quant-trading-firms"),
    ("Medium - Hedge Fund Style Quant System in Python", "https://medium.com/algorithmic-and-quantitative-trading/how-to-build-a-hedge-fund-style-quant-trading-system-in-python-da5e916980d2"),
    ("Arkalogi - Retail vs Institutional Tech Stack", "https://arkalogi.com/blogs/what-software-technology-need-fast-algo-trading"),
    ("MetaTrader 5 - Algorithmic Trading Overview", "https://www.metatrader5.com/en/automated-trading"),
    ("GitHub - TradingAgents Multi-Agent Framework", "https://github.com/TauricResearch/TradingAgents"),
    ("Python Financial - Backtesting Landscape 2026", "https://python.financial/"),
    ("Medium - AI Trading Bot with RL for MT5 (2026)", "https://medium.com/@jsgastoniriartecabrera/building-an-ai-trading-bot-from-reinforcement-learning-theory-to-metatrader-5-implementation-acb3241bf6c9"),
    ("SaintQuant - How to Build a Profitable Bot 2026", "https://saintquant.com/blog/161-how-to-build-a-profitable-crypto-trading-bot-in-2026-a-quantitative-guide-for-algorithmic-traders"),
    ("Power Trading Group - Retail Algo Trading 2026", "https://www.powertrading.group/options-trading-blog/algorithmic-trading-retail-traders-2026"),
    ("QuantConnect Review 2026", "https://newyorkcityservers.com/blog/quantconnect-review"),
]
pdf.set_font("Helvetica", "", 10)
pdf.set_text_color(40, 40, 40)
for title, url in sources:
    pdf.cell(8, 7, chr(149), new_x="RIGHT", new_y="LAST")
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(20, 80, 180)
    pdf.cell(8, 6, "", new_x="RIGHT", new_y="LAST")
    pdf.cell(0, 6, url, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(40, 40, 40)
    pdf.ln(1)

# -- Save ----------------------------------------------------------------------
out_path = "/Users/prince/algo-trading/Algo_Trading_Knowledge_Base.pdf"
pdf.output(out_path)
print(f"PDF saved: {out_path}")
