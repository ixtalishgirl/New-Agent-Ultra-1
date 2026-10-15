//! File indexing: fast directory walk producing path/size/mtime records.
//!
//! Used to index project trees so the agent can answer "where is X"
//! without repeated filesystem walks. Pure std; no external crates.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// One indexed file.
#[derive(Debug, Clone)]
pub struct FileEntry {
    pub path: String,
    pub size_bytes: u64,
    pub modified_ms: u64,
    pub is_dir: bool,
    /// Lowercase file extension without dot ("" when none).
    pub ext: String,
}

fn ms_since_epoch(t: SystemTime) -> u64 {
    t.duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

/// Options controlling the walk.
#[derive(Debug, Clone)]
pub struct IndexOptions {
    /// Maximum recursion depth (0 = top level only).
    pub max_depth: usize,
    /// Skip hidden files/dirs (names starting with '.').
    pub skip_hidden: bool,
    /// Skip these directory names entirely.
    pub skip_dirs: Vec<String>,
    /// Only include these extensions (lowercase, no dot). Empty = all.
    pub include_exts: Vec<String>,
    /// Follow symlinks.
    pub follow_symlinks: bool,
}

impl Default for IndexOptions {
    fn default() -> Self {
        IndexOptions {
            max_depth: 12,
            skip_hidden: true,
            skip_dirs: vec![
                "node_modules".into(),
                ".git".into(),
                "__pycache__".into(),
                "target".into(),
                ".venv".into(),
                "venv".into(),
            ],
            include_exts: vec![],
            follow_symlinks: false,
        }
    }
}

/// Recursively index `root`. Returns entries (dirs included).
pub fn index_dir(root: &Path, opts: &IndexOptions) -> std::io::Result<Vec<FileEntry>> {
    let mut out = Vec::new();
    let mut stack: Vec<(PathBuf, usize)> = vec![(root.to_path_buf(), 0)];
    while let Some((dir, depth)) = stack.pop() {
        if depth > opts.max_depth {
            continue;
        }
        let rd = match fs::read_dir(&dir) {
            Ok(r) => r,
            Err(_) => continue, // permission errors: skip
        };
        for entry in rd.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            if opts.skip_hidden && name.starts_with('.') {
                continue;
            }
            let file_type = entry.file_type().ok();
            let is_symlink = file_type.map(|t| t.is_symlink()).unwrap_or(false);
            // Resolve dir-ness: use symlink_metadata unless following symlinks.
            let meta = fs::symlink_metadata(&path).ok();
            let is_dir = if opts.follow_symlinks {
                fs::metadata(&path).map(|m| m.is_dir()).unwrap_or(false)
            } else {
                file_type.map(|t| t.is_dir()).unwrap_or(false)
            };
            if is_dir {
                if opts.skip_dirs.iter().any(|d| d == &name) {
                    continue;
                }
                out.push(FileEntry {
                    path: path.to_string_lossy().to_string(),
                    size_bytes: 0,
                    modified_ms: meta
                        .as_ref()
                        .and_then(|m| m.modified().ok())
                        .map(ms_since_epoch)
                        .unwrap_or(0),
                    is_dir: true,
                    ext: String::new(),
                });
                stack.push((path, depth + 1));
            } else if !is_symlink || opts.follow_symlinks {
                let ext = Path::new(&name)
                    .extension()
                    .map(|e| e.to_string_lossy().to_lowercase())
                    .unwrap_or_default();
                if !opts.include_exts.is_empty() && !opts.include_exts.iter().any(|e| e == &ext) {
                    continue;
                }
                out.push(FileEntry {
                    path: path.to_string_lossy().to_string(),
                    size_bytes: meta.as_ref().map(|m| m.len()).unwrap_or(0),
                    modified_ms: meta
                        .as_ref()
                        .and_then(|m| m.modified().ok())
                        .map(ms_since_epoch)
                        .unwrap_or(0),
                    is_dir: false,
                    ext,
                });
            }
        }
    }
    Ok(out)
}

/// Filter entries whose path contains `query` (case-insensitive), newest first.
pub fn search<'a>(entries: &'a [FileEntry], query: &str, limit: usize) -> Vec<&'a FileEntry> {
    let q = query.to_lowercase();
    let mut hits: Vec<&FileEntry> = entries
        .iter()
        .filter(|e| e.path.to_lowercase().contains(&q))
        .collect();
    hits.sort_by(|a, b| b.modified_ms.cmp(&a.modified_ms));
    if limit > 0 && hits.len() > limit {
        hits.truncate(limit);
    }
    hits
}

/// Total bytes across file (non-dir) entries.
pub fn total_bytes(entries: &[FileEntry]) -> u64 {
    entries.iter().filter(|e| !e.is_dir).map(|e| e.size_bytes).sum()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn make_tree() -> tempfile::TempDir {
        let d = tempfile::TempDir::new().unwrap();
        let sub = d.path().join("sub");
        fs::create_dir(&sub).unwrap();
        let mut f = fs::File::create(d.path().join("main.py")).unwrap();
        writeln!(f, "print(1)").unwrap();
        let mut f = fs::File::create(sub.join("lib.rs")).unwrap();
        writeln!(f, "fn main() {{}}").unwrap();
        let mut f = fs::File::create(d.path().join(".hidden")).unwrap();
        writeln!(f, "x").unwrap();
        let nm = d.path().join("node_modules");
        fs::create_dir(&nm).unwrap();
        let mut f = fs::File::create(nm.join("big.js")).unwrap();
        writeln!(f, "y").unwrap();
        d
    }

    #[test]
    fn indexes_and_skips() {
        let d = make_tree();
        let entries = index_dir(d.path(), &IndexOptions::default()).unwrap();
        let paths: Vec<&str> = entries.iter().map(|e| e.path.as_str()).collect();
        assert!(paths.iter().any(|p| p.ends_with("main.py")));
        assert!(paths.iter().any(|p| p.ends_with("lib.rs")));
        assert!(!paths.iter().any(|p| p.ends_with(".hidden"))); // hidden skipped
        assert!(!paths.iter().any(|p| p.contains("node_modules"))); // skipped dir
    }

    #[test]
    fn ext_filter() {
        let d = make_tree();
        let mut opts = IndexOptions::default();
        opts.include_exts = vec!["py".to_string()];
        let entries = index_dir(d.path(), &opts).unwrap();
        let files: Vec<&FileEntry> = entries.iter().filter(|e| !e.is_dir).collect();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].ext, "py");
    }

    #[test]
    fn search_finds() {
        let d = make_tree();
        let entries = index_dir(d.path(), &IndexOptions::default()).unwrap();
        let hits = search(&entries, "lib", 10);
        assert_eq!(hits.len(), 1);
        assert!(hits[0].path.ends_with("lib.rs"));
        assert!(search(&entries, "zzz", 10).is_empty());
    }

    #[test]
    fn totals() {
        let d = make_tree();
        let entries = index_dir(d.path(), &IndexOptions::default()).unwrap();
        assert!(total_bytes(&entries) > 0);
    }

    #[test]
    fn max_depth_respected() {
        let d = make_tree();
        let mut opts = IndexOptions::default();
        opts.max_depth = 0;
        let entries = index_dir(d.path(), &opts).unwrap();
        assert!(!entries.iter().any(|e| e.path.ends_with("lib.rs")));
    }
}
