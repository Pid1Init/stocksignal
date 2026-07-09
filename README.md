# stocksignal

Weekly Hong Kong watchlist scanner that:

- Downloads daily OHLC from Yahoo Finance
- Converts daily candles into weekly candles (`W-FRI`)
- Checks two 5-rule patterns
- Sends Telegram alerts when matches are found

## Files

- `weekly_hk_stock_alert.py`: main scanner script
- `requirements.txt`: Python dependencies

## Ubuntu VPS deployment

### 1) Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip tzdata
```

### 2) Create project environment

```bash
cd /path/to/stocksignal
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3) Telegram credentials file (committed)

```bash
cat telegram.env
```

The script uses:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Load from the repo file before running:

```bash
set -a
source ./telegram.env
set +a
```

### 4) Test manually

```bash
source .venv/bin/activate
set -a
source ./telegram.env
set +a
python3 weekly_hk_stock_alert.py --always-send
```

`--always-send` is optional and sends a Telegram message even when no signals are found.

## Schedule every Saturday 9:00 PM (Hong Kong time)

Use cron with `CRON_TZ`:

```bash
crontab -e
```

Add:

```cron
CRON_TZ=Asia/Hong_Kong
0 21 * * 6 . /path/to/stocksignal/.venv/bin/activate && set -a && . /path/to/stocksignal/telegram.env && set +a && python /path/to/stocksignal/weekly_hk_stock_alert.py >> /var/log/stocksignal.log 2>&1
```

Notes:

- `6` means Saturday in cron.
- Keep `telegram.env` readable only by trusted users.
- Ensure `/var/log/stocksignal.log` is writable by the cron user.

## Signal definitions

### Signal set #2 (bullish)

1. Prior week is red candle (`Open > Close`)
2. Current week is green candle (`Open < Close`)
3. Current week opens below prior week low
4. Current week closes above midpoint of prior week (`(Open + Close) / 2`)
5. Current close vs close from 4 weeks ago is `> bullish-threshold` (default `> 5%`)

### Signal set #3 (bearish)

1. Prior week is green candle (`Open < Close`)
2. Current week is red candle (`Open > Close`)
3. Current week opens above prior week high
4. Current week closes below midpoint of prior week (`(Open + Close) / 2`)
5. Current close vs close from 4 weeks ago is `< bearish-threshold` (default `< -5%`)

You can override thresholds:

```bash
python3 weekly_hk_stock_alert.py --bullish-threshold 5 --bearish-threshold -5
```
