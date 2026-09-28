
import re, sqlite3, requests, time, io, hashlib, os, shutil
import pdfplumber
from datetime import datetime
import pandas as pd
import numpy as np
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Politician Trade Research V7.0",layout="wide",initial_sidebar_state="collapsed")
DB=os.getenv("POLITICIAN_DB_PATH","politician_trades.db"); HOUSE="https://disclosures-clerk.house.gov"
SEC="https://data.sec.gov"; SEC_WWW="https://www.sec.gov"
SEC_CONTACT=os.getenv("SEC_CONTACT_EMAIL","contact@example.com")
UA={"User-Agent":f"PoliticianTradeResearch personal research {SEC_CONTACT}"}

def cx():
    c=sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
      filing_id TEXT,politician TEXT,chamber TEXT,ticker TEXT,asset TEXT,"transaction" TEXT,
      trade_date TEXT,notification_date TEXT,amount TEXT,amount_low REAL,amount_high REAL,
      source_url TEXT,UNIQUE(filing_id,chamber,ticker,asset,"transaction",trade_date,amount))""")
    c.execute("""CREATE TABLE IF NOT EXISTS backtests(
      filing_id TEXT,chamber TEXT,ticker TEXT,notification_date TEXT,entry_date TEXT,entry_price REAL,
      r30 REAL,r90 REAL,r180 REAL,b30 REAL,b90 REAL,b180 REAL,
      excess30 REAL,excess90 REAL,excess180 REAL,updated TEXT,
      UNIQUE(filing_id,chamber,ticker,notification_date))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fundamentals(
      ticker TEXT PRIMARY KEY,cik TEXT,company TEXT,assets REAL,liabilities REAL,revenue REAL,
      net_income REAL,equity REAL,shares REAL,sec_updated TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS import_keys(
      tx_key TEXT PRIMARY KEY, filing_id TEXT, created TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS enrichment_status(
      kind TEXT, item TEXT, status TEXT, attempts INTEGER DEFAULT 0,
      last_error TEXT, updated TEXT, PRIMARY KEY(kind,item))""")
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist(
      kind TEXT,value TEXT,created TEXT,UNIQUE(kind,value))""")
    c.execute("""CREATE TABLE IF NOT EXISTS alert_rules(
      id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,min_amount REAL,max_lag INTEGER,
      cluster_min INTEGER,small_company_revenue REAL,enabled INTEGER DEFAULT 1)""")
    c.commit();return c

def http(url):
    r=requests.get(url,headers=UA,timeout=45);r.raise_for_status();return r

def amount_range(s):
    n=[float(x.replace(",","")) for x in re.findall(r"\$?([\d,]+)",str(s))]
    return (n[0],n[1]) if len(n)>=2 else ((n[0],n[0]) if n else (None,None))

def ticker_from_asset(s):
    m=re.findall(r"\(([A-Z][A-Z0-9.\-]{0,7})\)",str(s));return m[-1] if m else None


def norm(v):
    return re.sub(r"\s+"," ",str(v or "")).strip().upper()

def stable_tx_key(row):
    # row layout: filing_id,name,chamber,ticker,asset,tx,trade_date,notification_date,amount,lo,hi,url
    # Deliberately exclude politician name/source URL and normalized numeric range; the filing and
    # transaction fields identify the disclosed line while remaining stable across presentation changes.
    parts=[row[0], row[2], row[3], row[4], row[5], row[6], row[7], row[8]]
    return hashlib.sha256("|".join(norm(x) for x in parts).encode("utf-8")).hexdigest()

def bootstrap_import_keys(c):
    # Register already-imported rows so upgrading from V6.2 doesn't reinsert them.
    rows=c.execute('SELECT filing_id,politician,chamber,ticker,asset,"transaction",trade_date,notification_date,amount,amount_low,amount_high,source_url FROM trades').fetchall()
    for row in rows:
        k=stable_tx_key(row)
        c.execute("INSERT OR IGNORE INTO import_keys(tx_key,filing_id,created) VALUES(?,?,?)",
                  (k,str(row[0]),str(datetime.now())))
    c.commit()

@st.cache_data(ttl=21600)
def house_index(year):
    txt=http(f"{HOUSE}/public_disc/financial-pdfs/{year}FD.txt").content.decode("utf-8",errors="replace")
    rows=[x.split("\t")[:9] for x in txt.splitlines() if len(x.split("\t"))>=9]
    d=pd.DataFrame(rows,columns=["Prefix","Last","First","Suffix","FilingType","StateDistrict","Year","FilingDate","DocID"])
    return d[d.FilingType.astype(str).str.contains("P",case=False,na=False)]

@st.cache_data(ttl=21600)
def parse_ptr(docid,name,year):
    # Official House PTR PDFs live in a YEAR subdirectory.
    url=f"{HOUSE}/public_disc/ptr-pdfs/{int(year)}/{docid}.pdf"
    out=[]
    try:
        raw=http(url).content
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    if not table or len(table)<2:
                        continue
                    # Normalize every cell. House PDFs can contain line breaks inside Asset cells.
                    rows=[["" if c is None else re.sub(r"\s+"," ",str(c)).strip() for c in row] for row in table]
                    header=[c.lower() for c in rows[0]]
                    def col(*needles):
                        for i,h in enumerate(header):
                            if any(n in h for n in needles):
                                return i
                        return None
                    ia=col("asset"); it=col("transaction type"); idt=col("date")
                    ind=col("notification date"); iam=col("amount")
                    # "date" can accidentally resolve to Notification Date, so locate exact-ish Date separately.
                    date_candidates=[i for i,h in enumerate(header) if h=="date" or h.endswith(" date")]
                    if len(date_candidates)>=2:
                        idt=date_candidates[0]; ind=date_candidates[1]
                    if None in (ia,it,idt,ind,iam):
                        continue
                    for row in rows[1:]:
                        if len(row)<=max(ia,it,idt,ind,iam):
                            continue
                        asset=row[ia]; tx=row[it]; td=row[idt]; nd=row[ind]; amount=row[iam]
                        # Ignore description/subholding continuation rows.
                        if not asset or not re.match(r"^(P|S|E)",tx.strip(),re.I):
                            continue
                        if not re.search(r"\d{1,2}/\d{1,2}/\d{4}",td):
                            continue
                        lo,hi=amount_range(amount)
                        out.append((str(docid),name,"House",ticker_from_asset(asset),asset,tx,td,nd,
                                    amount,lo,hi,url))
    except Exception:
        return []
    return out

def refresh_house(year,limit=150):
    idx=house_index(year).sort_values("FilingDate",ascending=False).head(limit)
    c=cx()
    bootstrap_import_keys(c)
    filings_checked=0
    rows_detected=0
    inserted=0
    duplicates=0
    parse_empty=0

    for _,r in idx.iterrows():
        filings_checked += 1
        rows=parse_ptr(r.DocID,f'{r["First"]} {r["Last"]}'.strip(),year)
        if not rows:
            parse_empty += 1
            continue

        # De-dupe within a single PDF too.
        unique_rows={}
        for row in rows:
            unique_rows[stable_tx_key(row)] = row
        rows_detected += len(unique_rows)

        for k,row in unique_rows.items():
            exists=c.execute("SELECT 1 FROM import_keys WHERE tx_key=?",(k,)).fetchone()
            if exists:
                duplicates += 1
                continue
            c.execute("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",row)
            c.execute("INSERT OR IGNORE INTO import_keys(tx_key,filing_id,created) VALUES(?,?,?)",
                      (k,str(row[0]),str(datetime.now())))
            inserted += 1

    c.commit();c.close()
    return {
        "filings_checked":filings_checked,
        "rows_detected":rows_detected,
        "inserted":inserted,
        "duplicates":duplicates,
        "empty_filings":parse_empty
    }

@st.cache_data(ttl=86400)
def ticker_map():
    j=http(f"{SEC_WWW}/files/company_tickers.json").json()
    return {v["ticker"].upper():str(v["cik_str"]).zfill(10) for v in j.values()}

def latest_fact(f,names,units=("USD","shares")):
    for name in names:
        node=f.get("us-gaap",{}).get(name,{}).get("units",{})
        for unit in units:
            vals=[x for x in node.get(unit,[]) if x.get("form") in ("10-K","10-Q") and x.get("val") is not None]
            if vals:
                vals.sort(key=lambda x:(x.get("filed",""),x.get("end","")));return float(vals[-1]["val"])
    return None


TICKER_ALIASES = {
    # Common class-share notation differences between disclosure text and SEC ticker list.
    "BRK.B": "BRK-B",
    "BRK/B": "BRK-B",
    "MOG.A": "MOG-A",
    "MOG/A": "MOG-A",
}

def normalized_sec_ticker(t):
    x=str(t or "").strip().upper()
    return TICKER_ALIASES.get(x,x)

def classify_unmapped_ticker(t):
    x=str(t or "").strip().upper()
    if x in TICKER_ALIASES:
        return "share-class symbol normalization"
    if x.endswith(("Y","F")) and len(x)>=4:
        return "possible ADR / OTC / foreign security"
    if "." in x or "/" in x:
        return "share-class or symbol-format mapping"
    return "not present in current SEC ticker map"

def sec_diagnostics(tickers,refresh_days=30):
    raw=sorted(set(str(x).strip().upper() for x in tickers
                   if pd.notna(x) and str(x).strip().upper() not in ("","NONE","NAN")))
    mp=ticker_map()

    mapped={}
    unmapped=[]
    for original in raw:
        candidate=normalized_sec_ticker(original)
        if candidate in mp:
            mapped[original]=candidate
        else:
            unmapped.append(original)

    c=cx()
    existing=pd.read_sql("SELECT ticker,sec_updated FROM fundamentals",c)
    status=pd.read_sql("SELECT item,status,last_error,updated FROM enrichment_status WHERE kind='SEC'",c)
    c.close()

    existing_map={}
    for _,r in existing.iterrows():
        try: existing_map[str(r.ticker).upper()]=pd.Timestamp(r.sec_updated)
        except Exception: pass

    unavailable=set()
    for _,r in status.iterrows():
        if str(r.status).lower()=="unavailable":
            unavailable.add(str(r.item).upper())

    now=pd.Timestamp.now()
    fresh=[]; opened=[]; unavailable_raw=[]
    for original,candidate in mapped.items():
        if original in unavailable or candidate in unavailable:
            unavailable_raw.append(original)
            continue
        old=existing_map.get(original)
        if old is None:
            old=existing_map.get(candidate)
        if old is not None and (now-old).days < refresh_days:
            fresh.append(original)
        else:
            opened.append(original)

    return {
        "raw":raw,
        "mapped":mapped,
        "compatible":list(mapped.keys()),
        "unmapped":unmapped,
        "fresh":fresh,
        "open":opened,
        "unavailable":unavailable_raw
    }

def refresh_sec(tickers,batch_size=20,refresh_days=30,progress=None):
    diag=sec_diagnostics(tickers,refresh_days)
    mp=ticker_map(); c=cx()
    todo=diag["open"][:batch_size]
    ok=0; unavailable_now=[]; errors=[]

    for i,original in enumerate(todo,1):
        sec_ticker=diag["mapped"][original]
        cik=mp[sec_ticker]
        try:
            j=http(f"{SEC}/api/xbrl/companyfacts/CIK{cik}.json").json()
            f=j.get("facts",{})
            row=(original,cik,j.get("entityName"),
                 latest_fact(f,["Assets"]),
                 latest_fact(f,["Liabilities"]),
                 latest_fact(f,["Revenues","RevenueFromContractWithCustomerExcludingAssessedTax","SalesRevenueNet"]),
                 latest_fact(f,["NetIncomeLoss","ProfitLoss"]),
                 latest_fact(f,["StockholdersEquity"]),
                 latest_fact(f,["EntityCommonStockSharesOutstanding"],("shares",)),
                 str(datetime.now()))
            c.execute("INSERT OR REPLACE INTO fundamentals VALUES(?,?,?,?,?,?,?,?,?,?)",row)
            c.execute("""INSERT INTO enrichment_status(kind,item,status,attempts,last_error,updated)
                         VALUES('SEC',?,'ok',1,NULL,?)
                         ON CONFLICT(kind,item) DO UPDATE SET status='ok',
                         attempts=enrichment_status.attempts+1,last_error=NULL,updated=excluded.updated""",
                      (original,str(datetime.now())))
            c.commit(); ok+=1
        except requests.HTTPError as e:
            status_code=getattr(e.response,"status_code",None)
            msg=f"HTTPError {status_code}: {str(e)}"[:500]
            if status_code==404:
                c.execute("""INSERT INTO enrichment_status(kind,item,status,attempts,last_error,updated)
                             VALUES('SEC',?,'unavailable',1,?,?)
                             ON CONFLICT(kind,item) DO UPDATE SET status='unavailable',
                             attempts=enrichment_status.attempts+1,last_error=excluded.last_error,updated=excluded.updated""",
                          (original,msg,str(datetime.now())))
                unavailable_now.append((original,msg))
            else:
                c.execute("""INSERT INTO enrichment_status(kind,item,status,attempts,last_error,updated)
                             VALUES('SEC',?,'error',1,?,?)
                             ON CONFLICT(kind,item) DO UPDATE SET status='error',
                             attempts=enrichment_status.attempts+1,last_error=excluded.last_error,updated=excluded.updated""",
                          (original,msg,str(datetime.now())))
                errors.append((original,msg))
            c.commit()
        except Exception as e:
            msg=f"{type(e).__name__}: {str(e)}"[:500]
            c.execute("""INSERT INTO enrichment_status(kind,item,status,attempts,last_error,updated)
                         VALUES('SEC',?,'error',1,?,?)
                         ON CONFLICT(kind,item) DO UPDATE SET status='error',
                         attempts=enrichment_status.attempts+1,last_error=excluded.last_error,updated=excluded.updated""",
                      (original,msg,str(datetime.now())))
            c.commit(); errors.append((original,msg))

        if progress:
            progress.progress(i/max(1,len(todo)),text=f"SEC {i}/{len(todo)}: {original}")
        time.sleep(.15)

    c.close()
    st.cache_data.clear()
    after=sec_diagnostics(tickers,refresh_days)

    # Defensive terminal-state reconciliation: anything marked unavailable in SQLite
    # must never remain in the open count, even if a cached upstream ticker map changes.
    c2=cx()
    terminal_rows=c2.execute(
        "SELECT item FROM enrichment_status WHERE kind='SEC' AND status='unavailable'"
    ).fetchall()
    c2.close()
    terminal={str(r[0]).upper() for r in terminal_rows}
    after["open"]=[t for t in after["open"] if str(t).upper() not in terminal]
    after["unavailable"]=sorted(set(after["unavailable"]) | terminal)

    return {
        "unique_tickers":len(diag["raw"]),
        "sec_compatible":len(diag["compatible"]),
        "already_fresh_before":len(diag["fresh"]),
        "unavailable_before":len(diag["unavailable"]),
        "open_before":len(diag["open"]),
        "processed":len(todo),
        "saved":ok,
        "unavailable_now":unavailable_now,
        "failed":len(errors),
        "remaining":len(after["open"]),
        "unmapped":diag["unmapped"],
        "unmapped_classification":[(t,classify_unmapped_ticker(t)) for t in diag["unmapped"]],
        "unavailable_total":after["unavailable"],
        "errors":errors
    }

@st.cache_data(ttl=21600)
def prices(ticker,start,end):
    return yf.download(ticker,start=start,end=end,auto_adjust=True,progress=False)

def close(ticker,start,end):
    p=prices(ticker,start,end)
    if p.empty:return pd.Series(dtype=float)
    q=p["Close"];q=q.iloc[:,0] if isinstance(q,pd.DataFrame) else q
    q.index=pd.to_datetime(q.index).tz_localize(None) if getattr(q.index,"tz",None) else pd.to_datetime(q.index)
    return q.dropna()

def backtest(ticker,report,benchmark="SPY"):
    rd=pd.Timestamp(report).normalize();end=rd+pd.Timedelta(days=210)
    s=close(ticker,rd-pd.Timedelta(days=2),end);b=close(benchmark,rd-pd.Timedelta(days=2),end)
    sa=s[s.index>rd];ba=b[b.index>rd]
    if sa.empty or ba.empty:return None
    ed=max(sa.index[0],ba.index[0]);sa=s[s.index>=ed];ba=b[b.index>=ed]
    ep=float(sa.iloc[0]);bp=float(ba.iloc[0]);z={"entry_date":str(ed.date()),"entry_price":ep}
    for h in (30,90,180):
        ss=sa[sa.index>=ed+pd.Timedelta(days=h)];bb=ba[ba.index>=ed+pd.Timedelta(days=h)]
        sr=None if ss.empty else float(ss.iloc[0]/ep-1);br=None if bb.empty else float(bb.iloc[0]/bp-1)
        z[f"r{h}"]=sr;z[f"b{h}"]=br;z[f"excess{h}"]=None if sr is None or br is None else sr-br
    return z

def _bt_key(r):
    return (str(r.filing_id),str(r.chamber),str(r.ticker),str(pd.Timestamp(r.notification_date).date()))

def backtest_queue():
    d=load(False)
    d=d[d.transaction.astype(str).str.upper().str.startswith("P")].dropna(subset=["ticker","notification_date"])
    c=cx()
    done=pd.read_sql("SELECT filing_id,chamber,ticker,notification_date FROM backtests",c)
    unavailable=pd.read_sql("SELECT item FROM enrichment_status WHERE kind='BACKTEST' AND status='unavailable'",c)
    c.close()
    done_keys={(str(r.filing_id),str(r.chamber),str(r.ticker),str(pd.Timestamp(r.notification_date).date())) for _,r in done.iterrows()}
    unavailable_keys=set(unavailable.item.astype(str).tolist()) if len(unavailable) else set()
    candidates=[]
    for _,r in d.sort_values("notification_date",ascending=False).iterrows():
        k=_bt_key(r); ks="|".join(k)
        if k not in done_keys and ks not in unavailable_keys:candidates.append(r)
    return candidates

def run_backtests(maxrows=25,progress=None):
    candidates=backtest_queue();todo=candidates[:maxrows]
    c=cx();n=0;failed=0
    for i,r in enumerate(todo,1):
        k=_bt_key(r);ks="|".join(k)
        err=None
        try:z=backtest(r.ticker,r.notification_date)
        except Exception as e:z=None;err=f"{type(e).__name__}: {e}"[:500]
        if z:
            c.execute("INSERT OR REPLACE INTO backtests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (r.filing_id,r.chamber,r.ticker,str(r.notification_date.date()),z["entry_date"],z["entry_price"],
               z["r30"],z["r90"],z["r180"],z["b30"],z["b90"],z["b180"],z["excess30"],z["excess90"],z["excess180"],str(datetime.now())))
            c.execute("""INSERT INTO enrichment_status(kind,item,status,attempts,last_error,updated)
                         VALUES('BACKTEST',?,'ok',1,NULL,?)
                         ON CONFLICT(kind,item) DO UPDATE SET status='ok',attempts=enrichment_status.attempts+1,last_error=NULL,updated=excluded.updated""",
                      (ks,str(datetime.now())))
            c.commit();n+=1
        else:
            msg=err or "No usable ticker/benchmark price series after public notification date."
            c.execute("""INSERT INTO enrichment_status(kind,item,status,attempts,last_error,updated)
                         VALUES('BACKTEST',?,'unavailable',1,?,?)
                         ON CONFLICT(kind,item) DO UPDATE SET status='unavailable',attempts=enrichment_status.attempts+1,last_error=excluded.last_error,updated=excluded.updated""",
                      (ks,msg,str(datetime.now())))
            c.commit();failed+=1
        if progress:progress.progress(i/max(1,len(todo)),text=f"Backtest {i}/{len(todo)}: {r.ticker}")
    c.close();st.cache_data.clear()
    return {"processed":len(todo),"saved":n,"failed":failed,"remaining":max(0,len(candidates)-len(todo))}

def run_backfill(batch_size=25,max_batches=8,progress=None):
    total_saved=0;total_unavailable=0;batches=0
    for b in range(max_batches):
        before=len(backtest_queue())
        if before==0:break
        r=run_backtests(batch_size)
        total_saved+=r["saved"];total_unavailable+=r["failed"];batches+=1
        after=len(backtest_queue())
        if progress:
            progress.progress((b+1)/max_batches,text=f"Backfill batch {b+1}: {after} remaining")
        if after>=before:break
    return {"batches":batches,"saved":total_saved,"failed":total_unavailable,"remaining":len(backtest_queue())}

def backup_database(target=None):
    """Create a consistent SQLite snapshot after a successful refresh."""
    target=target or os.getenv("POLITICIAN_DB_BACKUP","politician_trades_backup.db")
    src=cx()
    dst=sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close();src.close()
    return target

def run_daily_pipeline(house_limit=150,sec_batch_size=20,sec_max_batches=20,backtest_batch_size=25,backtest_max_batches=20):
    """Run the same incremental pipeline without UI button interaction."""
    summary={"started":str(datetime.now())}
    summary["house"]=refresh_house(datetime.now().year,limit=house_limit)

    raw=load(False)
    tickers=raw.ticker.dropna().astype(str).tolist() if len(raw) else []
    sec_saved=sec_unavailable=sec_failed=0
    sec_batches=0
    sec_remaining=0
    for _ in range(sec_max_batches):
        r=refresh_sec(tickers,batch_size=sec_batch_size)
        sec_batches+=1
        sec_saved+=r["saved"];sec_unavailable+=len(r["unavailable_now"]);sec_failed+=r["failed"]
        sec_remaining=r["remaining"]
        if sec_remaining==0 or (r["processed"]==0 and r["failed"]==0):break
        if r["failed"] and r["saved"]==0 and not r["unavailable_now"]:break
    summary["sec"]={"batches":sec_batches,"saved":sec_saved,"unavailable":sec_unavailable,
                    "failed":sec_failed,"remaining":sec_remaining}

    bt=run_backfill(batch_size=backtest_batch_size,max_batches=backtest_max_batches)
    summary["backtests"]=bt
    summary["backup"]=backup_database()
    summary["finished"]=str(datetime.now())
    return summary

def load(with_f=True):
    c=cx();t=pd.read_sql("SELECT * FROM trades",c);b=pd.read_sql("SELECT * FROM backtests",c);f=pd.read_sql("SELECT * FROM fundamentals",c);c.close()
    for z in ("trade_date","notification_date"):
        if z in t:t[z]=pd.to_datetime(t[z],errors="coerce")
    if len(t):
        t["lag_days"]=(t.notification_date-t.trade_date).dt.days;t["est_amount"]=(t.amount_low+t.amount_high)/2
    if len(b):
        b.notification_date=pd.to_datetime(b.notification_date,errors="coerce")
        t=t.merge(b,on=["filing_id","chamber","ticker","notification_date"],how="left")
    if with_f and len(f):t=t.merge(f,on="ticker",how="left")
    return t

def quality(r):
    score=100;issues=[]
    if pd.isna(r.get("ticker")):score-=30;issues.append("no ticker")
    if pd.isna(r.get("trade_date")):score-=25;issues.append("trade date")
    if pd.isna(r.get("notification_date")):score-=25;issues.append("report date")
    if pd.isna(r.get("amount_low")):score-=10;issues.append("amount")
    if not str(r.get("source_url","")).startswith("http"):score-=10;issues.append("source")
    if pd.notna(r.get("lag_days")) and (r.lag_days<0 or r.lag_days>90):score-=15;issues.append("lag check")
    return max(0,score),", ".join(issues) if issues else "OK"

def add_watch(kind,value):
    c=cx();c.execute("INSERT OR IGNORE INTO watchlist VALUES(?,?,?)",(kind,value,str(datetime.now())));c.commit();c.close()

def watch():
    c=cx();w=pd.read_sql("SELECT * FROM watchlist",c);c.close();return w

def add_rule(name,min_amount,max_lag,cluster,rev):
    c=cx();c.execute("INSERT INTO alert_rules(name,min_amount,max_lag,cluster_min,small_company_revenue,enabled) VALUES(?,?,?,?,?,1)",
                     (name,min_amount,max_lag,cluster,rev));c.commit();c.close()

def rules():
    c=cx();r=pd.read_sql("SELECT * FROM alert_rules",c);c.close();return r

def cluster_counts(x):
    out=[]
    for _,r in x.iterrows():
        if pd.isna(r.notification_date):out.append(1);continue
        w=x[(x.ticker==r.ticker)&(x.notification_date>=r.notification_date-pd.Timedelta(days=30))&
            (x.notification_date<=r.notification_date+pd.Timedelta(days=30))]
        out.append(w.politician.nunique())
    return out

if os.getenv("HEADLESS_REFRESH")=="1":
    result=run_daily_pipeline()
    print(result)
else:
    st.title("Politician Trade Research V7.0")
    st.caption("Mobile-ready research dashboard • data quality • watchlist • alert rules • backtests")

    with st.sidebar:
        page=st.radio("View",["Home","Discover","Watchlist","Alerts","Backtests","Analytics","Politicians","Companies","Data Quality","Sources"])
        st.divider()
        if st.button("Run full refresh now"):
            with st.spinner("Running House → SEC → backtests…"):
                r=run_daily_pipeline()
            st.success(
                f'Full refresh complete • House +{r["house"]["inserted"]} • '
                f'SEC +{r["sec"]["saved"]} • Backtests +{r["backtests"]["saved"]}'
            )
            st.caption(f'Database snapshot: {r["backup"]}')
        if st.button("Refresh House"):
            with st.spinner("Checking official House filings…"):
                result=refresh_house(datetime.now().year)
            st.success(
                f'{result["filings_checked"]} filings checked • '
                f'{result["rows_detected"]} transactions detected • '
                f'{result["inserted"]} new • '
                f'{result["duplicates"]} already stored'
            )
            if result["empty_filings"]:
                st.caption(f'{result["empty_filings"]} filings returned no parseable transaction table and remain a data-quality check.')
        if st.button("Refresh SEC (next batch)"):
            d0=load(False)
            ticker_list=d0.ticker.dropna().tolist() if len(d0) else []
            bar=st.progress(0,text="Preparing SEC batch…")
            r=refresh_sec(ticker_list,batch_size=20,progress=bar)
            bar.empty()

            st.success(
                f'{r["saved"]} saved • {len(r["unavailable_now"])} marked unavailable • '
                f'{r["failed"]} retryable failed • {r["remaining"]} still open'
            )
            st.caption(
                f'{r["unique_tickers"]} unique parsed tickers • '
                f'{r["sec_compatible"]} SEC-mapped • '
                f'{r["already_fresh_before"]} already fresh • '
                f'{r["unavailable_before"]} already unavailable • '
                f'{r["open_before"]} open before batch'
            )

            if r["unmapped_classification"]:
                with st.expander(f'Unmapped tickers ({len(r["unmapped_classification"])})'):
                    for ticker,reason in r["unmapped_classification"]:
                        st.write(f"**{ticker}** — {reason}")

            if r["unavailable_total"]:
                with st.expander(f'SEC fundamentals unavailable ({len(r["unavailable_total"])})'):
                    st.write(", ".join(r["unavailable_total"]))

            if r["errors"]:
                with st.expander(f'Retryable SEC errors ({len(r["errors"])})',expanded=True):
                    for ticker,msg in r["errors"]:
                        st.error(f"{ticker}: {msg}")

            if r["remaining"]>0:
                st.caption("Press again later to continue. Saved and permanently unavailable companies are skipped.")
        if st.button("Update backtests (next batch)"):
            bar=st.progress(0,text="Preparing backtest batch…");r=run_backtests(25,progress=bar);bar.empty()
            st.success(f'{r["saved"]} backtests saved • {r["failed"]} unavailable • {r["remaining"]} remaining')
            if r["remaining"]>0: st.caption("Press again later to continue. Existing backtests are skipped.")
        if st.button("Backfill remaining backtests"):
            bar=st.progress(0,text="Starting controlled backfill…")
            r=run_backfill(batch_size=25,max_batches=8,progress=bar);bar.empty()
            st.success(f'{r["saved"]} saved • {r["failed"]} unavailable • {r["remaining"]} remaining • {r["batches"]} batches')
            if r["remaining"]>0:st.caption("Run again later to continue. Completed and unavailable cases are skipped.")

    d=load()

    if page=="Home":
        if d.empty:st.info("Open the sidebar and refresh House data first.")
        else:
            buys=d[d.transaction.astype(str).str.upper().str.startswith("P")].copy()
            c1,c2,c3=st.columns(3);c1.metric("Disclosures",len(d));c2.metric("Purchases",len(buys));c3.metric("Politicians",d.politician.nunique())
            st.subheader("Newest public purchase disclosures")
            cols=[x for x in ["notification_date","politician","ticker","company","amount","lag_days","source_url"] if x in buys]
            st.dataframe(buys[cols].sort_values("notification_date",ascending=False).head(15),
              column_config={"source_url":st.column_config.LinkColumn("Filing")},use_container_width=True,hide_index=True)

    elif page=="Discover":
        if len(d):
            x=d[d.transaction.astype(str).str.upper().str.startswith("P")].dropna(subset=["ticker"]).copy()
            x["cluster30"]=cluster_counts(x)
            qs=x.apply(quality,axis=1);x["quality"]=[a for a,b in qs];x["quality_note"]=[b for a,b in qs]
            minq=st.slider("Minimum data quality",0,100,70)
            x=x[x.quality>=minq]
            st.dataframe(x.sort_values(["cluster30","notification_date"],ascending=False),use_container_width=True,hide_index=True)
            t=st.selectbox("Add ticker to watchlist",[""]+sorted(x.ticker.unique().tolist()))
            if t and st.button("Watch ticker"):add_watch("ticker",t);st.success(f"{t} added.")

    elif page=="Watchlist":
        w=watch()
        if w.empty:st.info("No watchlist items yet.")
        else:
            st.dataframe(w,use_container_width=True,hide_index=True)
            if len(d):
                tickers=w[w.kind=="ticker"].value.tolist()
                x=d[d.ticker.isin(tickers)]
                st.subheader("Matching disclosures")
                st.dataframe(x.sort_values("notification_date",ascending=False),use_container_width=True,hide_index=True)

    elif page=="Alerts":
        st.subheader("Saved research rules")
        with st.form("rule"):
            name=st.text_input("Rule name","Large / fast / clustered purchase")
            amt=st.number_input("Minimum estimated amount",0,10000000,100000,10000)
            lag=st.number_input("Maximum reporting lag (days)",0,90,20)
            clu=st.number_input("Minimum politicians in 30-day ticker cluster",1,20,2)
            rev=st.number_input("Maximum company revenue ($, 0 = ignore)",0,100000000000,2000000000,100000000)
            if st.form_submit_button("Save rule"):add_rule(name,amt,lag,clu,rev);st.success("Rule saved.")
        r=rules();st.dataframe(r,use_container_width=True,hide_index=True)
        if len(d) and len(r):
            x=d[d.transaction.astype(str).str.upper().str.startswith("P")].dropna(subset=["ticker"]).copy();x["cluster30"]=cluster_counts(x)
            matches=[]
            for _,rule in r[r.enabled==1].iterrows():
                q=x[(x.est_amount.fillna(0)>=rule.min_amount)&(x.lag_days.fillna(999)<=rule.max_lag)&(x.cluster30>=rule.cluster_min)]
                if rule.small_company_revenue>0:q=q[q.revenue.fillna(np.inf)<=rule.small_company_revenue]
                if len(q):
                    z=q.copy();z["matched_rule"]=rule["name"];matches.append(z)
            if matches:
                st.subheader("Current matches");st.dataframe(pd.concat(matches).sort_values("notification_date",ascending=False),use_container_width=True,hide_index=True)

    elif page=="Backtests":
        if len(d):
            x=d[d.transaction.astype(str).str.upper().str.startswith("P")].copy()

            st.subheader("Historical performance after public disclosure")
            st.caption(
                "Signal = public notification date. The simulated entry is the first common trading day after "
                "the disclosure. Returns use adjusted historical closing prices. SPY is the market benchmark."
            )

            h=st.selectbox("Holding period",[30,90,180],index=1,
                           format_func=lambda v:f"{v} days after simulated entry")
            who=st.selectbox("Politician",["All"]+sorted(x.politician.dropna().unique().tolist()))
            if who!="All":x=x[x.politician==who]

            rc=f"r{h}";bc=f"b{h}";ec=f"excess{h}"
            y=x.dropna(subset=[rc]).copy()
            pending=max(0,len(x)-len(y))

            c1,c2,c3,c4=st.columns(4)
            c1.metric("Completed (N)",len(y),help="Number of transactions with enough elapsed time and usable price data for this horizon.")
            c2.metric("Positive return",f"{(y[rc]>0).mean():.1%}" if len(y) else "—",
                      help="Share of completed cases where the stock return was above 0%.")
            c3.metric("Outperformed SPY",f"{(y[ec]>0).mean():.1%}" if len(y) else "—",
                      help="Share of completed cases where the stock return was higher than SPY over the same period.")
            c4.metric("Median vs. SPY",f"{y[ec].median():.1%}" if len(y) else "—",
                      help="Median stock return minus SPY return. Positive means above the benchmark; negative means below it.")

            st.caption(f"Pending / not available for {h} days: {pending}")

            show=y.sort_values("notification_date",ascending=False).copy()
            rename={
                "filing_id":"Filing ID","politician":"Politician","chamber":"Chamber","ticker":"Ticker",
                "asset":"Asset","trade_date":"Trade date","notification_date":"Public notification",
                "entry_date":"Simulated entry","entry_price":"Adjusted entry price",
                rc:f"Stock return {h}d",bc:f"SPY return {h}d",ec:f"Excess vs SPY {h}d"
            }
            preferred=["filing_id","politician","chamber","ticker","asset","trade_date","notification_date",
                       "entry_date","entry_price",rc,bc,ec]
            cols=[c for c in preferred if c in show.columns]
            table=show[cols].rename(columns=rename)
            for c in [f"Stock return {h}d",f"SPY return {h}d",f"Excess vs SPY {h}d"]:
                if c in table.columns:
                    table[c]=table[c].apply(lambda v:"—" if pd.isna(v) else f"{v:.2%}")
            st.dataframe(
                table,use_container_width=True,hide_index=True,
                column_config={"Adjusted entry price":st.column_config.NumberColumn(format="$%.4f")}
            )

            st.subheader("Validation sample")
            st.caption(
                "Use this table to audit individual calculations. 'Pending' means the required horizon has not "
                "yet produced a usable observation; it is not counted as a completed result."
            )
            audit_cols=[c for c in ["politician","ticker","trade_date","notification_date","entry_date","entry_price",
                                    "r30","b30","excess30","r90","b90","excess90","r180","b180","excess180"] if c in x.columns]
            sample=x.sort_values("notification_date",ascending=False)[audit_cols].head(20).copy()
            audit_rename={
                "politician":"Politician","ticker":"Ticker","trade_date":"Trade date",
                "notification_date":"Public notification","entry_date":"Simulated entry",
                "entry_price":"Adjusted entry price",
                "r30":"Stock 30d","b30":"SPY 30d","excess30":"Vs SPY 30d",
                "r90":"Stock 90d","b90":"SPY 90d","excess90":"Vs SPY 90d",
                "r180":"Stock 180d","b180":"SPY 180d","excess180":"Vs SPY 180d"
            }
            sample=sample.rename(columns=audit_rename)
            for c in ["Stock 30d","SPY 30d","Vs SPY 30d","Stock 90d","SPY 90d","Vs SPY 90d",
                      "Stock 180d","SPY 180d","Vs SPY 180d"]:
                if c in sample.columns:
                    sample[c]=sample[c].apply(lambda v:"Pending" if pd.isna(v) else f"{v:.2%}")
            st.dataframe(sample,use_container_width=True,hide_index=True)

            with st.expander("How to read these numbers"):
                st.markdown("""
    **Trade date** = date of the politician's reported transaction.  
    **Public notification** = date the disclosure became public in our dataset.  
    **Simulated entry** = first common trading day after the public notification.  
    **Stock return** = historical stock performance after the simulated entry.  
    **SPY return** = performance of the SPDR S&P 500 ETF over the same horizon.  
    **Excess vs SPY** = stock return minus SPY return.  
    **Pending** = the horizon is not yet complete or a usable observation is not available.

    These are historical, descriptive results and do not establish that a disclosure caused the later price movement.
    """)

    elif page=="Analytics":
        st.subheader("Analytics")
        st.caption(
            "Descriptive analysis of publicly disclosed House transactions. Returns are historical results after "
            "public notification; disclosed dollar amounts are ranges, not exact investment amounts."
        )
        if len(d):
            buys=d[d.transaction.astype(str).str.upper().str.startswith("P")].copy()
            buys=buys.dropna(subset=["politician"])

            tab1,tab2,tab3,tab4,tab5=st.tabs([
                "Transactions","Disclosed volume","By politician","Largest purchases","Reporting lag"
            ])

            with tab1:
                st.markdown("### Historical transaction outcomes")
                horizon=st.selectbox("Return horizon",[30,90,180],index=1,key="analytics_h")
                metric=st.selectbox("Sort by",["Stock return","Excess vs SPY"],key="analytics_metric")
                rc=f"r{horizon}";bc=f"b{horizon}";ec=f"excess{horizon}"
                q=buys.dropna(subset=[rc]).copy()
                sortcol=rc if metric=="Stock return" else ec
                q=q.sort_values(sortcol,ascending=False)
                cols=[c for c in ["politician","ticker","asset","trade_date","notification_date","lag_days",
                                   "amount_low","amount_high","est_amount",rc,bc,ec] if c in q.columns]
                q=q[cols].rename(columns={
                    "politician":"Politician","ticker":"Ticker","asset":"Asset","trade_date":"Trade date",
                    "notification_date":"Public notification","lag_days":"Reporting lag (days)",
                    "amount_low":"Disclosed min","amount_high":"Disclosed max","est_amount":"Estimated midpoint",
                    rc:f"Stock {horizon}d",bc:f"SPY {horizon}d",ec:f"Vs SPY {horizon}d"
                })
                for c in [f"Stock {horizon}d",f"SPY {horizon}d",f"Vs SPY {horizon}d"]:
                    if c in q:q[c]=q[c].apply(lambda v:"—" if pd.isna(v) else f"{v:.2%}")
                st.dataframe(q,use_container_width=True,hide_index=True)
                st.caption(f"Completed observations for {horizon} days: {len(q)}. Sort order is descriptive, not a forecast.")

            with tab2:
                st.markdown("### Disclosed purchase volume by politician")
                v=buys.dropna(subset=["amount_low","amount_high"]).copy()
                if len(v):
                    g=v.groupby("politician",as_index=False).agg(
                        purchases=("ticker","size"),
                        disclosed_min=("amount_low","sum"),
                        disclosed_max=("amount_high","sum"),
                        estimated_midpoint=("est_amount","sum")
                    ).sort_values("estimated_midpoint",ascending=False)
                    g=g.rename(columns={
                        "politician":"Politician","purchases":"Purchases",
                        "disclosed_min":"Sum of disclosed minima",
                        "disclosed_max":"Sum of disclosed maxima",
                        "estimated_midpoint":"Estimated midpoint total"
                    })
                    for c in ["Sum of disclosed minima","Sum of disclosed maxima","Estimated midpoint total"]:
                        g[c]=g[c].apply(lambda v:f"${v:,.0f}")
                    st.dataframe(g,use_container_width=True,hide_index=True)
                    st.info("The midpoint is only an estimate derived from each disclosed amount range. It is not the politician's exact invested amount.")
                else: st.info("No disclosed amount ranges available.")

            with tab3:
                st.markdown("### Historical results by politician")
                h2=st.selectbox("Evaluation horizon",[30,90,180],index=1,key="analytics_pol_h")
                r=f"r{h2}";e=f"excess{h2}"
                z=buys.dropna(subset=[r,e]).copy()
                if len(z):
                    rows=[]
                    for name,gp in z.groupby("politician"):
                        rows.append({
                            "Politician":name,
                            "Completed N":len(gp),
                            f"Median stock {h2}d":gp[r].median(),
                            f"Median vs SPY {h2}d":gp[e].median(),
                            "Positive cases":(gp[r]>0).mean(),
                            "Cases above SPY":(gp[e]>0).mean()
                        })
                    p=pd.DataFrame(rows).sort_values(["Completed N","Politician"],ascending=[False,True])
                    for c in [f"Median stock {h2}d",f"Median vs SPY {h2}d","Positive cases","Cases above SPY"]:
                        p[c]=p[c].apply(lambda v:f"{v:.1%}")
                    st.dataframe(p,use_container_width=True,hide_index=True)
                    st.caption("Completed N is shown so results from a small number of disclosures are not confused with larger samples.")
                else: st.info("No completed observations for this horizon.")

            with tab4:
                st.markdown("### Largest disclosed individual purchases")
                q=buys.dropna(subset=["amount_low","amount_high"]).copy()
                if len(q):
                    q=q.sort_values(["est_amount","notification_date"],ascending=[False,False])
                    cols=[c for c in ["politician","ticker","asset","trade_date","notification_date",
                                       "amount_low","amount_high","est_amount","r30","r90","r180"] if c in q.columns]
                    q=q[cols].rename(columns={
                        "politician":"Politician","ticker":"Ticker","asset":"Asset","trade_date":"Trade date",
                        "notification_date":"Public notification","amount_low":"Disclosed min",
                        "amount_high":"Disclosed max","est_amount":"Estimated midpoint",
                        "r30":"Stock 30d","r90":"Stock 90d","r180":"Stock 180d"
                    })
                    for c in ["Stock 30d","Stock 90d","Stock 180d"]:
                        if c in q:q[c]=q[c].apply(lambda v:"Pending" if pd.isna(v) else f"{v:.2%}")
                    for c in ["Disclosed min","Disclosed max","Estimated midpoint"]:
                        q[c]=q[c].apply(lambda v:f"${v:,.0f}")
                    st.dataframe(q,use_container_width=True,hide_index=True)
                    st.caption("Ordered by the estimated midpoint of the disclosed range; exact purchase amounts are generally not disclosed.")

            with tab5:
                st.markdown("### Reporting lag")
                q=buys.dropna(subset=["trade_date","notification_date","lag_days"]).copy()
                if len(q):
                    c1,c2,c3=st.columns(3)
                    c1.metric("Median lag",f"{q.lag_days.median():.0f} days")
                    c2.metric("Average lag",f"{q.lag_days.mean():.1f} days")
                    c3.metric("Observed purchases",len(q))
                    cols=[c for c in ["politician","ticker","asset","trade_date","notification_date","lag_days"] if c in q.columns]
                    q=q.sort_values("lag_days",ascending=False)[cols].rename(columns={
                        "politician":"Politician","ticker":"Ticker","asset":"Asset","trade_date":"Trade date",
                        "notification_date":"Public notification","lag_days":"Reporting lag (days)"
                    })
                    st.dataframe(q,use_container_width=True,hide_index=True)
                    st.caption("Reporting lag = public notification date minus reported transaction date.")

    elif page=="Politicians":
        if len(d):
            who=st.selectbox("Politician",sorted(d.politician.unique()));x=d[d.politician==who]
            if st.button("Watch politician"):add_watch("politician",who);st.success("Added.")
            st.dataframe(x.sort_values("notification_date",ascending=False),use_container_width=True,hide_index=True)

    elif page=="Companies":
        if len(d):
            t=st.selectbox("Ticker",sorted(d.ticker.dropna().unique()));x=d[d.ticker==t];r=x.iloc[0]
            st.subheader(f'{r.get("company") if pd.notna(r.get("company")) else t} ({t})')
            st.dataframe(x.sort_values("notification_date",ascending=False),use_container_width=True,hide_index=True)

    elif page=="Data Quality":
        if len(d):
            q=d.apply(quality,axis=1);x=d.copy();x["quality_score"]=[a for a,b in q];x["quality_note"]=[b for a,b in q]
            c1,c2,c3=st.columns(3);c1.metric("Rows",len(x));c2.metric("Median quality",f"{x.quality_score.median():.0f}/100")
            c3.metric("Rows <70",int((x.quality_score<70).sum()))
            st.dataframe(x.sort_values(["quality_score","notification_date"]),use_container_width=True,hide_index=True)

    else:
        st.markdown("""
    ### Source policy
    **House:** official U.S. House Periodic Transaction Reports are primary evidence.  
    **SEC:** public Company Facts data enriches listed-company fundamentals.  
    **Market prices:** free yfinance convenience layer, kept separate and replaceable.  
    **Senate:** intentionally remains a separate adapter until an official-source ingestion path is robust enough for automated production use.

    ### Quality policy
    Every disclosure can be checked for ticker, dates, amount band, source link and plausible reporting lag.
    A low quality score means the row needs verification; it does not mean the filing itself is false.

    ### Alerts
    Saved rules are research filters. They do not predict future returns and do not imply wrongdoing.
    Historical backtests use the first market session after public disclosure to reduce look-ahead bias.
    """)
