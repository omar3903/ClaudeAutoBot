-- tos-trader MySQL schema  (generated - source of truth is tos_bot/persistence/models_orm.py)
-- Canonical setup:  python scripts/init_db.py   (creates tables + indexes; also auto-adds
--                   any new columns to an existing database on every start)
-- Manual setup:
--   CREATE DATABASE tos_trader CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   CREATE USER 'tos'@'%' IDENTIFIED BY 'CHANGE_ME';
--   GRANT ALL PRIVILEGES ON tos_trader.* TO 'tos'@'%'; FLUSH PRIVILEGES;

USE tos_trader;

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

CREATE TABLE IF NOT EXISTS token_audit (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts DATETIME NOT NULL, 
	broker VARCHAR(16) NOT NULL, 
	event VARCHAR(32) NOT NULL, 
	refresh_token_age_days FLOAT, 
	refresh_token_expires_at DATETIME, 
	detail VARCHAR(400) NOT NULL, 
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_token_audit_ts ON token_audit (ts);

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
