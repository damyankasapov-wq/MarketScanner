"""
Strategy checker (backtester): replays a strategy over past sessions of stored
1-minute bars on a simulated account and reports every trade, the account curve
and a summary. Informational only: nothing here places orders.

    python -m marketscanner.backtest --from 2026-10-19 --to 2026-11-13 \
        --markets SPY,QQQ,GLD --cash 10000 --per-deal 1000 --min-cash 2000

Pipeline (each step is a pure function of its inputs):

    BarStore --load--> frames --propose--> TradeIdea --walk_exit--> ExitHit
        engine (cash rule, shorts as collateral) --> Trade rows
        equity curve  +  summary  --> ledger.csv, equity.png, summary.txt,
                                      report.html, trades/<id>.png
"""
