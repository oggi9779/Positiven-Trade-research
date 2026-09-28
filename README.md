# Politician Trade Research V6

V6 is the pre-deployment build.

## New
- Mobile-oriented Home view
- Persistent ticker/politician watchlist
- Persistent research alert rules
- Data-quality scoring and plausibility checks
- 30-day cross-politician ticker clusters
- 30/90/180-day backtests from first session after disclosure
- Official filing links
- SEC fundamentals
- SQLite persistence

## Run locally
pip install -r requirements.txt
streamlit run app.py

## Before deployment
Replace `contact@example.com` in app.py with a real contact email for SEC automated access.

## Next phase
Deployment changes should separate the persistent database from the Streamlit application filesystem
(e.g. hosted Postgres) so history, watchlists and rules survive app restarts/redeployments.
