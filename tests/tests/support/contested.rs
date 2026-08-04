// ───────────────────────────── dyn traffic summary ───────────────────────

use super::stats::percentile;

pub struct DynTrafficResult {
    pub small_latencies: Vec<f64>,
    pub burst_latencies: Vec<f64>,
    pub sent: u64,
    pub received: u64,
    pub bulk_bytes: u64,
}

pub fn dyn_run_secs() -> u64 {
    std::env::var("DYN_RUN_SECS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(15)
}

pub fn summarize(label: &str, results: &[DynTrafficResult]) {
    let mut all_small: Vec<f64> = results
        .iter()
        .flat_map(|r| r.small_latencies.clone())
        .collect();
    let mut all_burst: Vec<f64> = results
        .iter()
        .flat_map(|r| r.burst_latencies.clone())
        .collect();
    all_small.sort_by(|a, b| a.partial_cmp(b).unwrap());
    all_burst.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let delivery = if results.iter().map(|r| r.sent).sum::<u64>() == 0 {
        0.0
    } else {
        results.iter().map(|r| r.received).sum::<u64>() as f64
            / results.iter().map(|r| r.sent).sum::<u64>() as f64
    };
    let bulk_total: u64 = results.iter().map(|r| r.bulk_bytes).sum();
    let bulk_secs = results.len() as f64 * dyn_run_secs() as f64;

    eprintln!(
        "[dyn {label}] small p50/p90/p99={:.0}/{:.0}/{:.0} ms  burst_p50={:.0} ms  bulk={:.3} MiB/s  delivery={:.3}",
        percentile(&all_small, 0.50),
        percentile(&all_small, 0.90),
        percentile(&all_small, 0.99),
        percentile(&all_burst, 0.50),
        bulk_total as f64 / (1024.0 * 1024.0) / bulk_secs,
        delivery,
    );

    let arms = [
        ("small", all_small.as_slice()),
        ("burst", all_burst.as_slice()),
    ];
    if let Ok(path) = netem_test::report::dump_csv(&format!("dyn_{label}"), &arms) {
        eprintln!("[dyn {label}] samples: {}", path.display());
    }
    eprintln!("{}", netem_test::report::ab_report(label, "ms", &arms));

    assert!(
        delivery > 0.80,
        "[dyn {label}] delivery too low: {delivery:.3}"
    );
    assert!(
        percentile(&all_small, 0.50) > 0.0,
        "[dyn {label}] p50 must be positive finite"
    );
}
