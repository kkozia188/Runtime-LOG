// Writes the canonical v1 format sample: fixed writer/info/fields, 4 slots, two committed cycles.
// Writer pid and creation times (bytes 32..55) are zeroed so the bytes are reproducible.
// Usage: format_sample <out_file>   (the same program exists in robot_driver; both compare to the
// same golden file, so the two copies of shm_ring.hpp cannot drift apart unnoticed)
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include SHM_RING_HEADER

int main(int argc, char ** argv)
{
  if (argc != 2) return 2;
  const std::string name = "/runtime-log.formatcheck-" + std::to_string(getpid());
  runtime_log::ShmRingWriter writer;
  std::string error;
  if (!writer.create(name, "format", "check", {"a", "b/c"}, 4, &error)) {
    std::fprintf(stderr, "%s\n", error.c_str());
    return 1;
  }
  writer.set_state(runtime_log::WriterState::kActive);
  double * values = writer.begin();
  values[0] = 1.5;
  values[1] = -2.0;
  writer.commit(1000, 1000000);
  values = writer.begin();
  values[0] = 3.0;
  values[1] = 4.0;
  writer.commit(2000, 1000000);
  const size_t size = writer.size_bytes();
  writer.close();
  const int fd = shm_open(name.c_str(), O_RDONLY, 0);
  std::vector<unsigned char> bytes(size);
  if (fd < 0 || pread(fd, bytes.data(), size, 0) != static_cast<ssize_t>(size)) return 1;
  close(fd);
  shm_unlink(name.c_str());
  std::memset(bytes.data() + 32, 0, 24);
  FILE * out = std::fopen(argv[1], "wb");
  if (out == nullptr) return 1;
  std::fwrite(bytes.data(), 1, size, out);
  std::fclose(out);
  return 0;
}
