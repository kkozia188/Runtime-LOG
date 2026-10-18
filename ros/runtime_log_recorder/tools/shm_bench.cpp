// Micro-benchmark and sample writer for the shared-memory ring.
//
//   shm_bench --name /runtime-log.bench --fields 375 --cycles 300000 --period-us 1000 --cpu 14 --prio 80
//
// Each cycle copies `fields` doubles through a pointer table (like reading ros2_control state
// interfaces) into the ring and commits. Measures the copy+commit time and the wake-up latency.
// --period-us 0 writes as fast as possible (stress test); --pattern writes value = seq * 1000 + i.

#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "runtime_log_recorder/shm_ring.hpp"

namespace
{
struct Options
{
  std::string name = "/runtime-log.bench";
  int fields = 40;
  int slots = 2000;
  long cycles = 10000;
  long period_us = 1000;
  int cpu = -1;
  int prio = 0;
  bool pattern = false;
};

Options parse(int argc, char ** argv)
{
  Options o;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    auto next = [&]() { return std::string(i + 1 < argc ? argv[++i] : ""); };
    if (a == "--name") o.name = next();
    else if (a == "--fields") o.fields = std::stoi(next());
    else if (a == "--slots") o.slots = std::stoi(next());
    else if (a == "--cycles") o.cycles = std::stol(next());
    else if (a == "--period-us") o.period_us = std::stol(next());
    else if (a == "--cpu") o.cpu = std::stoi(next());
    else if (a == "--prio") o.prio = std::stoi(next());
    else if (a == "--pattern") o.pattern = true;
    else { std::fprintf(stderr, "unknown argument %s\n", a.c_str()); std::exit(2); }
  }
  return o;
}

void report(const char * label, std::vector<int64_t> & v)
{
  if (v.empty()) return;
  std::sort(v.begin(), v.end());
  auto at = [&](double f) { return v[std::min(v.size() - 1, static_cast<size_t>(f * (v.size() - 1)))]; };
  long over5 = std::count_if(v.begin(), v.end(), [](int64_t x) { return x > 5000; });
  std::printf("%s_ns p50=%lld p99=%lld p999=%lld max=%lld over5us=%ld n=%zu\n", label, (long long)at(0.5),
              (long long)at(0.99), (long long)at(0.999), (long long)v.back(), over5, v.size());
}
}  // namespace

int main(int argc, char ** argv)
{
  const Options o = parse(argc, argv);
  if (o.cpu >= 0) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(o.cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) { std::perror("sched_setaffinity"); return 1; }
  }
  if (o.prio > 0) {
    sched_param p{};
    p.sched_priority = o.prio;
    if (sched_setscheduler(0, SCHED_FIFO, &p) != 0) { std::perror("sched_setscheduler"); return 1; }
  }
  mlockall(MCL_CURRENT | MCL_FUTURE);

  std::vector<std::string> names;
  for (int i = 0; i < o.fields; ++i) names.push_back("axis" + std::to_string(i / 12) + "/v" + std::to_string(i % 12));
  runtime_log::ShmRingWriter writer;
  std::string error;
  if (!writer.create(o.name, "shm_bench", "benchmark", names, static_cast<uint32_t>(o.slots), &error)) {
    std::fprintf(stderr, "create failed: %s\n", error.c_str());
    return 1;
  }
  writer.set_state(runtime_log::WriterState::kActive);

  std::vector<double> source(o.fields);
  std::vector<const double *> pointers;
  for (auto & value : source) pointers.push_back(&value);
  std::vector<int64_t> write_ns, wake_ns;
  write_ns.reserve(o.cycles);
  wake_ns.reserve(o.cycles);

  timespec next{};
  clock_gettime(CLOCK_MONOTONIC, &next);
  for (long n = 1; n <= o.cycles; ++n) {
    if (o.period_us > 0) {
      next.tv_nsec += o.period_us * 1000;
      while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; ++next.tv_sec; }
      clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, nullptr);
      const int64_t woke = runtime_log::clock_ns(CLOCK_MONOTONIC);
      wake_ns.push_back(woke - (static_cast<int64_t>(next.tv_sec) * 1000000000LL + next.tv_nsec));
    }
    for (int i = 0; i < o.fields; ++i) source[i] = o.pattern ? static_cast<double>(n) * 1000.0 + i : source[i] + 1.0;
    const int64_t t0 = runtime_log::clock_ns(CLOCK_MONOTONIC);
    double * values = writer.begin();
    for (int i = 0; i < o.fields; ++i) values[i] = *pointers[i];
    writer.commit(t0, o.period_us * 1000);
    const int64_t t1 = runtime_log::clock_ns(CLOCK_MONOTONIC);
    write_ns.push_back(t1 - t0);
  }
  writer.set_state(runtime_log::WriterState::kInactive);
  std::printf("fields=%d slots=%d slot_bytes=%zu locked=%d cycles=%ld period_us=%ld cpu=%d prio=%d\n", o.fields, o.slots,
              writer.size_bytes(), writer.locked() ? 1 : 0, o.cycles, o.period_us, o.cpu, o.prio);
  report("write", write_ns);
  report("wake", wake_ns);
  return 0;
}
