"""Realised P&L on simulated positions.

    python -m tools.paper_report

Reports the numbers that decide whether the strategy pays, not whether the
direction was right. Expectancy is the one to watch: a credit spread book can
win most of its trades and still lose money, because the losses are larger
than the wins by construction.
"""
from __future__ import annotations

import datetime as dt

from config import get_settings
from db.repository import Repository


def _pct(hit: int, total: int) -> str:
    return f"{hit / total * 100:.0f}%" if total else "n/a"


def main() -> None:
    settings = get_settings()
    repo = Repository(settings.database_url)
    closed = repo.closed_paper_positions()
    open_positions = repo.open_paper_positions()

    print("=" * 62)
    print("PAPER TRADING")
    print("=" * 62)
    print(f"exit rules: close after {getattr(settings, 'paper_hold_days', 4)} days, "
          f"or if the mark reaches {getattr(settings, 'paper_stop_multiple', 2.0)}x credit")

    if open_positions:
        print(f"\nopen ({len(open_positions)}):")
        now = dt.datetime.now(dt.timezone.utc)
        for p in open_positions:
            opened = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=dt.timezone.utc)
            held = (now - opened).total_seconds() / 86400.0
            mark = p.last_mark if p.last_mark is not None else p.credit
            unreal = p.credit - mark
            print(f"  #{p.id:<4} {p.strategy:<19} {p.short_strike:.0f}/{p.long_strike:.0f}"
                  f"  exp {p.expiration}  held {held:.1f}d"
                  f"  mark ${mark:.2f}  unrealised ${unreal:+.2f}"
                  f"  stop ${p.stop_price:.2f}")

    if not closed:
        print("\nNo closed positions yet - nothing to measure.")
        print("Positions open only when a scan clears all three gates AND an")
        print("option chain is available, so check Schwab connectivity first.")
        return

    wins = [p for p in closed if (p.pnl or 0) > 0]
    losses = [p for p in closed if (p.pnl or 0) <= 0]
    total = sum(p.pnl or 0 for p in closed)
    avg_win = sum(p.pnl for p in wins) / len(wins) if wins else 0.0
    avg_loss = sum(p.pnl for p in losses) / len(losses) if losses else 0.0
    risked = sum(p.credit * getattr(settings, "paper_stop_multiple", 2.0) - p.credit
                 for p in closed)

    print(f"\nclosed: {len(closed)}")
    print(f"  win rate      {_pct(len(wins), len(closed))}   ({len(wins)}W / {len(losses)}L)")
    print(f"  total P&L     ${total:+.2f} per contract")
    print(f"  avg win       ${avg_win:+.2f}")
    print(f"  avg loss      ${avg_loss:+.2f}")
    print(f"  expectancy    ${total / len(closed):+.2f} per trade   <- the number that matters")
    if risked:
        print(f"  return on risk {total / risked * 100:+.1f}%")

    by_reason: dict[str, list] = {}
    for p in closed:
        by_reason.setdefault(p.exit_reason or "?", []).append(p)
    print("\n  by exit reason:")
    for reason, group in sorted(by_reason.items()):
        pnl = sum(p.pnl or 0 for p in group)
        print(f"    {reason:<6} {len(group):3d} trades   ${pnl:+8.2f}   "
              f"avg ${pnl / len(group):+.2f}")

    print("\n  recent closes:")
    for p in closed[-8:]:
        print(f"    {p.closed_at:%m-%d %H:%M}  {p.strategy:<19} "
              f"{p.short_strike:.0f}/{p.long_strike:.0f}  "
              f"cr ${p.credit:.2f} -> ${p.exit_mark:.2f}  "
              f"{p.exit_reason:<5} ${p.pnl:+.2f}")

    if len(closed) < 30:
        print(f"\n  NOTE: {len(closed)} trades is too few to conclude anything. "
              f"Expect 30+ before the numbers mean much.")


if __name__ == "__main__":
    main()
