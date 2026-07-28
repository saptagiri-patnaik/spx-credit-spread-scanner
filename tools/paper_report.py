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

    def arm_stats(group: list) -> dict | None:
        if not group:
            return None
        wins = [p for p in group if (p.pnl or 0) > 0]
        losses = [p for p in group if (p.pnl or 0) <= 0]
        total = sum(p.pnl or 0 for p in group)
        gross_win = sum(p.pnl for p in wins)
        gross_loss = abs(sum(p.pnl for p in losses))
        # Peak-to-trough on the running equity curve, in entry order.
        equity, peak, drawdown = 0.0, 0.0, 0.0
        for p in sorted(group, key=lambda x: x.closed_at):
            equity += p.pnl or 0
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)
        return {
            "n": len(group), "w": len(wins), "l": len(losses), "total": total,
            "avg_win": gross_win / len(wins) if wins else 0.0,
            "avg_loss": -gross_loss / len(losses) if losses else 0.0,
            "expectancy": total / len(group),
            "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
            "drawdown": drawdown,
        }

    model = arm_stats([p for p in closed if p.arm == "model"])
    baseline = arm_stats([p for p in closed if p.arm == "baseline"])

    print(f"\nclosed: {len(closed)}")
    header = f"  {'':<16}{'MODEL':>12}{'BASELINE':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    def row(label: str, key: str, fmt: str = "{:+.2f}") -> None:
        m = fmt.format(model[key]) if model else "-"
        b = fmt.format(baseline[key]) if baseline else "-"
        print(f"  {label:<16}{m:>12}{b:>12}")

    print(f"  {'trades':<16}{model['n'] if model else '-':>12}"
          f"{baseline['n'] if baseline else '-':>12}")
    print(f"  {'win rate':<16}"
          f"{_pct(model['w'], model['n']) if model else '-':>12}"
          f"{_pct(baseline['w'], baseline['n']) if baseline else '-':>12}")
    row("total P&L", "total")
    row("avg win", "avg_win")
    row("avg loss", "avg_loss")
    row("expectancy", "expectancy")
    row("profit factor", "profit_factor", "{:.2f}")
    row("max drawdown", "drawdown")

    if model and baseline:
        edge = model["expectancy"] - baseline["expectancy"]
        print(f"\n  EDGE OVER BASELINE  ${edge:+.2f} per trade")
        if edge > 0:
            print("  The sentiment layer is adding value on this sample.")
        else:
            print("  The sentiment layer is SUBTRACTING value: mechanical premium")
            print("  selling did better. That is the result that matters most.")
    elif model and not baseline:
        print("\n  No baseline trades yet - nothing to compare against. Enable")
        print("  PAPER_BASELINE_ENABLED so the comparison exists from the start.")

    by_reason: dict[str, list] = {}
    for p in closed:
        by_reason.setdefault(f"{p.arm}/{p.exit_reason or '?'}", []).append(p)
    print("\n  by arm and exit reason:")
    for reason, group in sorted(by_reason.items()):
        pnl = sum(p.pnl or 0 for p in group)
        print(f"    {reason:<18} {len(group):3d} trades   ${pnl:+8.2f}   "
              f"avg ${pnl / len(group):+.2f}")

    print("\n  recent closes:")
    for p in closed[-8:]:
        print(f"    {p.closed_at:%m-%d %H:%M}  {p.arm:<8} {p.strategy:<19} "
              f"{p.short_strike:.0f}/{p.long_strike:.0f}  "
              f"cr ${p.credit:.2f} -> ${p.exit_mark:.2f}  "
              f"{p.exit_reason:<5} ${p.pnl:+.2f}")

    if len(closed) < 30:
        print(f"\n  NOTE: {len(closed)} trades is too few to conclude anything. "
              f"Expect 30+ before the numbers mean much.")


if __name__ == "__main__":
    main()
