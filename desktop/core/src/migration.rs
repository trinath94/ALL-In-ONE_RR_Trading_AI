//! Migration from the Ubuntu deployment: content verification and the migration report (Phase 23, Part 23).
//!
//! The import (Phase 18) restores the server's dump into a scratch database first. There, every table's shape
//! (its columns, as the server had them) and a fingerprint of its content are recorded:
//! * a table with an integer `id`: one MD5 over every row (all of the server's columns, ordered by id) and the
//!   largest id. After the real restore and this version's migrations, the same columns of the rows up to that id
//!   must give the same fingerprint: nothing the server had was changed, whatever this version added after it;
//! * a table without one (link tables, small): the MD5 of every row; each must still be present.
//!
//! Only plain lowercase identifiers are handled (every table of this application); anything else is reported as not
//! checked, never interpolated into SQL. The results are grouped into the domains Part 23 names, then written into a
//! human-readable migration report next to the backup.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Columns of every base table in `public` (name, ordered column list, whether it has an integer `id`).
pub const CATALOG_SQL: &str = "SELECT c.table_name || '|' || string_agg(c.column_name, ',' ORDER BY c.ordinal_position) || '|' || \
    bool_or(c.column_name = 'id' AND c.data_type IN ('integer', 'bigint', 'smallint'))::text \
    FROM information_schema.columns c JOIN information_schema.tables t ON t.table_schema = c.table_schema \
    AND t.table_name = c.table_name WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' GROUP BY c.table_name ORDER BY 1";

/// Trading records on this PC that an import would replace (the explicit-policy gate) and, for the report, users.
/// `to_regclass` makes a table that does not exist count as 0 (a fresh installation).
pub const LOCAL_RECORDS_SQL: &str = "SELECT t || '|' || CASE WHEN to_regclass('public.' || t) IS NULL THEN '0' ELSE \
    (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', t), false, true, '')))[1]::text END \
    FROM unnest(ARRAY['closed_positions', 'trade_orders', 'trading_signals', 'mt5_deals', 'strategy_experiments', \
    'model_registry', 'users']) t";

/// Tables whose records make an import need the explicit REPLACE policy when the PC already has them.
pub const PROTECTED_LOCAL: [&str; 6] = ["closed_positions", "trade_orders", "trading_signals", "mt5_deals", "strategy_experiments",
    "model_registry"];

/// What the reset to a new installation is about to change (recorded just before it, for the report).
pub const POLICY_OBSERVATIONS_SQL: &str = "SELECT 'active_mode|' || coalesce((SELECT active_mode::text FROM trading_state LIMIT 1), '') \
    UNION ALL SELECT 'auto_trading|' || coalesce((SELECT auto_trading::text FROM trading_state LIMIT 1), '') \
    UNION ALL SELECT 'live_approved|' || (SELECT count(*) FROM trading_accounts WHERE mode = 'LIVE' AND live_approved_at IS NOT NULL) \
    UNION ALL SELECT 'live_active|' || (SELECT count(*) FROM trading_accounts WHERE mode = 'LIVE' AND is_active) \
    UNION ALL SELECT 'connectors|' || (SELECT count(*) FROM mt5_agents WHERE revoked_at IS NULL) \
    UNION ALL SELECT 'link_tokens|' || (SELECT count(*) FROM mt5_agent_tokens WHERE used_at IS NULL AND expires_at > now()) \
    UNION ALL SELECT 'sessions|' || (SELECT count(*) FROM user_sessions WHERE NOT revoked)";

/// Part 23's verification domains (items 5-13) and the tables that hold them.
pub const DOMAINS: [(&str, &[&str]); 9] = [
    ("Strategy records", &["strategies", "strategy_configurations", "strategy_parameters", "strategy_versions", "strategy_fingerprints"]),
    ("Account records", &["trading_accounts", "broker_connections", "account_snapshots"]),
    ("Trade history", &["trade_orders", "trade_executions", "mt5_deals", "trading_signals", "execution_idempotency",
                        "position_adjustments", "open_positions"]),
    ("Closed positions", &["closed_positions"]),
    ("Strategy performance", &["strategy_performance", "performance_snapshots", "portfolio_snapshots"]),
    ("Experiments", &["strategy_experiments", "optimisation_results", "backtest_jobs", "backtest_results"]),
    ("Learning records", &["learning_jobs", "learning_memory", "failure_reports", "missed_opportunities", "trade_features",
                           "drift_events", "consensus_decisions", "ai_recommendations"]),
    ("Model metadata", &["model_registry", "model_predictions", "approval_records"]),
    ("Configuration", &["risk_configs", "scanner_settings", "notification_settings", "trading_state", "users", "roles",
                        "permissions", "role_permissions", "mt5_terminals", "symbols", "market_sessions"]),
];

/// Changes this version makes on purpose, not losses: (table, explanation).
pub const POLICY_CHANGES: [(&str, &str); 2] = [
    ("alembic_version", "the schema version is advanced by the migrations"),
    ("role_permissions", "the standard roles (admin, trader, readonly) are reset to this version's permissions by its seed"),
];

pub fn valid_ident(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b.len() <= 63 && (b[0].is_ascii_lowercase() || b[0] == b'_')
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Shape {
    pub columns: Vec<String>,
    pub has_id: bool,
}

/// Parse `CATALOG_SQL` output. Tables or columns with unusual names are left out (and so reported as not checked).
pub fn parse_catalog(out: &str) -> BTreeMap<String, Shape> {
    out.lines().filter_map(|l| {
        let mut p = l.trim().splitn(3, '|');
        let (t, cols, id) = (p.next()?, p.next()?, p.next()?);
        let columns: Vec<String> = cols.split(',').map(String::from).collect();
        (valid_ident(t) && columns.iter().all(|c| valid_ident(c)))
            .then(|| (t.to_string(), Shape { columns, has_id: id == "true" }))
    }).collect()
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct Fingerprint {
    /// Tables with an id: the largest id (None: empty) and the MD5 over the rows up to it.
    pub max_id: Option<i64>,
    pub hash: Option<String>,
    /// Tables without an id: the MD5 of each row.
    pub rows: Option<Vec<String>>,
}

/// One SQL script (read through psql's stdin) that fingerprints `shapes`. With `bounds`, an id table is limited to
/// the rows up to the server's largest id (None: the server's table was empty, so no row).
pub fn fingerprint_sql(shapes: &BTreeMap<String, Shape>, bounds: Option<&BTreeMap<String, Fingerprint>>) -> String {
    let mut sql = String::new();
    for (t, s) in shapes {
        let row = format!("ROW({})", s.columns.join(", "));
        if s.has_id {
            let filter = match bounds.and_then(|b| b.get(t)) {
                None => String::new(),
                Some(Fingerprint { max_id: Some(n), .. }) => format!(" WHERE id <= {n}"),
                Some(_) => " WHERE false".to_string(),
            };
            sql.push_str(&format!("SELECT 'f|{t}|' || coalesce(max(id)::text, '') || '|' || \
                md5(coalesce(string_agg(md5({row}::text), '' ORDER BY id), '')) FROM {t}{filter};\n"));
        } else {
            sql.push_str(&format!("SELECT 'r|{t}|' || md5({row}::text) FROM {t};\n"));
        }
    }
    sql
}

/// Parse the fingerprint script's output for `shapes`. An empty table without an id prints nothing: it is recorded
/// as having no rows (not as missing).
pub fn fingerprints_for(shapes: &BTreeMap<String, Shape>, out: &str) -> BTreeMap<String, Fingerprint> {
    let mut m = parse_fingerprints(out);
    for (t, sh) in shapes {
        if !sh.has_id {
            m.entry(t.clone()).or_default().rows.get_or_insert_with(Vec::new);
        }
    }
    m
}

pub fn parse_fingerprints(out: &str) -> BTreeMap<String, Fingerprint> {
    let mut m: BTreeMap<String, Fingerprint> = BTreeMap::new();
    for l in out.lines() {
        let mut p = l.trim().splitn(4, '|');
        match (p.next(), p.next(), p.next(), p.next()) {
            (Some("f"), Some(t), Some(max), Some(h)) => {
                m.insert(t.into(), Fingerprint { max_id: max.parse().ok(), hash: Some(h.into()), rows: None });
            }
            (Some("r"), Some(t), Some(h), None) => {
                m.entry(t.into()).or_default().rows.get_or_insert_with(Vec::new).push(h.into());
            }
            _ => {}
        }
    }
    m
}

/// Everything recorded about the server's data in the scratch database (stored next to the backup).
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SourceRecord {
    pub counts: BTreeMap<String, u64>,
    pub shapes: BTreeMap<String, Shape>,
    pub fingerprints: BTreeMap<String, Fingerprint>,
    pub manifest: Option<serde_json::Value>,
    pub local_before: BTreeMap<String, u64>,
    pub policy: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Unchanged,
    /// Every server row unchanged; this version added rows after them.
    Grew,
    /// A change this version makes on purpose (see POLICY_CHANGES).
    Policy,
    Changed,
    Lost,
    NotChecked,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableResult {
    pub table: String,
    pub source_rows: u64,
    pub rows_now: Option<u64>,
    pub verdict: Verdict,
    pub detail: String,
}

/// Compare the server's record with the database after restore and migrations.
pub fn compare(src: &SourceRecord, counts_now: &BTreeMap<String, u64>, now: &BTreeMap<String, Fingerprint>) -> Vec<TableResult> {
    src.counts.iter().map(|(t, &n)| {
        let rows_now = counts_now.get(t).copied();
        let policy = POLICY_CHANGES.iter().find(|(pt, _)| pt == t).map(|(_, why)| why.to_string());
        let (verdict, detail) = match (rows_now, src.shapes.get(t), src.fingerprints.get(t), now.get(t)) {
            (None, ..) => (Verdict::Lost, "the table is missing".to_string()),
            (Some(r), ..) if r < n && policy.is_none() => (Verdict::Lost, format!("{} of the server's rows are missing", n - r)),
            (_, None, ..) | (_, _, None, _) => (Verdict::NotChecked, "content not fingerprinted (unusual name)".to_string()),
            (Some(r), Some(_), Some(a), Some(b)) => {
                let same = match (&a.rows, &b.rows) {
                    (Some(ra), Some(rb)) => {
                        let mut pool: BTreeMap<&str, i64> = BTreeMap::new();
                        rb.iter().for_each(|h| *pool.entry(h.as_str()).or_default() += 1);
                        let missing = ra.iter().filter(|h| {
                            let c = pool.entry(h.as_str()).or_default();
                            *c -= 1;
                            *c < 0
                        }).count();
                        (missing == 0, missing)
                    }
                    _ => (a.hash.is_some() && a.hash == b.hash, 0),
                };
                match (same, policy) {
                    ((true, _), _) if r == n => (Verdict::Unchanged, "every row unchanged".to_string()),
                    ((true, _), _) => (Verdict::Grew, format!("every server row unchanged; this version added {}", r - n)),
                    (_, Some(why)) => (Verdict::Policy, why),
                    ((false, 0), None) => (Verdict::Changed, "some of the server's rows differ after the migration".to_string()),
                    ((false, k), None) => (Verdict::Changed, format!("{k} of the server's rows differ or are missing")),
                }
            }
            (Some(_), Some(_), Some(_), None) => (Verdict::Lost, "the table could not be read after the migration".to_string()),
        };
        TableResult { table: t.clone(), source_rows: n, rows_now, verdict, detail }
    }).collect()
}

pub fn failed(results: &[TableResult]) -> Vec<&TableResult> {
    results.iter().filter(|r| matches!(r.verdict, Verdict::Changed | Verdict::Lost)).collect()
}

/// Part 23 domains: (domain, verdict text, ok).
pub fn domains(results: &[TableResult]) -> Vec<(String, String, bool)> {
    DOMAINS.iter().map(|(name, tables)| {
        let mine: Vec<&TableResult> = results.iter().filter(|r| tables.contains(&r.table.as_str())).collect();
        if mine.is_empty() {
            return (name.to_string(), "not in the server's schema (nothing to migrate)".into(), true);
        }
        let bad: Vec<String> = mine.iter().filter(|r| matches!(r.verdict, Verdict::Changed | Verdict::Lost))
            .map(|r| format!("{}: {}", r.table, r.detail)).collect();
        let rows: u64 = mine.iter().map(|r| r.source_rows).sum();
        if bad.is_empty() {
            let notes: Vec<String> = mine.iter().filter(|r| r.verdict == Verdict::Policy).map(|r| format!("{} ({})", r.table, r.detail)).collect();
            let extra = if notes.is_empty() { String::new() } else { format!("; by policy: {}", notes.join("; ")) };
            (name.to_string(), format!("verified: {} tables, {rows} server rows, content unchanged{extra}", mine.len()), true)
        } else {
            (name.to_string(), bad.join("; "), false)
        }
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shapes() -> BTreeMap<String, Shape> {
        parse_catalog("users|id,username,role_id|true\nrole_permissions|role_id,permission_id|false\nweird table|a|false\nx|Bad|false")
    }

    #[test]
    fn an_empty_link_table_is_recorded_with_no_rows_not_as_missing() {
        let f = fingerprints_for(&shapes(), "f|users|2|aaa");
        assert_eq!(f["role_permissions"].rows, Some(vec![]));
        let src = SourceRecord { counts: [("role_permissions".to_string(), 0)].into(), shapes: shapes(), fingerprints: f.clone(), ..Default::default() };
        let r = compare(&src, &[("role_permissions".to_string(), 0)].into(), &f);
        assert_eq!(r[0].verdict, Verdict::Unchanged);
    }

    #[test]
    fn catalog_keeps_only_plain_identifiers() {
        let s = shapes();
        assert_eq!(s.keys().collect::<Vec<_>>(), vec!["role_permissions", "users"]);
        assert!(s["users"].has_id && !s["role_permissions"].has_id);
        assert!(valid_ident("closed_positions") && !valid_ident("a;drop") && !valid_ident("A") && !valid_ident("1x") && !valid_ident(""));
    }

    #[test]
    fn fingerprint_sql_is_bounded_by_the_servers_ids() {
        let s = shapes();
        let free = fingerprint_sql(&s, None);
        assert!(free.contains("FROM users;") && free.contains("md5(ROW(id, username, role_id)::text)"));
        assert!(free.contains("SELECT 'r|role_permissions|' || md5(ROW(role_id, permission_id)::text) FROM role_permissions;"));
        let mut b = BTreeMap::new();
        b.insert("users".to_string(), Fingerprint { max_id: Some(42), hash: Some("h".into()), rows: None });
        assert!(fingerprint_sql(&s, Some(&b)).contains("FROM users WHERE id <= 42;"));
        b.insert("users".to_string(), Fingerprint { max_id: None, hash: Some("h".into()), rows: None });
        assert!(fingerprint_sql(&s, Some(&b)).contains("FROM users WHERE false;"), "an empty server table matches no row");
    }

    fn src(counts: &[(&str, u64)], fps: &str) -> SourceRecord {
        SourceRecord { counts: counts.iter().map(|(t, n)| (t.to_string(), *n)).collect(), shapes: shapes(),
                       fingerprints: parse_fingerprints(fps), ..Default::default() }
    }

    #[test]
    fn unchanged_grown_changed_lost_and_policy_are_told_apart() {
        let s = src(&[("users", 2), ("role_permissions", 3)], "f|users|2|aaa\nr|role_permissions|h1\nr|role_permissions|h2\nr|role_permissions|h3");
        let now = |c: &[(&str, u64)], f: &str| (c.iter().map(|(t, n)| (t.to_string(), *n)).collect::<BTreeMap<_, _>>(), parse_fingerprints(f));
        let (c, f) = now(&[("users", 2), ("role_permissions", 4)], "f|users|2|aaa\nr|role_permissions|h1\nr|role_permissions|h2\nr|role_permissions|h3\nr|role_permissions|h9");
        let r = compare(&s, &c, &f);
        assert_eq!(r.iter().map(|x| x.verdict.clone()).collect::<Vec<_>>(), vec![Verdict::Grew, Verdict::Unchanged]);
        let (c, f) = now(&[("users", 3), ("role_permissions", 3)], "f|users|2|bbb\nr|role_permissions|h1\nr|role_permissions|h2\nr|role_permissions|h9");
        let r = compare(&s, &c, &f);
        assert_eq!(r[0].verdict, Verdict::Policy, "standard roles reset by the seed: reported, not a failure");
        assert_eq!(r[1].verdict, Verdict::Changed);
        assert_eq!(failed(&r).len(), 1);
        let (c, f) = now(&[("users", 1)], "f|users|1|ccc");
        let r = compare(&s, &c, &f);
        assert!(r[1].verdict == Verdict::Lost && r[1].detail.contains("1 of the server's rows are missing"));
        assert_eq!(r[0].verdict, Verdict::Lost, "role_permissions table missing entirely");
    }

    #[test]
    fn domains_group_tables_and_name_failures() {
        let s = src(&[("users", 2), ("closed_positions", 5)], "f|users|2|aaa");
        let mut s = s;
        s.shapes.insert("closed_positions".into(), Shape { columns: vec!["id".into(), "profit".into()], has_id: true });
        s.fingerprints.insert("closed_positions".into(), Fingerprint { max_id: Some(5), hash: Some("p".into()), rows: None });
        let counts: BTreeMap<String, u64> = [("users".to_string(), 2), ("closed_positions".to_string(), 5)].into();
        let mut f = parse_fingerprints("f|users|2|aaa");
        f.insert("closed_positions".into(), Fingerprint { max_id: Some(5), hash: Some("CHANGED".into()), rows: None });
        let d = domains(&compare(&s, &counts, &f));
        let get = |n: &str| d.iter().find(|x| x.0 == n).unwrap().clone();
        assert!(get("Configuration").2 && get("Configuration").1.starts_with("verified: 1 tables, 2 server rows"));
        assert!(!get("Closed positions").2 && get("Closed positions").1.contains("closed_positions: some of the server's rows differ"));
        assert!(get("Experiments").2 && get("Experiments").1.contains("not in the server's schema"));
        assert_eq!(d.len(), 9);
    }
}
