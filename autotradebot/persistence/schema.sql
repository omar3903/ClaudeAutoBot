-- AutoTradeBot MySQL schema  (generated - source of truth is autotradebot/persistence/models_orm.py)
-- Canonical setup:  python scripts/init_db.py   (creates tables + indexes; also auto-adds
--                   any new columns to an existing database on every start)
-- Manual setup:
--   CREATE DATABASE autotradebot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   CREATE USER 'autotradebot'@'%' IDENTIFIED BY 'CHANGE_ME';
--   GRANT ALL PRIVILEGES ON autotradebot.* TO 'autotradebot'@'%'; FLUSH PRIVILEGES;

USE autotradebot;

CREATE TABLE IF NOT EXISTS account_snapshots (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts DATETIME NOT NULL, 
	broker VARCHAR(16) NOT NULL, 
	equity NUMERIC(20, 6) NOT NULL, 
	cash NUMERIC(20, 6) NOT NULL, 
	buying_power NUMERIC(20, 6) NOT NULL, 
	day_trades_5d INTEGER NOT NULL, 
	open_positions INTEGER NOT NULL, 
	unrealized_pl NUMERIC(20, 6) NOT NULL, 
	realized_pl_day NUMERIC(20, 6) NOT NULL, 
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_account_snapshots_ts ON account_snapshots (ts);

CREATE TABLE IF NOT EXISTS order_audit (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts DATETIME NOT NULL, 
	play_id VARCHAR(32), 
	trade_id VARCHAR(32), 
	broker VARCHAR(16) NOT NULL, 
	action VARCHAR(24) NOT NULL, 
	request JSON, 
	response JSON, 
	ok BOOL NOT NULL, 
	message VARCHAR(400) NOT NULL, 
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_order_audit_ts ON order_audit (ts);
CREATE INDEX ix_order_audit_play_id ON order_audit (play_id);
CREATE INDEX ix_order_audit_trade_id ON order_audit (trade_id);

CREATE TABLE IF NOT EXISTS scan_runs (
	id VARCHAR(32) NOT NULL, 
	kind VARCHAR(8) NOT NULL, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	universe_size INTEGER NOT NULL, 
	scanned INTEGER NOT NULL, 
	prefiltered INTEGER NOT NULL, 
	n_plays INTEGER NOT NULL, 
	shortlist JSON, 
	n_errors INTEGER NOT NULL, 
	elapsed_s FLOAT NOT NULL, 
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_scan_runs_started_at ON scan_runs (started_at);

CREATE TABLE IF NOT EXISTS play_logs (
	id VARCHAR(32) NOT NULL, 
	scan_run_id VARCHAR(32), 
	created_at DATETIME NOT NULL, 
	symbol VARCHAR(16) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	strategy VARCHAR(48) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	timeframe VARCHAR(16) NOT NULL, 
	entry NUMERIC(20, 6) NOT NULL, 
	stop NUMERIC(20, 6) NOT NULL, 
	targets JSON, 
	reward_risk FLOAT NOT NULL, 
	confidence FLOAT NOT NULL, 
	score FLOAT NOT NULL, 
	suggested_qty INTEGER NOT NULL, 
	dollar_risk NUMERIC(20, 6) NOT NULL, 
	notional NUMERIC(20, 6) NOT NULL, 
	rationale VARCHAR(400) NOT NULL, 
	explanation TEXT NOT NULL, 
	evidence JSON, 
	tags JSON, 
	noise JSON, 
	confirmations INTEGER, 
	status VARCHAR(16) NOT NULL, 
	decided_at DATETIME, 
	decided_by VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(scan_run_id) REFERENCES scan_runs (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_play_logs_symbol ON play_logs (symbol);
CREATE INDEX ix_play_logs_scan_run_id ON play_logs (scan_run_id);
CREATE INDEX ix_play_logs_created_at ON play_logs (created_at);
CREATE INDEX ix_play_logs_score ON play_logs (score);
CREATE INDEX ix_play_logs_status ON play_logs (status);
CREATE INDEX ix_play_logs_strategy ON play_logs (strategy);

CREATE TABLE IF NOT EXISTS trades (
	id VARCHAR(32) NOT NULL, 
	play_id VARCHAR(32), 
	symbol VARCHAR(16) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	strategy VARCHAR(48) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	timeframe VARCHAR(16) NOT NULL, 
	broker VARCHAR(16) NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	quantity NUMERIC(20, 6) NOT NULL, 
	entry_price NUMERIC(20, 6) NOT NULL, 
	entry_time DATETIME, 
	order_type VARCHAR(16) NOT NULL, 
	order_session VARCHAR(12) NOT NULL, 
	stop_price NUMERIC(20, 6), 
	target_price NUMERIC(20, 6), 
	initial_stop_price NUMERIC(20, 6), 
	initial_target_price NUMERIC(20, 6), 
	hwm_price NUMERIC(20, 6), 
	managed_exit BOOL NOT NULL, 
	exit_price NUMERIC(20, 6), 
	exit_time DATETIME, 
	exit_reason VARCHAR(24) NOT NULL, 
	fees NUMERIC(20, 6) NOT NULL, 
	realized_pl NUMERIC(20, 6), 
	realized_pl_pct FLOAT, 
	r_multiple FLOAT, 
	mae NUMERIC(20, 6), 
	mfe NUMERIC(20, 6), 
	is_day_trade BOOL NOT NULL, 
	session_date DATE, 
	pair_id VARCHAR(32), 
	notes TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(play_id) REFERENCES play_logs (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_trades_play_id ON trades (play_id);
CREATE INDEX ix_trades_session_date ON trades (session_date);
CREATE INDEX ix_trades_status ON trades (status);
CREATE INDEX ix_trades_entry_time ON trades (entry_time);
CREATE INDEX ix_trades_strategy ON trades (strategy);
CREATE INDEX ix_trades_exit_time ON trades (exit_time);
CREATE INDEX ix_trades_is_day_trade ON trades (is_day_trade);
CREATE INDEX ix_trades_pair_id ON trades (pair_id);
CREATE INDEX ix_trades_symbol ON trades (symbol);

CREATE TABLE IF NOT EXISTS fills (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	trade_id VARCHAR(32) NOT NULL, 
	broker_order_id VARCHAR(48) NOT NULL, 
	ts DATETIME NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	leg VARCHAR(8) NOT NULL, 
	quantity NUMERIC(20, 6) NOT NULL, 
	price NUMERIC(20, 6) NOT NULL, 
	commission NUMERIC(20, 6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(trade_id) REFERENCES trades (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_fills_trade_id ON fills (trade_id);
CREATE INDEX ix_fills_ts ON fills (ts);

CREATE TABLE IF NOT EXISTS filings_read (
	accession VARCHAR(24) NOT NULL, 
	form VARCHAR(12) NOT NULL, 
	filed DATE, 
	trades INTEGER NOT NULL, 
	read_at DATETIME NOT NULL, 
	PRIMARY KEY (accession)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_filings_read_filed ON filings_read (filed);

CREATE TABLE IF NOT EXISTS insider_trades (
	accession VARCHAR(24) NOT NULL, 
	line INTEGER NOT NULL, 
	symbol VARCHAR(16) NOT NULL, 
	issuer_cik BIGINT NOT NULL, 
	issuer_name VARCHAR(160) NOT NULL, 
	owner_cik BIGINT NOT NULL, 
	owner_name VARCHAR(160) NOT NULL, 
	`role` VARCHAR(20) NOT NULL, 
	title VARCHAR(160) NOT NULL, 
	code VARCHAR(2) NOT NULL, 
	trade_date DATE NOT NULL, 
	shares NUMERIC(20, 6) NOT NULL, 
	price NUMERIC(20, 6) NOT NULL, 
	shares_after NUMERIC(20, 6) NOT NULL, 
	planned BOOL NOT NULL, 
	direct BOOL NOT NULL, 
	offering BOOL NOT NULL, 
	filed DATE, 
	PRIMARY KEY (accession, line)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_insider_trades_symbol ON insider_trades (symbol);
CREATE INDEX ix_insider_trades_trade_date ON insider_trades (trade_date);

CREATE TABLE IF NOT EXISTS news_items (
	`key` VARCHAR(32) NOT NULL, 
	symbol VARCHAR(16) NOT NULL, 
	source VARCHAR(12) NOT NULL, 
	provider VARCHAR(40) NOT NULL, 
	kind VARCHAR(12) NOT NULL, 
	headline VARCHAR(500) NOT NULL, 
	url VARCHAR(500) NOT NULL, 
	ref VARCHAR(80) NOT NULL, 
	items VARCHAR(60) NOT NULL, 
	published_at DATETIME NOT NULL, 
	sentiment FLOAT, 
	sentiment_conf FLOAT, 
	fetched_at DATETIME NOT NULL, 
	PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_news_items_published_at ON news_items (published_at);
CREATE INDEX ix_news_items_symbol ON news_items (symbol);

CREATE TABLE IF NOT EXISTS daily_reviews (
	session_date DATE NOT NULL,
	created_at DATETIME NOT NULL,
	trades INTEGER NOT NULL,
	total_r FLOAT NOT NULL,
	realized_pl NUMERIC(20, 6) NOT NULL,
	mistakes INTEGER NOT NULL,
	review JSON,
	PRIMARY KEY (session_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pair_trades (
	id VARCHAR(32) NOT NULL,
	pair VARCHAR(40) NOT NULL,
	first_symbol VARCHAR(16) NOT NULL,
	second_symbol VARCHAR(16) NOT NULL,
	side VARCHAR(12) NOT NULL,
	status VARCHAR(12) NOT NULL,
	venue VARCHAR(16) NOT NULL,
	by VARCHAR(32) NOT NULL,
	hedge FLOAT NOT NULL,
	lookback INTEGER NOT NULL,
	half_life FLOAT NOT NULL,
	entry_z FLOAT NOT NULL,
	band_z FLOAT NOT NULL,
	stop_z FLOAT NOT NULL,
	exit_z FLOAT NOT NULL,
	time_stop_days INTEGER NOT NULL,
	spread_sd FLOAT NOT NULL,
	qty_first NUMERIC(20, 6) NOT NULL,
	qty_second NUMERIC(20, 6) NOT NULL,
	price_first NUMERIC(20, 6) NOT NULL,
	price_second NUMERIC(20, 6) NOT NULL,
	entry_first NUMERIC(20, 6),
	entry_second NUMERIC(20, 6),
	dollar_risk NUMERIC(20, 6) NOT NULL,
	trade_first_id VARCHAR(32),
	trade_second_id VARCHAR(32),
	opened_at DATETIME,
	closed_at DATETIME,
	exit_reason VARCHAR(24) NOT NULL,
	exit_z_at FLOAT,
	realized_pl NUMERIC(20, 6),
	r_multiple FLOAT,
	model JSON,
	notes TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_pair_trades_pair ON pair_trades (pair);
CREATE INDEX ix_pair_trades_status ON pair_trades (status);
CREATE INDEX ix_pair_trades_opened_at ON pair_trades (opened_at);
CREATE INDEX ix_pair_trades_closed_at ON pair_trades (closed_at);
CREATE INDEX ix_pair_trades_created_at ON pair_trades (created_at);

-- what a model learns from (research/dataset.py): the replay's trades and the plays not taken

CREATE TABLE IF NOT EXISTS sim_trades (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	run_id VARCHAR(32) NOT NULL, 
	ran_at DATETIME NOT NULL, 
	strategy VARCHAR(48) NOT NULL, 
	symbol VARCHAR(16) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	timeframe VARCHAR(16) NOT NULL, 
	entered_at DATETIME, 
	exited_at DATETIME, 
	entry_price NUMERIC(20, 6) NOT NULL, 
	exit_price NUMERIC(20, 6) NOT NULL, 
	r FLOAT NOT NULL, 
	exit_reason VARCHAR(24) NOT NULL, 
	noise JSON, 
	confirmed BOOL NOT NULL, 
	mfe_r FLOAT NOT NULL, 
	scaled BOOL NOT NULL, 
	held_out BOOL NOT NULL, 
	features JSON, 
	feature_schema INTEGER NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_sim_trades_ran_at ON sim_trades (ran_at);

CREATE INDEX ix_sim_trades_run_id ON sim_trades (run_id);

CREATE INDEX ix_sim_trades_strategy ON sim_trades (strategy);

CREATE INDEX ix_sim_trades_symbol ON sim_trades (symbol);

CREATE TABLE IF NOT EXISTS shadow_trades (
	play_id VARCHAR(32) NOT NULL, 
	session_date DATE NOT NULL, 
	symbol VARCHAR(16) NOT NULL, 
	strategy VARCHAR(48) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	timeframe VARCHAR(16) NOT NULL, 
	seen_at DATETIME, 
	passed_checks BOOL NOT NULL, 
	filled BOOL NOT NULL, 
	entered_at DATETIME, 
	exited_at DATETIME, 
	entry_price NUMERIC(20, 6), 
	exit_price NUMERIC(20, 6), 
	r FLOAT, 
	mfe_r FLOAT, 
	exit_reason VARCHAR(64) NOT NULL, 
	noise JSON, 
	confirmations INTEGER NOT NULL, 
	features JSON, 
	feature_schema INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (play_id)
);

CREATE INDEX ix_shadow_trades_session_date ON shadow_trades (session_date);

CREATE INDEX ix_shadow_trades_strategy ON shadow_trades (strategy);

CREATE INDEX ix_shadow_trades_symbol ON shadow_trades (symbol);

-- trades also gained submitted_at DATETIME, entry_context JSON and mfe_at DATETIME; play_logs gained
-- probability FLOAT - the app adds them to an existing database on start (persistence/db.py).
