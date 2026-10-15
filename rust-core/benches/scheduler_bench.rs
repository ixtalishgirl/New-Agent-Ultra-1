//! Criterion benchmarks for the Haley Rust core scheduler.

use criterion::{black_box, criterion_group, criterion_main, Criterion, Throughput};
use haley_rust_core::scheduler::Scheduler;

fn bench_push(c: &mut Criterion) {
    let mut group = c.benchmark_group("scheduler_push");
    for n in [1_000u64, 10_000, 100_000] {
        group.throughput(Throughput::Elements(n));
        group.bench_with_input(format!("push_{n}"), &n, |b, &n| {
            b.iter(|| {
                let s = Scheduler::new();
                for i in 0..n {
                    s.push("t", (i % 16) as i64, None, black_box("{}"));
                }
                black_box(s.len());
            });
        });
    }
    group.finish();
}

fn bench_pop(c: &mut Criterion) {
    let mut group = c.benchmark_group("scheduler_pop");
    for n in [1_000u64, 10_000, 100_000] {
        group.throughput(Throughput::Elements(n));
        group.bench_with_input(format!("pop_{n}"), &n, |b, &n| {
            b.iter_batched(
                || {
                    let s = Scheduler::new();
                    for i in 0..n {
                        s.push("t", (i % 16) as i64, None, "{}");
                    }
                    s
                },
                |s| {
                    while s.pop().is_some() {}
                    black_box(());
                },
                criterion::BatchSize::SmallInput,
            );
        });
    }
    group.finish();
}

fn bench_mixed(c: &mut Criterion) {
    c.bench_function("scheduler_mixed_push_pop_50k", |b| {
        b.iter(|| {
            let s = Scheduler::new();
            for i in 0..50_000u64 {
                s.push("t", (i % 8) as i64, None, black_box("{}"));
                if i % 3 == 0 {
                    black_box(s.pop());
                }
            }
            black_box(s.len());
        });
    });
}

criterion_group!(benches, bench_push, bench_pop, bench_mixed);
criterion_main!(benches);
