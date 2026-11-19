CREATE TABLE IF NOT EXISTS paper_signals(namespace TEXT NOT NULL,id TEXT NOT NULL,strategy INTEGER NOT NULL,symbol TEXT NOT NULL,bar_date TEXT NOT NULL,side TEXT NOT NULL CHECK(side IN('BUY','SELL')),status TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,eligible_date TEXT NOT NULL,PRIMARY KEY(namespace,id));
CREATE TABLE IF NOT EXISTS paper_positions(namespace TEXT NOT NULL,strategy INTEGER NOT NULL,symbol TEXT NOT NULL,qty INTEGER NOT NULL CHECK(qty>=0),entry_price REAL NOT NULL CHECK(entry_price>0),entry_at TEXT NOT NULL,high_water REAL NOT NULL CHECK(high_water>0),mark_price REAL,mark_at TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(namespace,strategy,symbol));
CREATE TABLE IF NOT EXISTS paper_fills(id INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,signal_id TEXT NOT NULL,strategy INTEGER NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,qty INTEGER NOT NULL CHECK(qty>0),price REAL NOT NULL CHECK(price>0),quote_at TEXT NOT NULL,filled_at TEXT NOT NULL,reason TEXT NOT NULL,realized_pnl REAL NOT NULL,cashflow REAL NOT NULL,UNIQUE(namespace,signal_id));
CREATE TABLE IF NOT EXISTS paper_watermarks(namespace TEXT NOT NULL,strategy INTEGER NOT NULL,symbol TEXT NOT NULL,bar_date TEXT NOT NULL,PRIMARY KEY(namespace,strategy,symbol));
CREATE TABLE IF NOT EXISTS paper_pnl(namespace TEXT NOT NULL,day TEXT NOT NULL,as_of TEXT NOT NULL,report_json TEXT NOT NULL,PRIMARY KEY(namespace,day));
CREATE TABLE IF NOT EXISTS paper_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,status TEXT NOT NULL,details_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_quote_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,symbol TEXT NOT NULL,observed_at TEXT NOT NULL,quote_at TEXT,price REAL,accepted INTEGER NOT NULL,reason TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'FYERS_QUOTES');
CREATE TABLE IF NOT EXISTS paper_data_audit(namespace TEXT NOT NULL,symbol TEXT NOT NULL,bar_date TEXT NOT NULL,reason TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(namespace,symbol,bar_date,reason));
CREATE TABLE IF NOT EXISTS paper_meta(namespace TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(namespace,key));
CREATE TABLE IF NOT EXISTS paper_outages(namespace TEXT NOT NULL,session_day TEXT NOT NULL,kind TEXT NOT NULL,detected_at TEXT NOT NULL,detail_json TEXT NOT NULL,PRIMARY KEY(namespace,session_day,kind));

CREATE INDEX IF NOT EXISTS paper_signals_pending_idx ON paper_signals(namespace,status);
CREATE INDEX IF NOT EXISTS paper_fills_day_idx ON paper_fills(namespace,filled_at);
CREATE INDEX IF NOT EXISTS paper_positions_open_idx ON paper_positions(namespace,qty);
