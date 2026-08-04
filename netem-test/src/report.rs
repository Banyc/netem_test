use dfsql::backend::dynamic::{Engine, Frame, Value};
use std::collections::HashMap;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
const HIST_BUCKETS: usize = 12;
const BAR_WIDTH: usize = 24;
const CDF_WIDTH: usize = 64;
const CDF_HEIGHT: usize = 11;
const GLYPHS: &[char] = &['a', 'b', 'c', 'd', 'e', 'f'];
const OVERLAP: char = '*';
const PERCENTILES: &[(&str, f64)] = &[("p50", 0.50), ("p90", 0.90), ("p99", 0.99), ("p999", 0.999)];
pub fn ab_report(title: &str, unit: &str, arms: &[(&str, &[f64])]) -> String {
    let arms: Vec<(String, Vec<f64>)> = arms
        .iter()
        .filter(|(_, vs)| !vs.is_empty())
        .map(|(label, vs)| {
            let mut sorted = vs.to_vec();
            sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
            (label.to_string(), sorted)
        })
        .collect();
    if arms.is_empty() {
        return format!("[dist {}] no samples\n", title);
    }
    let mut out = String::new();
    out.push_str(&format!("[dist {}] values in {}\n", title, unit));
    out.push_str(&stats_table(&arms));
    out.push_str(&shift_rows(&arms));
    out.push_str(&render_histogram(&arms, unit));
    out.push_str(&render_cdf(&arms, unit));
    out
}
pub fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let rank = (q * sorted.len() as f64).ceil() as usize;
    sorted[rank.saturating_sub(1).min(sorted.len() - 1)]
}
fn dfsql_summary(arms: &[(String, Vec<f64>)]) -> HashMap<String, (u64, f64, f64, f64)> {
    let rows = arms.iter().flat_map(|(label, vs)| {
        let label = label.clone();
        vs.iter()
            .map(move |v| vec![Value::String(label.as_str().into()), Value::Float(*v)])
    });
    let frame = Frame::from_rows(["arm", "v"], rows).expect("uniform two-column rows");
    let mut ex = Engine::from_frame("samples", frame);
    let stmts = dfsql::parse(
        "group arm agg (alias n len) (alias avg mean v) (alias med median v) (alias sd std v)",
    )
    .expect("valid dfsql");
    ex.execute(&stmts).expect("summary aggregation");
    let frame = ex.frame();
    let col = |name: &str| {
        frame
            .columns()
            .iter()
            .find(|c| c.name() == name)
            .unwrap_or_else(|| panic!("missing column {name}"))
            .values()
    };
    let as_f64 = |v: &Value| match v {
        Value::Float(f) => *f,
        Value::UInt(u) => *u as f64,
        Value::Int(i) => *i as f64,
        Value::Null => f64::NAN,
        other => panic!("unexpected value {other:?}"),
    };
    let mut summary = HashMap::new();
    let labels = col("arm");
    let (n, mean, median, std) = (col("n"), col("avg"), col("med"), col("sd"));
    for (i, label) in labels.iter().enumerate() {
        let Value::String(label) = label else {
            panic!("arm column must be strings");
        };
        summary.insert(
            label.to_string(),
            (
                as_f64(&n[i]) as u64,
                as_f64(&mean[i]),
                as_f64(&median[i]),
                as_f64(&std[i]),
            ),
        );
    }
    summary
}
fn stats_table(arms: &[(String, Vec<f64>)]) -> String {
    let summary = dfsql_summary(arms);
    let label_w = arms.iter().map(|(l, _)| l.len()).max().unwrap().max(4);
    let mut out = format!(
        " {:label_w$} {:>7} {:>9} {:>9} {:>9} {:>9}",
        "arm", "n", "mean", "std", "min", "max"
    );
    for (name, _) in PERCENTILES {
        out.push_str(&format!(" {name:>9}"));
    }
    out.push('\n');
    for (label, sorted) in arms {
        let (n, mean, _median, std) = summary[label];
        out.push_str(&format!(
            " {label:label_w$} {n:>7} {mean:>9.1} {std:>9.1} {:>9.1} {:>9.1}",
            sorted[0],
            sorted[sorted.len() - 1]
        ));
        for (_, q) in PERCENTILES {
            out.push_str(&format!(" {:>9.1}", percentile(sorted, *q)));
        }
        out.push('\n');
    }
    out
}
fn shift_rows(arms: &[(String, Vec<f64>)]) -> String {
    if arms.len() < 2 {
        return String::new();
    }
    let base = &arms[0];
    let mut out = String::new();
    for (label, sorted) in &arms[1..] {
        out.push_str(&format!(" shift {} vs {}:", label, base.0));
        for (name, q) in PERCENTILES {
            let b = percentile(&base.1, *q);
            let v = percentile(sorted, *q);
            if b > 0.0 {
                out.push_str(&format!(" {name} {:+.1}%", (v - b) / b * 100.0));
            } else {
                out.push_str(&format!(" {name} n/a"));
            }
        }
        out.push('\n');
    }
    out
}
fn log_axis(arms: &[(String, Vec<f64>)]) -> (f64, f64) {
    let min_pos = arms
        .iter()
        .flat_map(|(_, vs)| vs.iter())
        .copied()
        .filter(|v| *v > 0.0)
        .fold(f64::INFINITY, f64::min);
    let max = arms
        .iter()
        .map(|(_, vs)| vs[vs.len() - 1])
        .fold(f64::NEG_INFINITY, f64::max);
    if !min_pos.is_finite() {
        return (0.1, 1.0);
    }
    if max <= min_pos {
        return (min_pos / 2.0, min_pos * 2.0);
    }
    (min_pos, max)
}
fn log_edges(lo: f64, hi: f64, n: usize) -> Vec<f64> {
    let (llo, lhi) = (lo.ln(), hi.ln());
    (0..=n)
        .map(|i| (llo + (lhi - llo) * i as f64 / n as f64).exp())
        .collect()
}
fn bucket_counts(sorted: &[f64], edges: &[f64]) -> Vec<usize> {
    let mut counts = vec![0usize; edges.len() - 1];
    for &v in sorted {
        let i = edges[1..edges.len() - 1]
            .iter()
            .position(|e| v < *e)
            .unwrap_or(counts.len() - 1);
        counts[i] += 1;
    }
    counts
}
fn render_histogram(arms: &[(String, Vec<f64>)], unit: &str) -> String {
    let (lo, hi) = log_axis(arms);
    let edges = log_edges(lo, hi, HIST_BUCKETS);
    let counts: Vec<Vec<usize>> = arms
        .iter()
        .map(|(_, vs)| bucket_counts(vs, &edges))
        .collect();
    let count_w = counts
        .iter()
        .flatten()
        .max()
        .map_or(1, |m| m.to_string().len());
    let col_w = BAR_WIDTH + 1 + count_w;
    let mut out = format!(" histogram ({unit}, log buckets)\n");
    out.push_str(&format!(" {:>19}", "range"));
    for (label, _) in arms {
        out.push_str(&format!(" {label:<col_w$}"));
    }
    out.push('\n');
    for i in 0..HIST_BUCKETS {
        out.push_str(&format!(" {:>8.1}..{:>8.1}", edges[i], edges[i + 1]));
        for (arm, arm_counts) in counts.iter().enumerate() {
            let max = *counts[arm].iter().max().unwrap_or(&1) as f64;
            let count = arm_counts[i];
            let bar = if max > 0.0 {
                (count as f64 / max * BAR_WIDTH as f64).round() as usize
            } else {
                0
            };
            let bar = if count > 0 { bar.max(1) } else { 0 };
            out.push_str(&format!(
                " {:.<BAR_WIDTH$} {count:>count_w$}",
                "█".repeat(bar)
            ));
        }
        out.push('\n');
    }
    out
}
fn cdf_at(sorted: &[f64], x: f64) -> f64 {
    sorted.partition_point(|v| *v <= x) as f64 / sorted.len() as f64
}
fn render_cdf(arms: &[(String, Vec<f64>)], unit: &str) -> String {
    let (lo, hi) = log_axis(arms);
    let xs = log_edges(lo, hi, CDF_WIDTH - 1);
    let mut canvas = vec![vec![' '; CDF_WIDTH]; CDF_HEIGHT];
    for (arm, (_, sorted)) in arms.iter().enumerate() {
        let glyph = GLYPHS[arm % GLYPHS.len()];
        for (col, x) in xs.iter().enumerate() {
            let f = cdf_at(sorted, *x);
            let row = ((1.0 - f) * (CDF_HEIGHT - 1) as f64).round() as usize;
            let cell = &mut canvas[row.min(CDF_HEIGHT - 1)][col];
            *cell = if *cell == ' ' || *cell == glyph {
                glyph
            } else {
                OVERLAP
            };
        }
    }
    let mut out = format!(" CDF ({unit}, log x-axis)\n");
    for (row, line) in canvas.iter().enumerate() {
        let percent = (1.0 - row as f64 / (CDF_HEIGHT - 1) as f64) * 100.0;
        let y_label = if row == 0 || row == CDF_HEIGHT - 1 || row == (CDF_HEIGHT - 1) / 2 {
            format!("{:>4.0}%", percent).to_string()
        } else {
            "    |".to_string()
        };
        out.push_str(&format!(" {y_label} {}\n", line.iter().collect::<String>()));
    }
    let mid = (lo * hi).sqrt();
    let left = format!("{lo:.1}");
    let mid_s = format!("{mid:.1}");
    let right = format!("{hi:.1}");
    let gap = CDF_WIDTH
        .saturating_sub(left.len() + mid_s.len() + right.len())
        .max(2);
    out.push_str(&format!(
        "     {}{}{}-{}{}\n",
        left,
        " ".repeat(gap / 2),
        mid_s,
        " ".repeat(gap - gap / 2),
        right
    ));
    out.push_str(" Legend:");
    for (arm, (label, _)) in arms.iter().enumerate() {
        out.push_str(&format!(" {}{label}", GLYPHS[arm % GLYPHS.len()]));
    }
    out.push_str(&format!(" {OVERLAP}=overlap\n"));
    out
}
fn report_dir() -> PathBuf {
    std::env::var_os("NETEM_REPORT_DIR")
        .or_else(|| std::env::var_os("NETEM_DIST_DIR"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("target/netem-report"))
}
pub fn dump_csv(scenario: &str, arms: &[(&str, &[f64])]) -> io::Result<PathBuf> {
    dump_csv_to(&report_dir(), scenario, arms)
}
pub fn dump_csv_to(dir: &Path, scenario: &str, arms: &[(&str, &[f64])]) -> io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    let slug: String = scenario
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect();
    let path = dir.join(format!("{slug}.csv"));
    let mut file = std::io::BufWriter::new(std::fs::File::create(&path)?);
    writeln!(file, "series,value")?;
    for (label, vs) in arms {
        for v in *vs {
            writeln!(file, "{label},{v}")?;
        }
    }
    file.flush()?;
    Ok(path)
}
pub fn load_csv(path: &Path) -> io::Result<Vec<(String, Vec<f64>)>> {
    let content = std::fs::read_to_string(path)?;
    let mut order: Vec<String> = Vec::new();
    let mut arms: HashMap<String, Vec<f64>> = HashMap::new();
    for line in content.lines().skip(1) {
        let Some((series, value)) = line.split_once(',') else {
            continue;
        };
        let Ok(value) = value.trim().parse::<f64>() else {
            continue;
        };
        if !arms.contains_key(series) {
            order.push(series.to_string());
        }
        arms.entry(series.to_string()).or_default().push(value);
    }
    Ok(order
        .into_iter()
        .map(|s| {
            let vs = arms.remove(&s).unwrap();
            (s, vs)
        })
        .collect())
}
