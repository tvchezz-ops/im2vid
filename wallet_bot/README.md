# Telegram Stars Wallet Bot

Minimal standalone Telegram wallet bot for Telegram Stars payments. It sends Stars invoices, marks payment orders as paid, and credits users in the shared database.

## Environment

Set these variables for the wallet bot service:

```env
WALLET_BOT_TOKEN=
MAIN_BOT_USERNAME=
DATABASE_URL=
WALLET_ALLOWED_AMOUNTS=100,300,500,1000,3000,5000
```

- `WALLET_BOT_TOKEN` is the token of the wallet bot.
- `MAIN_BOT_USERNAME` is the username of the generation bot without `@`.
- `DATABASE_URL` must point to the same PostgreSQL database used by the main bot.
- `WALLET_ALLOWED_AMOUNTS` is a comma-separated list of allowed Telegram Stars packages.

The main bot and wallet bot must not use the same bot token. The main bot uses `BOT_TOKEN`; the wallet bot uses `WALLET_BOT_TOKEN`. Both services use the same PostgreSQL `DATABASE_URL` so the wallet bot can create and complete payment orders for the same users.

## Run

```bash
python -m wallet_bot.main
```

## Railway

You can create a separate Railway service from the same repository for the wallet bot.

Start command:

```bash
python -m wallet_bot.main
```

Configure the wallet service with `WALLET_BOT_TOKEN`, `MAIN_BOT_USERNAME`, `DATABASE_URL`, and `WALLET_ALLOWED_AMOUNTS`. Use a different Telegram bot token from the main bot service, and point both services to the same PostgreSQL database.
