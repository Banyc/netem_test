use netem_test::dist;
use std::path::PathBuf;
fn main() {
    let paths: Vec<PathBuf> = std::env::args().skip(1).map(PathBuf::from).collect();
    if paths.is_empty() {
        eprintln!("usage: ab <samples.csv> [more.csv ...]");
        std::process::exit(2);
    }
    let mut files = Vec::new();
    for path in &paths {
        let stem = path
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| path.display().to_string());
        match dist::load_csv(path) {
            Ok(arms) => files.push((stem, arms)),
            Err(error) => {
                eprintln!("failed to read {}: {}", path.display(), error);
                std::process::exit(1);
            }
        }
    }
    if files.len() == 1 {
        let (stem, arms) = &files[0];
        let arms: Vec<(&str, &[f64])> = arms
            .iter()
            .map(|(label, vs)| (label.as_str(), vs.as_slice()))
            .collect();
        print!("{}", dist::ab_report(stem, "ms", &arms));
        return;
    }
    let mut series_order: Vec<String> = Vec::new();
    for (_, arms) in &files {
        for (label, _) in arms {
            if !series_order.contains(label) {
                series_order.push(label.clone());
            }
        }
    }
    for series in &series_order {
        let labeled: Vec<(String, &[f64])> = files
            .iter()
            .filter_map(|(stem, arms)| {
                arms.iter()
                    .find(|(label, _)| label == series)
                    .map(|(_, vs)| (format!("{stem}:{series}"), vs.as_slice()))
            })
            .collect();
        let arms: Vec<(&str, &[f64])> = labeled
            .iter()
            .map(|(label, vs)| (label.as_str(), *vs))
            .collect();
        print!("{}", dist::ab_report(series, "ms", &arms));
        println!();
    }
}
