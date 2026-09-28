# Production data model

Current prototype: SQLite.

For deployment migrate these tables to persistent Postgres:
- trades
- fundamentals
- backtests
- watchlist
- alert_rules

Recommended production additions:
- source_fetch_log
- parse_errors
- alert_events
- users (only if multi-user authentication is later needed)

Do not store the production database solely on an ephemeral Streamlit filesystem.
