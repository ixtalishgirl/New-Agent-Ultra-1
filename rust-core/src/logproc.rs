//! Large log processing: fast line counting, pattern search, and tail.
//!
//! Operates on raw bytes with memchr-style scanning (via `Iterator::position`
//! on slices) to stay fast on multi-hundred-MB files without loading
//! everything into a String.

use std::fs::File;
use std::io::{BufReader, Read};
use std::path::Path;

const CHUNK: usize = 256 * 1024;

/// Count lines (`\n`) in a file without loading it fully.
pub fn count_lines(path: &Path) -> std::io::Result<u64> {
    let f = File::open(path)?;
    let mut r = BufReader::with_capacity(CHUNK, f);
    let mut buf = vec![0u8; CHUNK];
    let mut count = 0u64;
    let mut last_byte = 0u8;
    let mut any = false;
    loop {
        let n = r.read(&mut buf)?;
        if n == 0 {
            break;
        }
        any = true;
        count += buf[..n].iter().filter(|&&b| b == b'\n').count() as u64;
        last_byte = buf[n - 1];
    }
    // Count a trailing line that lacks a final newline.
    if any && last_byte != b'\n' {
        count += 1;
    }
    Ok(count)
}

/// Count lines containing `needle` (byte substring, case-sensitive).
pub fn grep_count(path: &Path, needle: &[u8]) -> std::io::Result<u64> {
    if needle.is_empty() {
        return count_lines(path);
    }
    let f = File::open(path)?;
    let mut r = BufReader::with_capacity(CHUNK, f);
    let mut buf = vec![0u8; CHUNK];
    let mut carry: Vec<u8> = Vec::new();
    let mut count = 0u64;
    loop {
        let n = r.read(&mut buf)?;
        if n == 0 {
            break;
        }
        // Prepend carry from previous chunk so matches spanning chunks are found.
        let mut window: Vec<u8> = Vec::with_capacity(carry.len() + n);
        window.extend_from_slice(&carry);
        window.extend_from_slice(&buf[..n]);
        // Scan line by line.
        let mut start = 0;
        for (i, &b) in window.iter().enumerate() {
            if b == b'\n' {
                if window[start..i].windows(needle.len()).any(|w| w == needle) {
                    count += 1;
                }
                start = i + 1;
            }
        }
        // Save incomplete trailing line as carry (bounded by needle len + line sanity cap).
        carry.clear();
        let tail = &window[start..];
        let keep = tail.len().min(needle.len().saturating_add(8192));
        carry.extend_from_slice(&tail[tail.len().saturating_sub(keep)..]);
        // Last partial line at EOF still needs checking on final iteration.
        if n < CHUNK {
            if !tail.is_empty() && tail.windows(needle.len()).any(|w| w == needle) {
                count += 1;
            }
            carry.clear(); // consumed
            break;
        }
    }
    // Edge: file size was an exact multiple of CHUNK, so the final partial
    // line sits in `carry` and the loop exited via `n == 0`.
    if !carry.is_empty() && carry.windows(needle.len()).any(|w| w == needle) {
        count += 1;
    }
    Ok(count)
}

/// Return the last `n` lines of a file (lossy UTF-8).
pub fn tail_lines(path: &Path, n: usize) -> std::io::Result<Vec<String>> {
    if n == 0 {
        return Ok(Vec::new());
    }
    let data = std::fs::read(path)?;
    let text = String::from_utf8_lossy(&data);
    let lines: Vec<&str> = text.lines().collect();
    let start = lines.len().saturating_sub(n);
    Ok(lines[start..].iter().map(|s| s.to_string()).collect())
}

/// Find byte offsets of the first `limit` occurrences of `needle`.
pub fn find_offsets(path: &Path, needle: &[u8], limit: usize) -> std::io::Result<Vec<u64>> {
    let data = std::fs::read(path)?;
    let mut out = Vec::new();
    if needle.is_empty() || limit == 0 {
        return Ok(out);
    }
    let mut from = 0;
    while out.len() < limit {
        match data[from..].windows(needle.len()).position(|w| w == needle) {
            Some(pos) => {
                out.push((from + pos) as u64);
                from += pos + 1;
            }
            None => break,
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_tmp(lines: &[&str], trailing_newline: bool) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        for (i, l) in lines.iter().enumerate() {
            write!(f, "{l}").unwrap();
            if trailing_newline || i + 1 < lines.len() {
                writeln!(f).unwrap();
            }
        }
        f.flush().unwrap();
        f
    }

    #[test]
    fn counts_lines() {
        let f = write_tmp(&["a", "b", "c"], true);
        assert_eq!(count_lines(f.path()).unwrap(), 3);
        let f2 = write_tmp(&["a", "b", "c"], false);
        assert_eq!(count_lines(f2.path()).unwrap(), 3);
        let f3 = write_tmp(&[], true);
        assert_eq!(count_lines(f3.path()).unwrap(), 0);
    }

    #[test]
    fn grep_counts_matches() {
        let f = write_tmp(&["error one", "ok line", "another error", "ok"], true);
        assert_eq!(grep_count(f.path(), b"error").unwrap(), 2);
        assert_eq!(grep_count(f.path(), b"missing").unwrap(), 0);
        assert_eq!(grep_count(f.path(), b"").unwrap(), 4);
    }

    #[test]
    fn tail_returns_last_n() {
        let f = write_tmp(&["1", "2", "3", "4", "5"], true);
        assert_eq!(tail_lines(f.path(), 2).unwrap(), vec!["4", "5"]);
        assert_eq!(tail_lines(f.path(), 99).unwrap().len(), 5);
        assert!(tail_lines(f.path(), 0).unwrap().is_empty());
    }

    #[test]
    fn offsets_found() {
        let f = write_tmp(&["ab", "xab", "ab"], true);
        let offs = find_offsets(f.path(), b"ab", 10).unwrap();
        assert_eq!(offs.len(), 3);
        assert!(offs.windows(2).all(|w| w[0] < w[1]));
    }

    #[test]
    fn large_file_perf_sanity() {
        // 200k lines; must complete quickly.
        let mut f = tempfile::NamedTempFile::new().unwrap();
        for i in 0..200_000 {
            writeln!(f, "line {i} with some content").unwrap();
        }
        f.flush().unwrap();
        let n = count_lines(f.path()).unwrap();
        assert_eq!(n, 200_000);
        assert_eq!(grep_count(f.path(), b"line 199999").unwrap(), 1);
    }
}
