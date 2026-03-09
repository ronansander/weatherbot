#!/usr/bin/env python3
"""
Weather Trading Bot v3 — Polymarket
Auto-cycle + Forecast Monitoring + Kelly + EV

Two threads running in parallel:
  - Entry thread:  scans for new trades every 60 minutes
  - Monitor thread: checks forecasts every 10 minutes, closes if EV goes negative

Usage:
    python bot_v3.py           # Start both threads (paper mode)
    python bot_v3.py --live    # Start both threads (live simulation)
    python bot_v3.py --once    # Run one scan and exit (no loop)
    python bot_v3.py --positions
    python bot_v3.py --reset
"""

import re
import json
import time
import argparse
import threading
import requests
from datetime import datetime, timezone, timedelta

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json") as f:
    _cfg = json.load(f)

ENTRY_THRESHOLD   = _cfg.get("entry_threshold", 0.15)
EXIT_THRESHOLD    = _cfg.get("exit_threshold", 0.45)
MAX_TRADES        = _cfg.get("max_trades_per_run", 5)
MIN_HOURS_LEFT    = _cfg.get("min_hours_to_resolution", 2)

NOAA_ACCURACY     = 0.78
KELLY_FRACTION    = 0.25
MAX_POSITION_PCT  = 0.10
MIN_EV            = 0.05
SIM_BALANCE       = 1000.0

ENTRY_INTERVAL    = 60 * 60      # Scan for new entries every 60 minutes
MONITOR_INTERVAL  = 10 * 60      # Check forecasts every 10 minutes

LOCATIONS = {
    "nyc":     {"lat": 40.71, "lon": -74.00, "name": "New York City"},
    "chicago": {"lat": 41.87, "lon": -87.62, "name": "Chicago"},
    "miami":   {"lat": 25.76, "lon": -80.19, "name": "Miami"},
    "dallas":  {"lat": 32.77, "lon": -96.79, "name": "Dallas"},
    "seattle": {"lat": 47.60, "lon": -122.33, "name": "Seattle"},
    "atlanta": {"lat": 33.74, "lon": -84.38, "name": "Atlanta"},
}

ACTIVE_LOCATIONS = _cfg.get("locations", "nyc,chicago,miami,dallas,seattle,atlanta").split(",")
ACTIVE_LOCATIONS = [l.strip().lower() for l in ACTIVE_LOCATIONS]

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# COLORS
# =============================================================================

class C:
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"
    RESET  = "\033[0m"
    BOLD   = "\033[1m"

def ok(msg):    print(f"{C.GREEN}  ✅ {msg}{C.RESET}")
def warn(msg):  print(f"{C.YELLOW}  ⚠️  {msg}{C.RESET}")
def info(msg):  print(f"{C.CYAN}  {msg}{C.RESET}")
def skip(msg):  print(f"{C.GRAY}  ⏸️  {msg}{C.RESET}")
def alert(msg): print(f"{C.RED}  🚨 {msg}{C.RESET}")

def ts():
    return datetime.now().strftime("%H:%M:%S")

# =============================================================================
# KELLY + EV
# =============================================================================

def calculate_ev(our_prob: float, market_price: float) -> float:
    if market_price <= 0 or market_price >= 1:
        return 0.0
    payout = (1.0 / market_price) - 1.0
    ev = (our_prob * payout) - (1.0 - our_prob)
    return round(ev, 4)

def calculate_kelly(our_prob: float, market_price: float) -> float:
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1.0 / market_price) - 1.0
    p = our_prob
    q = 1.0 - p
    kelly = (p * b - q) / b
    kelly = max(0.0, kelly)
    kelly = kelly * KELLY_FRACTION
    kelly = min(kelly, MAX_POSITION_PCT)
    return round(kelly, 4)

def calculate_position_size(kelly_fraction: float, balance: float) -> float:
    return round(kelly_fraction * balance, 2)

# =============================================================================
# SIMULATION STATE
# =============================================================================

SIM_FILE = "simulation.json"
_sim_lock = threading.Lock()  # Thread-safe file access

def load_sim() -> dict:
    try:
        with open(SIM_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "balance": SIM_BALANCE,
            "starting_balance": SIM_BALANCE,
            "positions": {},
            "trades": [],
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "peak_balance": SIM_BALANCE,
        }

def save_sim(sim: dict):
    with open(SIM_FILE, "w") as f:
        json.dump(sim, f, indent=2)

def reset_sim():
    import os
    if os.path.exists(SIM_FILE):
        os.remove(SIM_FILE)
    print(f"{C.GREEN}  ✅ Simulation reset — balance back to ${SIM_BALANCE:.2f}{C.RESET}")

# =============================================================================
# OPEN-METEO FORECAST
# =============================================================================

def get_forecast(city_slug: str) -> dict:
    loc = LOCATIONS[city_slug]
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit&forecast_days=4"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        result = {}
        for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
            result[date] = round(temp, 1)
        return result
    except Exception as e:
        warn(f"Forecast error for {city_slug}: {e}")
        return {}

# =============================================================================
# POLYMARKET API
# =============================================================================

def get_polymarket_event(city_slug: str, month: str, day: int, year: int):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception as e:
        warn(f"Polymarket API error: {e}")
    return None

def get_market_price(market_id: str) -> float:
    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        r = requests.get(url, timeout=5)
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

# =============================================================================
# PARSING
# =============================================================================

def parse_temp_range(question: str):
    if not question:
        return None
    if "or below" in question.lower():
        m = re.search(r'(\d+)°F or below', question, re.IGNORECASE)
        if m: return (-999, int(m.group(1)))
    if "or higher" in question.lower():
        m = re.search(r'(\d+)°F or higher', question, re.IGNORECASE)
        if m: return (int(m.group(1)), 999)
    m = re.search(r'between (\d+)-(\d+)°F', question, re.IGNORECASE)
    if m: return (int(m.group(1)), int(m.group(2)))
    return None

def hours_until_resolution(event: dict) -> float:
    try:
        end_date = event.get("endDate") or event.get("end_date_iso")
        if not end_date: return 999
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        delta = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        return max(0, delta)
    except Exception:
        return 999

# =============================================================================
# SHOW POSITIONS
# =============================================================================

def show_positions():
    sim = load_sim()
    positions = sim["positions"]
    print(f"\n{C.BOLD}📊 Open Positions:{C.RESET}")
    if not positions:
        print("  No open positions")
        return

    total_pnl = 0
    for mid, pos in positions.items():
        current_price = get_market_price(mid) or pos["entry_price"]
        pnl = (current_price - pos["entry_price"]) * pos["shares"]
        total_pnl += pnl
        pnl_str = f"{C.GREEN}+${pnl:.2f}{C.RESET}" if pnl >= 0 else f"{C.RED}-${abs(pnl):.2f}{C.RESET}"
        print(f"\n  • {pos['question'][:65]}...")
        print(f"    Entry: ${pos['entry_price']:.3f} | Now: ${current_price:.3f} | PnL: {pnl_str}")
        print(f"    Kelly: {pos.get('kelly_pct', 0):.1%} | EV: {pos.get('ev', 0):.2f} | Cost: ${pos['cost']:.2f}")
        print(f"    Last forecast: {pos.get('last_forecast_temp', '?')}°F | Date: {pos.get('date', '?')}")

    print(f"\n  Balance:      ${sim['balance']:.2f}")
    pnl_color = C.GREEN if total_pnl >= 0 else C.RED
    print(f"  Open PnL:     {pnl_color}{'+'if total_pnl>=0 else ''}{total_pnl:.2f}{C.RESET}")
    print(f"  Total trades: {sim['total_trades']} | W/L: {sim['wins']}/{sim['losses']}")

# =============================================================================
# FORECAST MONITOR THREAD
# Runs every 10 minutes — re-fetches forecast for each open position
# Closes position if new forecast temp no longer matches the bucket we bought
# =============================================================================

def forecast_monitor(dry_run: bool):
    print(f"\n{C.CYAN}  📡 Forecast monitor started — checking every {MONITOR_INTERVAL//60} minutes{C.RESET}")

    while True:
        time.sleep(MONITOR_INTERVAL)

        print(f"\n{C.BOLD}{C.CYAN}[{ts()}] 🔄 Forecast check...{C.RESET}")

        with _sim_lock:
            sim = load_sim()
            positions = sim["positions"]

            if not positions:
                skip("No open positions to monitor")
                continue

            for mid, pos in list(positions.items()):
                city_slug = pos.get("location", "")
                date_str = pos.get("date", "")
                question = pos.get("question", "")
                entry_price = pos.get("entry_price", 0)
                shares = pos.get("shares", 0)
                cost = pos.get("cost", 0)

                if city_slug not in LOCATIONS:
                    continue

                # Get fresh forecast
                forecast = get_forecast(city_slug)
                new_temp = forecast.get(date_str)

                if new_temp is None:
                    skip(f"No forecast data for {city_slug} {date_str}")
                    continue

                # Update stored forecast temp
                old_temp = pos.get("last_forecast_temp", pos.get("forecast_temp"))
                pos["last_forecast_temp"] = new_temp

                # Get current market price
                current_price = get_market_price(mid)
                if current_price is None:
                    continue

                # Check if new forecast still matches our bucket
                rng = parse_temp_range(question)
                forecast_still_matches = rng and rng[0] <= new_temp <= rng[1]

                # Recalculate EV with current price
                new_ev = calculate_ev(NOAA_ACCURACY, current_price)
                pnl = (current_price - entry_price) * shares

                city_name = LOCATIONS[city_slug]["name"]
                print(f"\n  📍 {city_name} — {date_str}")
                info(f"Old forecast: {old_temp}°F → New forecast: {new_temp}°F")
                info(f"Market price: ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                info(f"EV: {new_ev:+.2f} | Forecast matches bucket: {forecast_still_matches}")

                # Decision: close if forecast no longer matches OR EV went negative
                should_close = False
                close_reason = ""

                if not forecast_still_matches:
                    should_close = True
                    close_reason = f"Forecast changed to {new_temp}°F — no longer in our bucket"

                elif new_ev < 0:
                    should_close = True
                    close_reason = f"EV dropped to {new_ev:.2f} — edge gone"

                if should_close:
                    alert(f"CLOSING: {close_reason}")
                    info(f"Closing at ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

                    if not dry_run:
                        sim["balance"] = round(sim["balance"] + cost + pnl, 2)
                        sim["wins"] += 1 if pnl > 0 else 0
                        sim["losses"] += 1 if pnl <= 0 else 0
                        sim["trades"].append({
                            "type": "forecast_exit",
                            "question": question,
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "pnl": round(pnl, 2),
                            "cost": cost,
                            "close_reason": close_reason,
                            "old_forecast": old_temp,
                            "new_forecast": new_temp,
                            "ev_at_close": new_ev,
                            "kelly_pct": pos.get("kelly_pct", 0),
                            "closed_at": datetime.now().isoformat(),
                        })
                        del sim["positions"][mid]
                        ok(f"Position closed — balance: ${sim['balance']:.2f}")
                    else:
                        skip("Paper mode — not closing")
                else:
                    ok(f"Holding — forecast still valid, EV positive")

            sim["peak_balance"] = max(sim.get("peak_balance", sim["balance"]), sim["balance"])
            save_sim(sim)


# =============================================================================
# ENTRY SCANNER THREAD
# Runs every 60 minutes — scans all cities for new entry signals
# Skips markets where position is already open
# =============================================================================

def entry_scanner(dry_run: bool):
    # First run immediately, then every ENTRY_INTERVAL
    run_count = 0

    while True:
        run_count += 1
        print(f"\n{'='*55}")
        print(f"{C.BOLD}{C.CYAN}[{ts()}] 🔍 Entry scan #{run_count}{C.RESET}")
        print(f"{'='*55}")

        with _sim_lock:
            sim = load_sim()
            balance = sim["balance"]
            positions = sim["positions"]
            trades_executed = 0
            exits_found = 0

            # --- CHECK PRICE-BASED EXITS ---
            print(f"\n{C.BOLD}📤 Checking price exits...{C.RESET}")
            for mid, pos in list(positions.items()):
                current_price = get_market_price(mid)
                if current_price is None:
                    continue

                if current_price >= EXIT_THRESHOLD:
                    exits_found += 1
                    pnl = (current_price - pos["entry_price"]) * pos["shares"]
                    ok(f"EXIT: {pos['question'][:50]}...")
                    info(f"Price ${current_price:.3f} >= exit ${EXIT_THRESHOLD:.2f} | PnL: +${pnl:.2f}")

                    if not dry_run:
                        balance += pos["cost"] + pnl
                        sim["wins"] += 1 if pnl > 0 else 0
                        sim["losses"] += 1 if pnl <= 0 else 0
                        sim["trades"].append({
                            "type": "exit",
                            "question": pos["question"],
                            "entry_price": pos["entry_price"],
                            "exit_price": current_price,
                            "pnl": round(pnl, 2),
                            "cost": pos["cost"],
                            "kelly_pct": pos.get("kelly_pct", 0),
                            "ev": pos.get("ev", 0),
                            "closed_at": datetime.now().isoformat(),
                        })
                        del positions[mid]
                        ok(f"Closed — PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                    else:
                        skip("Paper mode — not selling")

            if exits_found == 0:
                skip("No price-based exits")

            # --- SCAN ENTRIES ---
            print(f"\n{C.BOLD}🌤 Scanning cities...{C.RESET}")

            for city_slug in ACTIVE_LOCATIONS:
                if city_slug not in LOCATIONS:
                    continue

                loc_data = LOCATIONS[city_slug]
                forecast = get_forecast(city_slug)
                if not forecast:
                    continue

                for i in range(0, 4):
                    date = datetime.now() + timedelta(days=i)
                    date_str = date.strftime("%Y-%m-%d")
                    month = MONTHS[date.month - 1]
                    day = date.day
                    year = date.year

                    forecast_temp = forecast.get(date_str)
                    if forecast_temp is None:
                        continue

                    event = get_polymarket_event(city_slug, month, day, year)
                    if not event:
                        continue

                    hours_left = hours_until_resolution(event)

                    print(f"\n{C.BOLD}📍 {loc_data['name']} — {date_str}{C.RESET}")
                    info(f"Forecast: {forecast_temp}°F | Resolves in: {hours_left:.0f}h")

                    if hours_left < MIN_HOURS_LEFT:
                        skip(f"Resolves in {hours_left:.0f}h — too soon")
                        continue

                    # Find matching bucket
                    matched = None
                    for market in event.get("markets", []):
                        question = market.get("question", "")
                        rng = parse_temp_range(question)
                        if rng and rng[0] <= forecast_temp <= rng[1]:
                            try:
                                prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                                yes_price = float(prices[0])
                            except Exception:
                                continue
                            matched = {"market": market, "question": question,
                                       "price": yes_price, "range": rng}
                            break

                    if not matched:
                        skip(f"No bucket for {forecast_temp}°F")
                        continue

                    price = matched["price"]
                    market_id = matched["market"].get("id", "")
                    question = matched["question"]

                    info(f"Bucket: {question[:60]}")
                    info(f"Market price: ${price:.3f}")

                    # Kelly + EV
                    our_prob = NOAA_ACCURACY
                    ev = calculate_ev(our_prob, price)
                    kelly_pct = calculate_kelly(our_prob, price)
                    position_size = calculate_position_size(kelly_pct, balance)

                    ev_color = C.GREEN if ev > 0 else C.RED
                    print(f"  {C.CYAN}  EV: {ev_color}{ev:+.2f}{C.RESET}  "
                          f"{C.CYAN}Kelly: {kelly_pct:.1%}  "
                          f"Size: ${position_size:.2f}{C.RESET}")

                    if price >= ENTRY_THRESHOLD:
                        skip(f"Price ${price:.3f} above threshold")
                        continue

                    if ev < MIN_EV:
                        skip(f"EV {ev:.2f} below minimum — skip")
                        continue

                    if kelly_pct <= 0:
                        skip("Kelly says no edge — skip")
                        continue

                    if market_id in positions:
                        skip("Already in this market")
                        continue

                    if trades_executed >= MAX_TRADES:
                        skip(f"Max trades ({MAX_TRADES}) reached")
                        continue

                    if position_size < 0.50:
                        skip(f"Position size ${position_size:.2f} too small")
                        continue

                    ok(f"ENTRY — EV={ev:+.2f} | Kelly={kelly_pct:.1%} | ${position_size:.2f}")

                    if not dry_run:
                        shares = position_size / price
                        balance -= position_size
                        positions[market_id] = {
                            "question": question,
                            "entry_price": price,
                            "shares": shares,
                            "cost": position_size,
                            "kelly_pct": kelly_pct,
                            "ev": ev,
                            "our_prob": our_prob,
                            "date": date_str,
                            "location": city_slug,
                            "forecast_temp": forecast_temp,
                            "last_forecast_temp": forecast_temp,
                            "opened_at": datetime.now().isoformat(),
                        }
                        sim["total_trades"] += 1
                        sim["trades"].append({
                            "type": "entry",
                            "question": question,
                            "entry_price": price,
                            "shares": shares,
                            "cost": position_size,
                            "kelly_pct": kelly_pct,
                            "ev": ev,
                            "our_prob": our_prob,
                            "location": city_slug,
                            "date": date_str,
                            "opened_at": datetime.now().isoformat(),
                        })
                        trades_executed += 1
                        ok(f"Position opened — ${position_size:.2f} deducted")
                    else:
                        skip("Paper mode — not buying")
                        trades_executed += 1

            # Save
            if not dry_run:
                sim["balance"] = round(balance, 2)
                sim["positions"] = positions
                sim["peak_balance"] = max(sim.get("peak_balance", balance), balance)
                save_sim(sim)

            print(f"\n  Balance: ${balance:.2f} | "
                  f"Trades: {trades_executed} | "
                  f"Exits: {exits_found} | "
                  f"Open positions: {len(positions)}")

        if dry_run:
            print(f"\n  {C.YELLOW}[PAPER MODE — use --live to simulate trades]{C.RESET}")

        next_scan = datetime.now() + timedelta(seconds=ENTRY_INTERVAL)
        print(f"\n  {C.GRAY}Next scan at {next_scan.strftime('%H:%M:%S')}{C.RESET}")
        time.sleep(ENTRY_INTERVAL)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weather Trading Bot v3 — Auto-cycle + Forecast Monitor")
    parser.add_argument("--live", action="store_true", help="Execute trades (updates simulation balance)")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit (no loop)")
    parser.add_argument("--positions", action="store_true", help="Show open positions")
    parser.add_argument("--reset", action="store_true", help="Reset simulation to $1000")
    args = parser.parse_args()

    if args.reset:
        reset_sim()

    elif args.positions:
        show_positions()

    elif args.once:
        # Single scan, no loop — useful for testing
        entry_scanner_once = threading.Thread(target=entry_scanner, args=(not args.live,), daemon=True)
        entry_scanner_once.start()
        entry_scanner_once.join(timeout=300)

    else:
        dry_run = not args.live
        mode = f"{C.YELLOW}PAPER MODE{C.RESET}" if dry_run else f"{C.GREEN}LIVE MODE{C.RESET}"

        print(f"\n{C.BOLD}{C.CYAN}🌤  Weather Trading Bot v3 — Auto-cycle + Forecast Monitor{C.RESET}")
        print("=" * 60)
        print(f"  Mode:             {mode}")
        print(f"  Entry scan:       every {ENTRY_INTERVAL//60} minutes")
        print(f"  Forecast monitor: every {MONITOR_INTERVAL//60} minutes")
        print(f"  Kelly fraction:   {KELLY_FRACTION:.0%}")
        print(f"  Max per trade:    {MAX_POSITION_PCT:.0%} of balance")
        print(f"  Min EV:           {MIN_EV:.2f}")
        print(f"  Press Ctrl+C to stop\n")

        # Start both threads
        t_entry = threading.Thread(
            target=entry_scanner,
            args=(dry_run,),
            daemon=True,
            name="EntryScanner"
        )
        t_monitor = threading.Thread(
            target=forecast_monitor,
            args=(dry_run,),
            daemon=True,
            name="ForecastMonitor"
        )

        t_entry.start()
        t_monitor.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}  Bot stopped{C.RESET}")
