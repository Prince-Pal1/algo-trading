import subprocess
import json
import time
import datetime
import math

TV_CLI = "node /Users/prince/algo-trading/tradingview-mcp-jackson/src/cli/index.js"

def run_tv_command(cmd, parse_json=True):
    full_cmd = f"{TV_CLI} {cmd}"
    try:
        result = subprocess.run(full_cmd, shell=True, check=True, capture_output=True, text=True)
        out = result.stdout.strip()
        if parse_json:
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                return out
        return out
    except subprocess.CalledProcessError as e:
        print(f"Error running TV command: {cmd}")
        print(f"Stderr: {e.stderr}")
        return None

def setup_chart(symbol="XAUUSD", timeframe="15"):
    print(f"Setting chart to {symbol}, {timeframe}m...")
    run_tv_command(f"symbol {symbol}", parse_json=False)
    run_tv_command(f"timeframe {timeframe}", parse_json=False)
    time.sleep(1)

def fetch_data(date=None, bars=500):
    if date:
        print(f"Scrolling to date: {date}")
        run_tv_command(f"scroll {date}", parse_json=False)
        time.sleep(2)
    
    print(f"Fetching {bars} bars...")
    data = run_tv_command(f"ohlcv -n {bars}")
    if not isinstance(data, list):
        print("Failed to fetch OHLCV list. Check if TV is connected.")
        return []
    
    return data

def calculate_metrics(ohlcv_data):
    if not ohlcv_data or len(ohlcv_data) < 20:
        return None

    trs = []
    closes = [candle['close'] for candle in ohlcv_data]
    
    for i in range(1, len(ohlcv_data)):
        high = ohlcv_data[i]['high']
        low = ohlcv_data[i]['low']
        prev_close = ohlcv_data[i-1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    
    atr = sum(trs) / len(trs) if trs else 0
    
    if len(closes) >= 20:
        sma_20_start = sum(closes[:20]) / 20
        sma_20_end = sum(closes[-20:]) / 20
        trend_slope = (sma_20_end - sma_20_start) / len(closes)
    else:
        trend_slope = 0
        
    return {
        "atr": atr,
        "trend_slope": trend_slope,
        "base_price": closes[-1] if closes else 0
    }

def calculate_similarity(metrics1, metrics2):
    if not metrics1 or not metrics2:
        return float('inf')
        
    norm_atr = 5.0
    norm_slope = 0.5
    
    diff_atr = (metrics1['atr'] - metrics2['atr']) / norm_atr
    diff_slope = (metrics1['trend_slope'] - metrics2['trend_slope']) / norm_slope
    
    return math.sqrt(diff_atr**2 + diff_slope**2)

def run_analysis(target_date="2026-03-24"):
    print("--- Starting Gold Volatility & Trend Analyzer ---")
    setup_chart("XAUUSD", "15")
    
    historical_bars = fetch_data(target_date, bars=384) # 4 days
    reference_metrics = calculate_metrics(historical_bars)
    
    if not reference_metrics:
        print("Failed to compute reference metrics.")
        return
        
    print(f"\nHistorical Signature (March 24-28, 2026):")
    print(f" - ATR: {reference_metrics['atr']:.3f}")
    print(f" - Trend Slope: {reference_metrics['trend_slope']:.4f}")
    
    print("\n--- Starting Live Market Scanner ---")
    setup_chart("XAUUSD", "15")
    
    try:
        while True:
            live_bars = fetch_data(date=None, bars=384)
            live_metrics = calculate_metrics(live_bars)
            
            if live_metrics:
                sim_distance = calculate_similarity(reference_metrics, live_metrics)
                print(f"[{datetime.datetime.now().time()}] Live ATR: {live_metrics['atr']:.3f}, Slope: {live_metrics['trend_slope']:.4f} -> Distance: {sim_distance:.3f}")
                
                if sim_distance < 0.2:
                    print("!!! ALERT !!! Market conditions closely match the March 24-28 reference period!")
            
            time.sleep(60 * 15)
            
    except KeyboardInterrupt:
        print("\nScanner stopped by user.")

if __name__ == "__main__":
    run_analysis("2026-03-24")
