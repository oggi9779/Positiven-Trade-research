
import re, sqlite3, requests, time, io
import pdfplumber
from datetime import datetime
import pandas as pd
import numpy as np
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Politician Trade Research V6",layout="wide",initial_sidebar_state="collapsed")
DB="politician_trades.db"; HOUSE="https://disclosures-clerk.house.gov"
SEC="https://data.sec.gov"; SEC_WWW="https://www.sec.gov"
UA={"User-Agent":"PoliticianTradeResearch personal research contact@example.com"}

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
    c=cx();n=0;parsed=0
    for _,r in idx.iterrows():
        rows=parse_ptr(r.DocID,f'{r["First"]} {r["Last"]}'.strip(),year)
        parsed+=len(rows)
        for row in rows:
            n+=c.execute("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",row).rowcount
    c.commit();c.close()
    return n

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

def refresh_sec(tickers):
    mp=ticker_map();c=cx();n=0
    for t in sorted(set(str(x).upper() for x in tickers if pd.notna(x))):
        cik=mp.get(t)
        if not cik:continue
        try:
            j=http(f"{SEC}/api/xbrl/companyfacts/CIK{cik}.json").json();f=j.get("facts",{})
            row=(t,cik,j.get("entityName"),latest_fact(f,["Assets"]),latest_fact(f,["Liabilities"]),
                 latest_fact(f,["Revenues","RevenueFromContractWithCustomerExcludingAssessedTax","SalesRevenueNet"]),
                 latest_fact(f,["NetIncomeLoss","ProfitLoss"]),latest_fact(f,["StockholdersEquity"]),
                 latest_fact(f,["EntityCommonStockSharesOutstanding"],("shares",)),str(datetime.now()))
            c.execute("INSERT OR REPLACE INTO fundamentals VALUES(?,?,?,?,?,?,?,?,?,?)",row);n+=1;time.sleep(.12)
        except:pass
    c.commit();c.close();return n

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

def run_backtests(maxrows=300):
    d=load(False);d=d[d.transaction.astype(str).str.upper().str.startswith("P")].dropna(subset=["ticker","notification_date"])
    c=cx();n=0
    for _,r in d.sort_values("notification_date",ascending=False).head(maxrows).iterrows():
        z=backtest(r.ticker,r.notification_date)
        if not z:continue
        c.execute("INSERT OR REPLACE INTO backtests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (r.filing_id,r.chamber,r.ticker,str(r.notification_date.date()),z["entry_date"],z["entry_price"],
           z["r30"],z["r90"],z["r180"],z["b30"],z["b90"],z["b180"],z["excess30"],z["excess90"],z["excess180"],str(datetime.now())))
        n+=1
    c.commit();c.close();st.cache_data.clear();return n

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

st.title("Politician Trade Research V6")
st.caption("Mobile-ready research dashboard • data quality • watchlist • alert rules • backtests")

with st.sidebar:
    page=st.radio("View",["Home","Discover","Watchlist","Alerts","Backtests","Politicians","Companies","Data Quality","Sources"])
    st.divider()
    if st.button("Refresh House"):
        st.success(f"{refresh_house(datetime.now().year)} new transaction rows imported.")
    if st.button("Refresh SEC"):
        d0=load(False);st.success(f"{refresh_sec(d0.ticker.dropna().tolist() if len(d0) else [])} companies.")
    if st.button("Update backtests"):
        st.success(f"{run_backtests()} purchases analyzed.")

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
        x=d[d.transaction.astype(str).str.upper().str.startswith("P")]
        h=st.selectbox("Holding period",[30,90,180],index=1);who=st.selectbox("Politician",["All"]+sorted(x.politician.unique().tolist()))
        if who!="All":x=x[x.politician==who]
        rc=f"r{h}";ec=f"excess{h}";y=x.dropna(subset=[rc])
        c1,c2,c3,c4=st.columns(4);c1.metric("N",len(y))
        c2.metric("Positive",f"{(y[rc]>0).mean():.1%}" if len(y) else "—")
        c3.metric("Beat SPY",f"{(y[ec]>0).mean():.1%}" if len(y) else "—")
        c4.metric("Median excess",f"{y[ec].median():.1%}" if len(y) else "—")
        st.dataframe(y.sort_values("notification_date",ascending=False),use_container_width=True,hide_index=True)

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
