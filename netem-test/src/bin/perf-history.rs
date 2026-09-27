//! `perf-history` — archive every perf run forever, and compare each run to the
//! previous one, reporting the degradation figure per arm and metric.
//!
//! Why this exists
//! ---------------
//!
//! A battery run is evidence only until the next run overwrites it, and a run
//! directory under a temp root is swept. That has cost this workspace the
//! ability to answer "is this worse than last time?" more than once — once
//! because the numbers lived only in a log, once because the previous run's
//! directory was deleted with a workspace. So:
//!
//! * **Every run is archived**, artifacts and all, under a durable root outside
//!   every repo and every temp tree. The archive is the record.
//! * **Every run is compared to the previous archived run**, and the comparison
//!   is written as `vs-prev.md` beside the run *and* into the archive.
//! * **An M1 degradation fails the run** (exit 6). M1 is the interactive tail
//!   and the standing priority: a run whose interactive tail is worse than the
//!   previous run's is a rejection, not a number to weigh. A *guard* bounds what
//!   a window can observe; it does not license a rise.
//!
//! The comparison is two-layered on purpose. The per-metric table below carries
//! the **degradation figure** — absolute and percent, per arm, per metric —
//! because that is the number a reader needs and a boolean cannot hold it. The
//! multi-axis coverage/claim verdict stays with the A/B tool that already works
//! (`mandate_compare.py`), invoked when present rather than reimplemented, so
//! its semantics cannot drift from these two implementations disagreeing.
//!
//! Usage
//! -----
//!
//! ```text
//! cargo run -p netem-test --bin perf-history -- <run-dir> [--label L]
//!     [--baseline <run-dir>] [--no-archive] [--only-compare] [--no-ab]
//! ```
//!
//! `$PERF_ARCHIVE_DIR` relocates the archive (default `.net-perf-history` in the
//! working directory, self-ignoring); `$PERF_BASELINE_DIR` compares against a
//! chosen run instead of the previous one (that is how a run is compared against
//! what is deployed, when the previous run is itself suspect).

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

/// The report a completed run writes, and the log its raw lines land in.
const REPORT_NAME: &str = "mandate-check.json";
const LOG_NAME: &str = "mandate-smoke.log";

/// Where runs are archived: `.net-perf-history` in the working directory, so
/// each repo carries its own history beside the work it describes. The
/// directory writes a `*` `.gitignore` for itself (the idiom the workspace's
/// `local/` uses), because an archive that lands inside a repo must not become a
/// working-copy change.
const DEFAULT_ARCHIVE_DIR: &str = ".net-perf-history";

/// Exit status for a run whose interactive tail regressed. Distinct from 1
/// (usage) and 2 (a run that could not be archived or compared) so a caller can
/// tell "worse than last time" from "could not tell".
const EXIT_M1_DEGRADATION: i32 = 6;

/// Metrics compared per arm, in the order a reader wants them. Latency
/// percentiles are the product's promise and lead; delivery and the own-wire
/// multiple follow because they are the two ways a latency win can be bought
/// dishonestly.
const LATENCY_METRICS: &[&str] = &["p50", "p90", "p99", "p999", "max"];
const OTHER_METRICS: &[&str] = &["over250", "wire_x", "delivery"];

/// The arms whose tail *is* M1: the interactive lane, clean and impaired. Matched
/// exactly, on the *arm* part of `<producer>/<arm>` — a substring test admits
/// `mandate-smoke/m4/hostile flow A`, which is an M4 four-flow arm whose tail is
/// not the interactive lane, and would reject a run for the wrong metric.
const M1_ARM_KEYS: &[&str] = &["clean", "hostile", "lone_tail", "lone tail"];

/// How much movement is noise rather than degradation, per arm and metric, as a
/// fraction. Not invented: the shipped lone-tail measurement reports a p99
/// coefficient of variation of 0.067 across ten runs while its *maximum* varies
/// by 7.3x, so a percentile and a single order statistic cannot share a band. A
/// 10 % band absorbs ordinary run-to-run movement on a percentile; a maximum
/// gets a much wider one because it is one sample's tail.
///
/// The **impaired** arms need a wider percentile band than the clean one, and
/// that is measured, not asserted: across the archived runs the hostile p99 has
/// read 122.2, 127.2, 187.2 and 211.7 ms -- a 1.7x spread at the same settings.
/// A 10 % band there rejects a good run roughly half the time, which is a
/// rejection nobody can act on. A clean-arm percentile is tight (cv 0.067), so it
/// keeps the narrow band; a per-arm band is the price of a rejection that means
/// something.
fn noise_band(metric: &str, arm: &str) -> f64 {
    let impaired = arm.contains("hostile") || arm.contains("lone");
    match metric {
        "p50" | "p90" => 0.10,
        "p99" => {
            if impaired {
                0.40
            } else {
                0.10
            }
        }
        "p999" => {
            if impaired {
                0.50
            } else {
                0.20
            }
        }
        "max" => 0.50,
        _ => 0.10,
    }
}

/// A metric where a *fall* is the degradation (delivery is the only one).
fn worse_when_lower(metric: &str) -> bool {
    metric == "delivery"
}

#[derive(Debug)]
struct Failure(String);

impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

type Result<T> = std::result::Result<T, Failure>;

fn fail<T>(message: impl Into<String>) -> Result<T> {
    Err(Failure(message.into()))
}

// ─────────────────────────────── archiving ───────────────────────────────────

fn archive_dir() -> PathBuf {
    match env::var("PERF_ARCHIVE_DIR") {
        Ok(value) if !value.trim().is_empty() => PathBuf::from(value),
        _ => PathBuf::from(DEFAULT_ARCHIVE_DIR),
    }
}

fn now_stamp() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // UTC, formatted without a date library: days since the epoch, then the
    // civil date from the standard algorithm.
    let days = (seconds / 86_400) as i64;
    let secs_of_day = seconds % 86_400;
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}{month:02}{day:02}T{:02}{:02}{:02}Z",
        secs_of_day / 3600,
        (secs_of_day % 3600) / 60,
        secs_of_day % 60
    )
}

/// Howard Hinnant's `civil_from_days`.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if month <= 2 { year + 1 } else { year }, month, day)
}

/// The revision the report names, short, if it names one.
fn revision_of(report: &Path) -> String {
    let Ok(text) = fs::read_to_string(report) else {
        return String::new();
    };
    for key in ["\"revision\"", "\"rev\"", "\"commit\"", "\"head\""] {
        if let Some(index) = text.find(key) {
            let rest = &text[index + key.len()..];
            if let Some(colon) = rest.find(':') {
                let after = &rest[colon + 1..];
                let span = after
                    .find('"')
                    .and_then(|start| after[start + 1..].find('"').map(|end| (start, end)));
                if let Some((start, end)) = span {
                    let value: String = after[start + 1..start + 1 + end]
                        .chars()
                        .filter(|c| c.is_ascii_alphanumeric())
                        .take(12)
                        .collect();
                    if !value.is_empty() {
                        return value;
                    }
                }
            }
        }
    }
    String::new()
}

/// What an archive keeps: everything a reader needs to re-derive a verdict
/// without re-running. The rendered panels are included deliberately — a run
/// whose panels were only summarised was not read, so the panels are the
/// artifact, not a by-product.
fn archive_entry(run_dir: &Path, label: &str) -> Result<PathBuf> {
    let base = archive_dir().join(label);
    fs::create_dir_all(&base)
        .map_err(|e| Failure(format!("cannot create {}: {e}", base.display())))?;
    // Self-ignoring at the archive *root*, so one `*` covers every label and an
    // archive inside a repo stays out of the working copy.
    let root = archive_dir();
    let ignore = root.join(".gitignore");
    if !ignore.exists() {
        let _ = fs::write(&ignore, "*\n");
    }

    let report = run_dir.join(REPORT_NAME);
    if !report.is_file() {
        return fail(format!(
            "{} has no {REPORT_NAME}, so it is not a completed run and there is nothing to archive",
            run_dir.display()
        ));
    }
    let revision = revision_of(&report);
    let stamp = now_stamp();
    let mut name = if revision.is_empty() {
        stamp.clone()
    } else {
        format!("{stamp}-{revision}")
    };
    let mut entry = base.join(&name);
    let mut suffix = 1;
    while entry.exists() {
        suffix += 1;
        name = format!("{stamp}-{suffix}");
        entry = base.join(&name);
    }
    fs::create_dir_all(&entry)
        .map_err(|e| Failure(format!("cannot create {}: {e}", entry.display())))?;

    let mut copied = 0usize;
    for item in fs::read_dir(run_dir).map_err(|e| Failure(format!("cannot read run dir: {e}")))? {
        let item = item.map_err(|e| Failure(format!("cannot read run entry: {e}")))?;
        let path = item.path();
        if !path.is_file() {
            continue;
        }
        let keep = matches!(
            path.extension().and_then(|e| e.to_str()),
            Some("json") | Some("log") | Some("csv")
        );
        if keep {
            copy(&path, &entry.join(item.file_name()))?;
            copied += 1;
        }
    }
    let plots = run_dir.join("plots");
    if plots.is_dir() {
        let out = entry.join("plots");
        fs::create_dir_all(&out)
            .map_err(|e| Failure(format!("cannot create {}: {e}", out.display())))?;
        for plot in fs::read_dir(&plots).map_err(|e| Failure(format!("cannot read plots: {e}")))? {
            let plot = plot.map_err(|e| Failure(format!("cannot read plot entry: {e}")))?;
            if plot.path().is_file() {
                copy(&plot.path(), &out.join(plot.file_name()))?;
                copied += 1;
            }
        }
    }
    if copied == 0 {
        return fail(format!(
            "{} held no artifacts to archive, which means the run wrote none",
            run_dir.display()
        ));
    }

    fs::write(base.join("latest"), format!("{name}\n"))
        .map_err(|e| Failure(format!("cannot write latest pointer: {e}")))?;
    Ok(entry)
}

fn copy(from: &Path, to: &Path) -> Result<()> {
    fs::copy(from, to).map_err(|e| Failure(format!("cannot copy {}: {e}", from.display())))?;
    Ok(())
}

/// Every archived entry for `label`, oldest first.
fn archived_runs(label: &str) -> Vec<PathBuf> {
    let base = archive_dir().join(label);
    let Ok(items) = fs::read_dir(&base) else {
        return Vec::new();
    };
    let mut runs: Vec<PathBuf> = items
        .filter_map(|item| item.ok())
        .map(|item| item.path())
        .filter(|path| path.is_dir() && path.join(REPORT_NAME).is_file())
        .collect();
    runs.sort_by(|a, b| a.file_name().cmp(&b.file_name()));
    runs
}

/// The newest archived run that is not the candidate.
fn previous_run(label: &str, candidate: &Path) -> Option<PathBuf> {
    let candidate = candidate
        .canonicalize()
        .unwrap_or_else(|_| candidate.to_path_buf());
    // `retain` + `pop`, not `filter(..).last()`/`next_back()`: clippy flags both
    // spellings of "the final element" on a Vec, and the newest run is simply the
    // last element of a list already sorted by name.
    let mut runs = archived_runs(label);
    runs.retain(|path| path.canonicalize().map(|p| p != candidate).unwrap_or(true));
    runs.pop()
}

fn baseline_from_env() -> Option<PathBuf> {
    match env::var("PERF_BASELINE_DIR") {
        Ok(value) if !value.trim().is_empty() => Some(PathBuf::from(value)),
        _ => None,
    }
}

// ────────────── reading the per-arm metrics out of a run ─────────────────────

/// A line that names an arm and carries its numbers: `[producer arm] k=v …`.
/// Parsed from text rather than from the report's JSON so the comparison keeps
/// working across schema bumps — which is the property a permanent record needs.
fn parse_arm_lines(text: &str, into: &mut BTreeMap<String, BTreeMap<String, f64>>) {
    for line in text.lines() {
        let line = line.trim();
        let Some(open) = line.strip_prefix('[') else {
            continue;
        };
        let Some(close) = open.find(']') else {
            continue;
        };
        let header = &open[..close];
        let mut parts = header.split_whitespace();
        let Some(producer) = parts.next() else {
            continue;
        };
        let arm = parts.collect::<Vec<_>>().join(" ");
        if arm.is_empty() {
            continue;
        }
        let body = &open[close + 1..];
        let mut values: BTreeMap<String, f64> = BTreeMap::new();
        // The raw producer line writes `p99= 26.5` and the report quotes it
        // verbatim, while the report's own typed fields write `"p99": 26.5,`.
        // Both put whitespace after the separator, so a token splitter sees
        // `p99=` with no value and silently measures nothing. Scan instead:
        // a key, optional space, a separator, optional space, a number.
        let bytes = body.as_bytes();
        let mut i = 0usize;
        while i < bytes.len() {
            let start = i;
            while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                i += 1;
            }
            if i == start {
                i += 1;
                continue;
            }
            let key = &body[start..i];
            while i < bytes.len() && (bytes[i] == b' ' || bytes[i] == b'\t') {
                i += 1;
            }
            if i >= bytes.len() || (bytes[i] != b'=' && bytes[i] != b':') {
                continue;
            }
            i += 1;
            while i < bytes.len() && (bytes[i] == b' ' || bytes[i] == b'\t') {
                i += 1;
            }
            let value_start = i;
            while i < bytes.len()
                && (bytes[i].is_ascii_digit() || bytes[i] == b'.' || bytes[i] == b'-')
            {
                i += 1;
            }
            let parsed = if i > value_start {
                body[value_start..i].parse::<f64>().ok()
            } else {
                None
            };
            if let Some(value) = parsed {
                values.entry(key.to_string()).or_insert(value);
            }
        }
        // `wire=… x=2.28` — the own-wire multiple is the `x` key.
        if let Some(x) = values.get("x").copied() {
            values.insert("wire_x".to_string(), x);
        }
        // `delivery=1.000` arrives as `delivery` because the scan stops at the
        // space the producer writes after `=`; nothing to repair, the key is
        // already right. A lane that delivered nothing would write `0`.
        if values.is_empty() {
            continue;
        }
        into.entry(format!("{producer}/{arm}"))
            .or_default()
            .extend(values);
    }
}

/// The per-arm metrics of a run directory or a report file.
fn read_arms(path: &Path) -> Result<BTreeMap<String, BTreeMap<String, f64>>> {
    let report = if path.is_dir() {
        path.join(REPORT_NAME)
    } else {
        path.to_path_buf()
    };
    let text = fs::read_to_string(&report)
        .map_err(|e| Failure(format!("cannot read {}: {e}", report.display())))?;
    let mut arms = BTreeMap::new();
    parse_arm_lines(&text, &mut arms);
    let log = report.with_file_name(LOG_NAME);
    if let Ok(log_text) = fs::read_to_string(&log) {
        parse_arm_lines(&log_text, &mut arms);
    }
    if arms.is_empty() {
        return fail(format!(
            "{} records no per-arm measurement lines, so there is nothing to compare — a run that produced no arms is not a baseline",
            report.display()
        ));
    }
    Ok(arms)
}

// ─────────────────── the table: the degradation figure ───────────────────────

#[derive(Debug, Clone)]
struct Row {
    arm: String,
    metric: String,
    baseline: Option<f64>,
    candidate: Option<f64>,
    percent: f64,
    band_percent: f64,
    worse: bool,
    missing: bool,
}

fn metric_rows(
    baseline: &BTreeMap<String, BTreeMap<String, f64>>,
    candidate: &BTreeMap<String, BTreeMap<String, f64>>,
) -> Vec<Row> {
    let mut rows = Vec::new();
    for (arm, base) in baseline {
        let Some(cand) = candidate.get(arm) else {
            rows.push(Row {
                arm: arm.clone(),
                metric: "*".to_string(),
                baseline: None,
                candidate: None,
                percent: 0.0,
                band_percent: 0.0,
                worse: true,
                missing: true,
            });
            continue;
        };
        for metric in LATENCY_METRICS.iter().chain(OTHER_METRICS) {
            let (old, new) = (base.get(*metric), cand.get(*metric));
            if old.is_none() && new.is_none() {
                continue;
            }
            let band = noise_band(metric, arm);
            let Some((old, new)) = old.zip(new) else {
                rows.push(Row {
                    arm: arm.clone(),
                    metric: metric.to_string(),
                    baseline: old.copied(),
                    candidate: new.copied(),
                    percent: 0.0,
                    band_percent: band * 100.0,
                    worse: true,
                    missing: true,
                });
                continue;
            };
            let (old, new) = (*old, *new);
            let percent = if old != 0.0 {
                (new - old) / old.abs() * 100.0
            } else {
                0.0
            };
            let worse = if worse_when_lower(metric) {
                new < old && percent.abs() > band * 100.0
            } else {
                new > old && percent > band * 100.0
            };
            rows.push(Row {
                arm: arm.clone(),
                metric: metric.to_string(),
                baseline: Some(old),
                candidate: Some(new),
                percent,
                band_percent: band * 100.0,
                worse,
                missing: false,
            });
        }
    }
    rows
}

/// The rows that are an M1 regression: an interactive arm's latency rose.
fn m1_degradations(rows: &[Row]) -> Vec<&Row> {
    rows.iter()
        .filter(|row| row.worse)
        .filter(|row| LATENCY_METRICS.contains(&row.metric.as_str()))
        .filter(|row| {
            // The arm part of `<producer>/<arm>`, which must itself carry no
            // further `/`: that is what separates the interactive arms from the
            // per-flow four-flow arms.
            let arm = row
                .arm
                .split_once('/')
                .map(|(_, arm)| arm)
                .unwrap_or(&row.arm);
            !arm.contains('/') && M1_ARM_KEYS.contains(&arm)
        })
        .collect()
}

fn figure(row: &Row) -> String {
    if row.missing {
        return "the measurement stopped (arm or metric absent from one side)".to_string();
    }
    match (row.baseline, row.candidate) {
        (Some(old), Some(new)) => format!("{old:.4} -> {new:.4}  ({:+.1}%)", row.percent),
        _ => "(unavailable)".to_string(),
    }
}

fn vs_prev_markdown(
    rows: &[Row],
    baseline_name: &str,
    candidate_name: &str,
    ab_lines: &[String],
) -> String {
    let degradations = m1_degradations(rows);
    let mut out = String::new();
    out.push_str("# vs-prev\n\n");
    out.push_str(&format!("baseline:  `{baseline_name}`\n"));
    out.push_str(&format!("candidate: `{candidate_name}`\n\n"));
    if degradations.is_empty() {
        out.push_str(
            "## M1: no degradation\n\nNo interactive arm's latency rose beyond its noise band.\n\n",
        );
    } else {
        out.push_str("## M1 DEGRADATION - this run is rejected\n\n");
        out.push_str("M1 is the interactive tail and the standing priority. Any one of these is a\nrejection, not a trade to weigh:\n\n");
        out.push_str("| arm | metric | figure | band |\n|---|---|---|---|\n");
        for row in &degradations {
            out.push_str(&format!(
                "| `{}` | {} | {} | +/-{:.0}% |\n",
                row.arm,
                row.metric,
                figure(row),
                row.band_percent
            ));
        }
        out.push('\n');
    }
    let worse: Vec<&Row> = rows.iter().filter(|row| row.worse).collect();
    out.push_str("## metrics that moved worse\n\n");
    if worse.is_empty() {
        out.push_str("No arm or metric moved beyond its noise band in the worse direction.\n\n");
    } else {
        out.push_str("| arm | metric | figure | band |\n|---|---|---|---|\n");
        for row in worse {
            out.push_str(&format!(
                "| `{}` | {} | {} | +/-{:.0}% |\n",
                row.arm,
                row.metric,
                figure(row),
                row.band_percent
            ));
        }
        out.push('\n');
    }
    out.push_str("## every metric\n\n| arm | metric | figure |\n|---|---|---|\n");
    for row in rows {
        if row.metric == "*" {
            continue;
        }
        out.push_str(&format!(
            "| `{}` | {} | {} |\n",
            row.arm,
            row.metric,
            figure(row)
        ));
    }
    if !ab_lines.is_empty() {
        out.push_str("\n## coverage and claim verdict (mandate_compare.py)\n\n```\n");
        for line in ab_lines {
            out.push_str(line);
            out.push('\n');
        }
        out.push_str("```\n");
    }
    out
}

// ─────────────── the optional reuse of the existing A/B tool ─────────────────

/// The full A/B verdict from `mandate_compare.py`, when it is present. The
/// per-metric table above answers "by how much"; this answers the multi-axis
/// coverage and claim question, and it is *the existing tool's* answer rather
/// than a second implementation of its semantics.
fn ab_verdict(baseline: &Path, candidate: &Path) -> Vec<String> {
    let script = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(|workspace| workspace.join("tools").join("mandate_compare.py"))
        .unwrap_or_else(|| PathBuf::from("mandate_compare.py"));
    if !script.is_file() {
        return vec![format!(
            "(mandate_compare.py not found at {} - coverage and claim axes not checked)",
            script.display()
        )];
    }
    let python = env::var("PYTHON").unwrap_or_else(|_| "python3".to_string());
    let output = Command::new(python)
        .arg(&script)
        .arg(candidate.join(REPORT_NAME))
        .arg("--baseline")
        .arg(baseline.join(REPORT_NAME))
        .output();
    match output {
        Ok(output) => {
            let mut lines: Vec<String> = String::from_utf8_lossy(&output.stdout)
                .lines()
                .map(|line| line.to_string())
                .collect();
            lines.extend(
                String::from_utf8_lossy(&output.stderr)
                    .lines()
                    .filter(|line| !line.trim().is_empty())
                    .map(|line| line.to_string()),
            );
            if lines.is_empty() {
                lines.push(format!(
                    "mandate_compare.py exited {:?} with no output",
                    output.status.code()
                ));
            }
            lines
        }
        Err(error) => vec![format!("could not run mandate_compare.py: {error}")],
    }
}

// ─────────────────────────────── the runner ──────────────────────────────────

struct Args {
    run: PathBuf,
    baseline: Option<PathBuf>,
    label: String,
    archive: bool,
    only_compare: bool,
    no_ab: bool,
}

fn parse_args(argv: &[String]) -> Result<Args> {
    let mut args = Args {
        run: PathBuf::from("."),
        baseline: None,
        label: "mandate-check".to_string(),
        archive: true,
        only_compare: false,
        no_ab: false,
    };
    let mut positional = Vec::new();
    let mut index = 0;
    while index < argv.len() {
        let arg = argv[index].as_str();
        match arg {
            "--baseline" => {
                index += 1;
                let Some(value) = argv.get(index) else {
                    return fail("--baseline needs a path");
                };
                args.baseline = Some(PathBuf::from(value));
            }
            "--label" => {
                index += 1;
                let Some(value) = argv.get(index) else {
                    return fail("--label needs a value");
                };
                args.label = value.clone();
            }
            "--no-archive" => args.archive = false,
            "--only-compare" => args.only_compare = true,
            "--no-ab" => args.no_ab = true,
            "-h" | "--help" => {
                println!("{}", usage());
                std::process::exit(0);
            }
            other if other.starts_with("--") => return fail(format!("unknown flag {other}")),
            other => positional.push(PathBuf::from(other)),
        }
        index += 1;
    }
    if let Some(first) = positional.first() {
        args.run = first.clone();
    }
    Ok(args)
}

fn usage() -> String {
    [
        "perf-history <run-dir> [options]",
        "",
        "  --baseline <dir>   compare against this run instead of the previous archived one",
        "  --label <name>     the archive series (default: mandate-check)",
        "  --no-archive       compare without archiving this run",
        "  --only-compare     exit 0 even on an M1 degradation (report-only)",
        "  --no-ab            skip the mandate_compare.py coverage/claim verdict",
        "",
        "$PERF_ARCHIVE_DIR relocates the archive (default ./.net-perf-history); $PERF_BASELINE_DIR chooses a baseline.",
    ]
    .join("\n")
}

fn run(args: Args) -> Result<i32> {
    let mut run_dir = args.run.clone();
    if run_dir.is_file() {
        run_dir = run_dir
            .parent()
            .map(|parent| parent.to_path_buf())
            .unwrap_or_else(|| PathBuf::from("."));
    }
    let run_dir = run_dir
        .canonicalize()
        .map_err(|e| Failure(format!("cannot resolve {}: {e}", run_dir.display())))?;

    let candidate = if args.archive {
        archive_entry(&run_dir, &args.label)?
    } else {
        run_dir.clone()
    };

    let baseline = match args.baseline.clone().or_else(baseline_from_env) {
        Some(path) => {
            if path.is_file() {
                path.parent().map(|p| p.to_path_buf()).unwrap_or(path)
            } else {
                path
            }
        }
        None => previous_run(&args.label, &candidate).unwrap_or_default(),
    };

    if baseline.as_os_str().is_empty() {
        let document = vs_prev_markdown(
            &[],
            "(none - this run is the first archived one)",
            &candidate.display().to_string(),
            &[],
        );
        let path = run_dir.join("vs-prev.md");
        fs::write(&path, &document)
            .map_err(|e| Failure(format!("cannot write vs-prev.md: {e}")))?;
        copy(&path, &candidate.join("vs-prev.md"))?;
        println!(
            "perf-history: archived {} as the first run; no previous run to compare",
            run_dir.display()
        );
        println!("vs-prev: {}", path.display());
        return Ok(0);
    }

    let rows = metric_rows(&read_arms(&baseline)?, &read_arms(&candidate)?);
    let ab_lines = if args.no_ab {
        Vec::new()
    } else {
        ab_verdict(&baseline, &candidate)
    };
    let document = vs_prev_markdown(
        &rows,
        &baseline.display().to_string(),
        &candidate.display().to_string(),
        &ab_lines,
    );
    let path = run_dir.join("vs-prev.md");
    fs::write(&path, &document).map_err(|e| Failure(format!("cannot write vs-prev.md: {e}")))?;
    // With --no-archive the candidate *is* the run directory, so copying the
    // document "into the archive" would copy a file onto itself and empty it.
    if candidate != run_dir {
        copy(&path, &candidate.join("vs-prev.md"))?;
    }

    let mut worse = 0usize;
    for row in rows.iter().filter(|row| row.worse) {
        worse += 1;
        println!("  WORSE {} {}: {}", row.arm, row.metric, figure(row));
    }
    for line in &ab_lines {
        println!("{line}");
    }
    println!("vs-prev: {}", path.display());

    let degradations = m1_degradations(&rows);
    if !degradations.is_empty() {
        eprintln!(
            "perf-history: M1 DEGRADATION - {} interactive metric(s) worse than the baseline, so this run is rejected",
            degradations.len()
        );
        for row in &degradations {
            eprintln!("  {} {}: {}", row.arm, row.metric, figure(row));
        }
        if !args.only_compare {
            return Ok(EXIT_M1_DEGRADATION);
        }
    }
    if worse == 0 {
        println!("no arm or metric moved worse beyond its noise band");
    }
    Ok(0)
}

fn main() {
    let argv: Vec<String> = env::args().skip(1).collect();
    let args = match parse_args(&argv) {
        Ok(args) => args,
        Err(error) => {
            eprintln!("perf-history: error: {error}");
            eprintln!("{}", usage());
            std::process::exit(1);
        }
    };
    match run(args) {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("perf-history: error: {error}");
            std::process::exit(2);
        }
    }
}
