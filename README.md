# stocksignal

Weekly Hong Kong watchlist scanner that:

- Downloads daily OHLC from Yahoo Finance
- Converts daily candles into weekly candles (`W-FRI`)
- Checks two 5-rule patterns
- Sends Telegram alerts when matches are found

## Files

- `weekly_hk_stock_alert.py`: main scanner script
- `run_weekly_scan.sh`: one-command launcher (loads `.venv` + `telegram.env`)
- `daily_tb_vol_rsi_alert.py`: TB_VOL_RSI_V1 daily scanner (Tweezer Bottom + Volume + RSI)
- `run_daily_tb_signal.sh`: one-command launcher for daily TB_VOL_RSI_V1 scan
- `telegram_trigger_listener.py`: handles Telegram `/trigger` command and runs all scanners
- `run_trigger_listener.sh`: one-command launcher for `/trigger` listener
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
chmod +x ./run_weekly_scan.sh
./run_weekly_scan.sh --always-send
```

`--always-send` is optional and sends a Telegram message even when no signals are found.

## Daily strategy (TB_VOL_RSI_V1) at 10:00 PM Hong Kong time

This scanner checks only the latest completed trading day for:

- Tweezer Bottom: previous day red, current day green, lows match within `0.5%`
- Volume filter: `volume / SMA20(volume) >= 1.5`
- RSI filter: `RSI14` between `60` and `80` (inclusive)

Manual test:

```bash
chmod +x ./run_daily_tb_signal.sh
./run_daily_tb_signal.sh
```

Use `--only-on-signal` to suppress no-signal messages.

## Schedule every Saturday 9:00 PM (Hong Kong time)

Use cron with `CRON_TZ`:

```bash
crontab -e
```

Add:

```cron
CRON_TZ=Asia/Hong_Kong
0 21 * * 6 /path/to/stocksignal/run_weekly_scan.sh >> /var/log/stocksignal.log 2>&1
```

Notes:

- `6` means Saturday in cron.
- Keep `telegram.env` readable only by trusted users.
- Ensure `/var/log/stocksignal.log` is writable by the cron user.

## Add daily cron at 10:00 PM (Hong Kong time)

```cron
CRON_TZ=Asia/Hong_Kong
0 22 * * * /path/to/stocksignal/run_daily_tb_signal.sh >> /var/log/stocksignal-daily.log 2>&1
```

## Telegram `/trigger` command (manual on-demand scan)

When you send `/trigger` to your bot from your configured `TELEGRAM_CHAT_ID`, it will:

- run the weekly scanner
- run the daily TB_VOL_RSI_V1 scanner
- reply back to Telegram with combined results

Manual test:

```bash
chmod +x ./run_trigger_listener.sh
./run_trigger_listener.sh --ignore-old-updates
./run_trigger_listener.sh
```

Recommended cron (checks bot updates every minute):

```cron
* * * * * /path/to/stocksignal/run_trigger_listener.sh >> /var/log/stocksignal-trigger.log 2>&1
```

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

## Bitcoin wallet generator (idle-capacity worker)

The repository now includes a standalone tool that continuously generates random
12-word Bitcoin seed phrases and appends wallet records to a JSONL file.

### Features

- Continuous wallet generation (optionally gated by VPS load)
- Backlog cap at configurable size (default `85 GB`)
- Local-node balance mode using Bitcoin Core (`bitcoin-cli`) for high scale
- Hourly Telegram summary of total BTC across all generated wallet addresses
- Telegram `/new` command to clear backlog and continue generating fresh wallets
- Batched address import worker for `100k+` wallet tracking

Install dependencies:

```bash
pip install -r requirements.txt
```

Run a quick test (generates 5 wallets and exits, Telegram disabled):

```bash
python3 bitcoin_wallet_generator.py --count 5 --ignore-load --disable-telegram
```

Run continuously with local-node balance mode:

```bash
python3 bitcoin_wallet_generator.py \
  --output ./generated_wallets.jsonl \
  --max-load-per-cpu 0.60 \
  --poll-seconds 5 \
  --storage-limit-gb 85 \
  --telegram-summary-interval-seconds 3600 \
  --balance-mode local-node \
  --bitcoin-cli-path bitcoin-cli
```

Optional: include private keys in output (highly sensitive):

```bash
python3 bitcoin_wallet_generator.py --include-private-key
```

### Telegram commands

- `/new` — clears `generated_wallets.jsonl` backlog and continues generation.
- `/check` — triggers an immediate wallet balance summary message.

The script reads Telegram credentials from env vars:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### Full-node setup for local mode

`--balance-mode local-node` requires a local Bitcoin Core node and `bitcoin-cli`.

Important storage note: **a real Bitcoin full node does not fit safely in 7 GB**.
Even in prune mode, chainstate + wallet/index overhead is already above that
budget on modern chain heights. Use a larger disk budget (at least tens of GB).

Ubuntu setup example:

```bash
sudo apt update
sudo apt install -y bitcoind bitcoin-cli
mkdir -p ~/.bitcoin
```

Create `~/.bitcoin/bitcoin.conf`:

```ini
server=1
daemon=1
txindex=0
prune=5500
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
```

Start node and verify sync status:

```bash
bitcoind -daemon
bitcoin-cli getblockchaininfo
```

The Python generator auto-creates and uses watch-only wallet(s) via
`bitcoin-cli` in local mode. `/new` creates a fresh watch-only wallet namespace
so hourly totals reflect only newly generated addresses after reset.
On modern Bitcoin Core versions, this uses descriptor watch-only wallets.

### systemd service (auto-start on reboot)

1. Make launcher executable:

```bash
chmod +x /workspace/run_bitcoin_wallet_generator.sh
```

2. Install service unit:

```bash
sudo cp /workspace/deploy/systemd/bitcoin-wallet-generator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bitcoin-wallet-generator.service
```

3. Check logs/status:

```bash
systemctl status bitcoin-wallet-generator.service
journalctl -u bitcoin-wallet-generator.service -f
```

### Daily chainstate rebuild script (03:00 Hong Kong time)

`rebuild_chainstate_daily.sh` is included for aggressive maintenance workflows
that intentionally delete `chainstate` and let Bitcoin Core rebuild it from
available block files.

> Warning: this is disruptive and can keep your node in reindex/rebuild work
> most of the time if run too frequently.

Run once manually:

```bash
sudo /root/stocksignal/rebuild_chainstate_daily.sh
```

Schedule it daily at 03:00 HKT with cron:

```bash
crontab -e
```

Add:

```cron
CRON_TZ=Asia/Hong_Kong
0 3 * * * /root/stocksignal/rebuild_chainstate_daily.sh >> /var/log/chainstate-rebuild.log 2>&1
```

## Full node reset automation (7 GiB prune + 50 GiB hard cap)

Two scripts are included for full lifecycle automation:

- `deploy_bitcoin_node.sh`
  - installs Bitcoin Core binaries (if missing)
  - enforces OS-level 50 GiB cap with `/root/bitcoin50.img` mounted at `/mnt/bitcoin50`
  - symlinks `/root/.bitcoin` to that capped mount
  - writes `bitcoin.conf` with `prune=7000` and RPC localhost settings
  - starts node and restarts `bitcoin-wallet-generator.service` so `/check` works
  - installs daily cron reset at 03:00 HKT
- `reset_bitcoin_node_daily.sh`
  - stops node + generator
  - deletes node data mount/image and all `/root/.bitcoin.bak*`
  - redeploys from scratch by calling `deploy_bitcoin_node.sh --reset-data`
  - sends Telegram alert on success/failure when `telegram.env` contains
    `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

### Initial deployment

```bash
sudo /root/stocksignal/deploy_bitcoin_node.sh --reset-data
```

### Manual destructive daily reset run

```bash
sudo /root/stocksignal/reset_bitcoin_node_daily.sh
```

### Installed schedule

`deploy_bitcoin_node.sh` installs this root crontab entry:

```cron
CRON_TZ=Asia/Hong_Kong
0 3 * * * /root/stocksignal/reset_bitcoin_node_daily.sh >> /var/log/bitcoin-node-reset.log 2>&1
```
