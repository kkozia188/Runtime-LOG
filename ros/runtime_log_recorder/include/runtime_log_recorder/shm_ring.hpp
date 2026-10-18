// Runtime-LOG shared-memory transport ring: one writer on the real-time core, one reader elsewhere.
//
// Layout (little-endian, x86-64 only; see docs/shm-format.md):
//   [0, 64)        line 0: magic, version, sizes, writer state, writer pid, creation times
//   [64, 128)      line 1: head = last committed cycle sequence (written by the writer only)
//   [128, 192)     writer name (NUL-terminated)
//   [192, 320)     free-form info (NUL-terminated)
//   [320, ...)     field table: 64 B per field {char name[56]; u32 offset; u8 type; u8 pad[3]}
//   [header_size, header_size + slot_count * slot_size)   slots
// Slot: {u64 seq; i64 time_ns; i64 period_ns; f64 values[field_count]} padded to 64 B.
//
// Writer per cycle (no allocation, lock or system call):
//   slot.seq = BUSY; payload; slot.seq = n (release); head = n (release).
// Reader: s1 = slot.seq (acquire); copy; s2 = slot.seq; valid only if s1 == s2 == expected n.
// The writer never waits for and never reads anything written by the reader.

#pragma once

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

namespace runtime_log
{

constexpr char kMagic[8] = {'R', 'T', 'L', 'O', 'G', 'S', 'H', 'M'};
constexpr uint32_t kVersion = 1;
constexpr uint64_t kBusy = UINT64_MAX;
constexpr size_t kCacheLine = 64;
constexpr size_t kFieldEntry = 64;
constexpr size_t kFieldTableOffset = 320;
constexpr size_t kFieldNameMax = 56;
constexpr size_t kSlotHeader = 24;  // seq, time_ns, period_ns
constexpr uint8_t kTypeF64 = 1;

enum class WriterState : uint32_t { kCreated = 0, kActive = 1, kInactive = 2 };

#pragma pack(push, 1)
struct SegmentHeader
{
  char magic[8];
  uint32_t version;
  uint32_t header_size;
  uint32_t slot_count;
  uint32_t slot_size;
  uint32_t field_count;
  uint32_t writer_state;
  int64_t writer_pid;
  int64_t created_realtime_ns;
  int64_t created_monotonic_ns;
};
struct FieldEntry
{
  char name[kFieldNameMax];
  uint32_t offset;  // byte offset of the value inside the slot
  uint8_t type;
  uint8_t pad[3];
};
#pragma pack(pop)
static_assert(sizeof(SegmentHeader) == 56, "line 0 layout");
static_assert(sizeof(FieldEntry) == kFieldEntry, "field entry layout");

inline size_t round_up(size_t value, size_t multiple) { return (value + multiple - 1) / multiple * multiple; }

inline int64_t clock_ns(clockid_t clock)
{
  timespec ts{};
  clock_gettime(clock, &ts);
  return static_cast<int64_t>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

class ShmRingWriter
{
public:
  ShmRingWriter() = default;
  ShmRingWriter(const ShmRingWriter &) = delete;
  ShmRingWriter & operator=(const ShmRingWriter &) = delete;
  ~ShmRingWriter() { close(); }

  // Non-real-time: create, size, map, pre-fault and lock the segment. `shm_name` starts with '/'.
  // An existing segment of the same name is replaced. Returns false and sets `error` on failure.
  bool create(
    const std::string & shm_name, const std::string & writer_name, const std::string & info,
    const std::vector<std::string> & fields, uint32_t slot_count, std::string * error)
  {
    close();
    if (fields.empty() || slot_count < 2) {
      return fail(error, "need at least one field and two slots");
    }
    for (const auto & field : fields) {
      if (field.size() >= kFieldNameMax) {
        return fail(error, "field name too long: " + field);
      }
    }
    header_size_ = round_up(kFieldTableOffset + kFieldEntry * fields.size(), 4096);
    slot_size_ = static_cast<uint32_t>(round_up(kSlotHeader + sizeof(double) * fields.size(), kCacheLine));
    slot_count_ = slot_count;
    field_count_ = static_cast<uint32_t>(fields.size());
    size_ = header_size_ + static_cast<size_t>(slot_size_) * slot_count_;

    ::shm_unlink(shm_name.c_str());
    const int fd = ::shm_open(shm_name.c_str(), O_CREAT | O_EXCL | O_RDWR, 0640);
    if (fd < 0) {
      return fail(error, "shm_open " + shm_name + ": " + std::strerror(errno));
    }
    if (::ftruncate(fd, static_cast<off_t>(size_)) != 0) {
      const std::string reason = std::strerror(errno);
      ::close(fd);
      ::shm_unlink(shm_name.c_str());
      return fail(error, "ftruncate: " + reason);
    }
    void * base = ::mmap(nullptr, size_, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    ::close(fd);
    if (base == MAP_FAILED) {
      ::shm_unlink(shm_name.c_str());
      return fail(error, std::string("mmap: ") + std::strerror(errno));
    }
    base_ = static_cast<uint8_t *>(base);
    std::memset(base_, 0, size_);  // touches every page now, not in the real-time loop
    locked_ = ::mlock(base_, size_) == 0;

    auto * header = reinterpret_cast<SegmentHeader *>(base_);
    std::memcpy(header->magic, kMagic, sizeof(kMagic));
    header->version = kVersion;
    header->header_size = static_cast<uint32_t>(header_size_);
    header->slot_count = slot_count_;
    header->slot_size = slot_size_;
    header->field_count = field_count_;
    header->writer_state = static_cast<uint32_t>(WriterState::kCreated);
    header->writer_pid = static_cast<int64_t>(::getpid());
    header->created_realtime_ns = clock_ns(CLOCK_REALTIME);
    header->created_monotonic_ns = clock_ns(CLOCK_MONOTONIC);
    std::strncpy(reinterpret_cast<char *>(base_ + 128), writer_name.c_str(), 63);
    std::strncpy(reinterpret_cast<char *>(base_ + 192), info.c_str(), 127);
    for (size_t i = 0; i < fields.size(); ++i) {
      auto * entry = reinterpret_cast<FieldEntry *>(base_ + kFieldTableOffset + kFieldEntry * i);
      std::strncpy(entry->name, fields[i].c_str(), kFieldNameMax - 1);
      entry->offset = static_cast<uint32_t>(kSlotHeader + sizeof(double) * i);
      entry->type = kTypeF64;
    }
    head_ = reinterpret_cast<uint64_t *>(base_ + kCacheLine);
    slots_ = base_ + header_size_;
    next_ = 1;
    name_ = shm_name;
    return true;
  }

  // Non-real-time: unmap; the segment itself stays in /dev/shm for post-mortem reading.
  void close()
  {
    if (base_ != nullptr) {
      if (locked_) {
        ::munlock(base_, size_);
      }
      ::munmap(base_, size_);
    }
    base_ = nullptr;
    head_ = nullptr;
    slots_ = nullptr;
    locked_ = false;
  }

  void set_state(WriterState state)
  {
    if (base_ != nullptr) {
      __atomic_store_n(&reinterpret_cast<SegmentHeader *>(base_)->writer_state, static_cast<uint32_t>(state), __ATOMIC_RELEASE);
    }
  }

  // Real-time: returns the value array of the next slot, already marked busy.
  double * begin()
  {
    current_ = slots_ + static_cast<size_t>(next_ % slot_count_) * slot_size_;
    __atomic_store_n(reinterpret_cast<uint64_t *>(current_), kBusy, __ATOMIC_RELAXED);
    __atomic_thread_fence(__ATOMIC_RELEASE);
    return reinterpret_cast<double *>(current_ + kSlotHeader);
  }

  // Real-time: publish the slot started by begin().
  void commit(int64_t time_ns, int64_t period_ns)
  {
    std::memcpy(current_ + 8, &time_ns, sizeof(time_ns));
    std::memcpy(current_ + 16, &period_ns, sizeof(period_ns));
    __atomic_store_n(reinterpret_cast<uint64_t *>(current_), next_, __ATOMIC_RELEASE);
    __atomic_store_n(head_, next_, __ATOMIC_RELEASE);
    ++next_;
  }

  bool ok() const { return base_ != nullptr; }
  bool locked() const { return locked_; }
  uint32_t field_count() const { return field_count_; }
  size_t size_bytes() const { return size_; }
  const std::string & name() const { return name_; }

private:
  static bool fail(std::string * error, const std::string & message)
  {
    if (error != nullptr) {
      *error = message;
    }
    return false;
  }

  uint8_t * base_{nullptr};
  uint64_t * head_{nullptr};
  uint8_t * slots_{nullptr};
  uint8_t * current_{nullptr};
  size_t size_{0};
  size_t header_size_{0};
  uint32_t slot_size_{0};
  uint32_t slot_count_{0};
  uint32_t field_count_{0};
  uint64_t next_{1};
  bool locked_{false};
  std::string name_;
};

}  // namespace runtime_log
