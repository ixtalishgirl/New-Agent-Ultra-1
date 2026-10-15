//! Resource usage monitoring: CPU and memory sampling on Linux.
//!
//! Reads `/proc/stat` and `/proc/<pid>/stat` + `/proc/meminfo` directly,
//! so sampling is cheap (no subprocess, no external crates).

use std::fs;
use std::time::{Duration, Instant};

/// One CPU sample: totals from /proc/stat.
#[derive(Debug, Clone, Copy, Default)]
struct CpuSample {
    total: u64,
    idle: u64,
}

/// Read aggregate CPU counters from /proc/stat.
fn read_cpu() -> Option<CpuSample> {
    let content = fs::read_to_string("/proc/stat").ok()?;
    let line = content.lines().next()?;
    let mut parts = line.split_whitespace();
    if parts.next()? != "cpu" {
        return None;
    }
    let nums: Vec<u64> = parts.filter_map(|p| p.parse().ok()).collect();
    if nums.len() < 4 {
        return None;
    }
    let idle = nums[3] + nums.get(4).copied().unwrap_or(0); // idle + iowait
    let total: u64 = nums.iter().sum();
    Some(CpuSample { total, idle })
}

/// System-wide memory from /proc/meminfo, in KiB.
#[derive(Debug, Clone, Copy, Default)]
pub struct MemInfo {
    pub total_kb: u64,
    pub available_kb: u64,
    pub used_kb: u64,
    pub used_percent: f64,
}

fn read_meminfo() -> Option<MemInfo> {
    let content = fs::read_to_string("/proc/meminfo").ok()?;
    let mut total = 0u64;
    let mut available = 0u64;
    for line in content.lines() {
        let mut it = line.split_whitespace();
        let key = it.next().unwrap_or("");
        let val: u64 = it.next().unwrap_or("0").parse().unwrap_or(0);
        match key {
            "MemTotal:" => total = val,
            "MemAvailable:" => available = val,
            _ => {}
        }
    }
    if total == 0 {
        return None;
    }
    let used = total.saturating_sub(available);
    Some(MemInfo {
        total_kb: total,
        available_kb: available,
        used_kb: used,
        used_percent: used as f64 / total as f64 * 100.0,
    })
}

/// Process RSS in KiB from /proc/self/statm.
fn read_self_rss_kb() -> Option<u64> {
    let content = fs::read_to_string("/proc/self/statm").ok()?;
    let rss_pages: u64 = content.split_whitespace().nth(1)?.parse().ok()?;
    let page_kb = 4; // standard 4 KiB pages on x86_64
    Some(rss_pages * page_kb)
}

/// Point-in-time resource snapshot.
#[derive(Debug, Clone, Default)]
pub struct ResourceSnapshot {
    /// System CPU usage percent (0-100) measured over the sampling interval.
    pub cpu_percent: f64,
    /// System memory info.
    pub mem: MemInfo,
    /// Current process RSS in KiB.
    pub self_rss_kb: u64,
    /// Number of logical CPUs.
    pub cpu_count: usize,
}

/// Lightweight monitor: call `sample()` periodically; CPU percent is
/// computed between consecutive samples.
#[derive(Debug)]
pub struct ResourceMonitor {
    last_cpu: Option<CpuSample>,
    last_at: Option<Instant>,
    cpu_count: usize,
}

impl ResourceMonitor {
    pub fn new() -> Self {
        ResourceMonitor {
            last_cpu: None,
            last_at: None,
            cpu_count: std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(1),
        }
    }

    /// Take a snapshot. The first call returns cpu_percent = 0.0 (no baseline yet).
    pub fn sample(&mut self) -> ResourceSnapshot {
        let cpu_percent = match (read_cpu(), self.last_cpu, self.last_at) {
            (Some(cur), Some(prev), Some(at)) => {
                let dt = at.elapsed();
                if dt < Duration::from_millis(10) {
                    0.0
                } else {
                    let total_d = cur.total.saturating_sub(prev.total) as f64;
                    let idle_d = cur.idle.saturating_sub(prev.idle) as f64;
                    if total_d <= 0.0 {
                        0.0
                    } else {
                        ((total_d - idle_d) / total_d * 100.0).clamp(0.0, 100.0)
                    }
                }
            }
            _ => 0.0,
        };
        self.last_cpu = read_cpu();
        self.last_at = Some(Instant::now());

        ResourceSnapshot {
            cpu_percent,
            mem: read_meminfo().unwrap_or_default(),
            self_rss_kb: read_self_rss_kb().unwrap_or(0),
            cpu_count: self.cpu_count,
        }
    }

    /// True when system memory pressure exceeds `threshold_percent`.
    pub fn memory_pressure(&mut self, threshold_percent: f64) -> bool {
        self.sample().mem.used_percent >= threshold_percent
    }
}

impl Default for ResourceMonitor {
    fn default() -> Self {
        Self::new()
    }
}

/// One-shot helpers (no monitor state).
pub fn quick_cpu_count() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
}

pub fn quick_mem_info() -> MemInfo {
    read_meminfo().unwrap_or_default()
}

pub fn quick_self_rss_kb() -> u64 {
    read_self_rss_kb().unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn meminfo_parses() {
        let m = read_meminfo().expect("must parse /proc/meminfo on linux");
        assert!(m.total_kb > 0);
        assert!(m.used_percent >= 0.0 && m.used_percent <= 100.0);
    }

    #[test]
    fn cpu_sample_parses() {
        let s = read_cpu().expect("must parse /proc/stat on linux");
        assert!(s.total > 0);
        assert!(s.idle <= s.total);
    }

    #[test]
    fn monitor_samples() {
        let mut mon = ResourceMonitor::new();
        let a = mon.sample();
        assert_eq!(a.cpu_percent, 0.0); // no baseline yet
        std::thread::sleep(Duration::from_millis(120));
        // burn a little CPU so the delta is measurable
        let mut x = 0u64;
        for i in 0..5_000_000u64 {
            x = x.wrapping_add(i);
        }
        std::hint::black_box(x);
        let b = mon.sample();
        assert!(b.cpu_percent >= 0.0 && b.cpu_percent <= 100.0);
        assert!(b.self_rss_kb > 0);
        assert!(b.cpu_count >= 1);
    }

    #[test]
    fn quick_helpers() {
        assert!(quick_cpu_count() >= 1);
        assert!(quick_mem_info().total_kb > 0);
    }
}
