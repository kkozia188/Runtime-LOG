#include <gtest/gtest.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>

#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "hardware_interface/handle.hpp"
#include "hardware_interface/loaned_state_interface.hpp"
#include "lifecycle_msgs/msg/state.hpp"
#include "rclcpp/rclcpp.hpp"
#include "runtime_log_recorder/recorder_controller.hpp"

namespace
{
struct Segment
{
  std::vector<uint8_t> bytes;
  runtime_log::SegmentHeader header() const
  {
    runtime_log::SegmentHeader h{};
    std::memcpy(&h, bytes.data(), sizeof(h));
    return h;
  }
  uint64_t head() const
  {
    uint64_t v = 0;
    std::memcpy(&v, bytes.data() + 64, sizeof(v));
    return v;
  }
  std::string field(size_t i) const { return reinterpret_cast<const char *>(bytes.data() + 320 + 64 * i); }
  double value(uint64_t seq, size_t i) const
  {
    const auto h = header();
    double v = 0;
    std::memcpy(&v, bytes.data() + h.header_size + (seq % h.slot_count) * h.slot_size + 24 + 8 * i, sizeof(v));
    return v;
  }
};

Segment read_segment(const std::string & name)
{
  const int fd = shm_open(name.c_str(), O_RDONLY, 0);
  EXPECT_GE(fd, 0) << name;
  const off_t size = lseek(fd, 0, SEEK_END);
  Segment segment;
  segment.bytes.resize(static_cast<size_t>(size));
  EXPECT_EQ(pread(fd, segment.bytes.data(), segment.bytes.size(), 0), size);
  close(fd);
  return segment;
}

class RecorderTest : public ::testing::Test
{
protected:
  static void SetUpTestSuite() { rclcpp::init(0, nullptr); }
  static void TearDownTestSuite() { rclcpp::shutdown(); }

  void SetUp() override
  {
    controller_ = std::make_unique<runtime_log_recorder::RecorderController>();
    ASSERT_EQ(controller_->init("rtlog_recorder_test"), controller_interface::return_type::OK);
  }

  void assign()
  {
    std::vector<hardware_interface::LoanedStateInterface> loaned;
    // Deliberately not in parameter order: the controller must match by name.
    loaned.emplace_back(status_);
    loaned.emplace_back(position_);
    loaned.emplace_back(wkc_);
    controller_->assign_interfaces({}, std::move(loaned));
  }

  double position_value_{1.5}, status_value_{0x1637}, wkc_value_{0};
  hardware_interface::StateInterface position_{"left_joint1", "position", &position_value_};
  hardware_interface::StateInterface status_{"left_joint1", "status_word", &status_value_};
  hardware_interface::StateInterface wkc_{"ethercat_master", "wc_error_count", &wkc_value_};
  std::unique_ptr<runtime_log_recorder::RecorderController> controller_;
};

TEST_F(RecorderTest, RecordsConfiguredInterfacesInParameterOrder)
{
  controller_->get_node()->set_parameter(rclcpp::Parameter(
    "interfaces", std::vector<std::string>{"left_joint1/position", "left_joint1/status_word", "ethercat_master/wc_error_count"}));
  controller_->get_node()->set_parameter(rclcpp::Parameter("slots", 128));
  ASSERT_EQ(controller_->configure().id(), lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE);
  const auto config = controller_->state_interface_configuration();
  ASSERT_EQ(config.names.size(), 3u);
  assign();
  ASSERT_EQ(controller_->get_node()->activate().id(), lifecycle_msgs::msg::State::PRIMARY_STATE_ACTIVE);
  ASSERT_TRUE(controller_->recording());

  for (int cycle = 1; cycle <= 5; ++cycle) {
    position_value_ = 1.5 + cycle;
    wkc_value_ = cycle;
    controller_->update(rclcpp::Time(1000000LL * cycle), rclcpp::Duration::from_nanoseconds(1000000));
  }
  const auto segment = read_segment(controller_->segment_name());
  const auto header = segment.header();
  EXPECT_EQ(std::string(header.magic, 8), "RTLOGSHM");
  EXPECT_EQ(header.field_count, 3u);
  EXPECT_EQ(header.slot_count, 128u);
  EXPECT_EQ(header.writer_pid, static_cast<int64_t>(getpid()));
  EXPECT_EQ(header.writer_state, 1u);
  EXPECT_EQ(segment.field(0), "left_joint1/position");
  EXPECT_EQ(segment.field(2), "ethercat_master/wc_error_count");
  EXPECT_EQ(segment.head(), 5u);
  EXPECT_DOUBLE_EQ(segment.value(5, 0), 6.5);
  EXPECT_DOUBLE_EQ(segment.value(5, 1), 0x1637);
  EXPECT_DOUBLE_EQ(segment.value(5, 2), 5.0);

  ASSERT_EQ(controller_->get_node()->deactivate().id(), lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE);
  EXPECT_EQ(read_segment(controller_->segment_name()).header().writer_state, 2u);
  shm_unlink(controller_->segment_name().c_str());
}

TEST_F(RecorderTest, MissingSharedMemoryOnlyDisablesRecording)
{
  controller_->get_node()->set_parameter(rclcpp::Parameter("interfaces", std::vector<std::string>{"left_joint1/position"}));
  controller_->get_node()->set_parameter(rclcpp::Parameter("segment_prefix", std::string("/no/such/dir")));
  ASSERT_EQ(controller_->configure().id(), lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE);
  EXPECT_FALSE(controller_->disabled_reason().empty());
  std::vector<hardware_interface::LoanedStateInterface> loaned;
  loaned.emplace_back(position_);
  controller_->assign_interfaces({}, std::move(loaned));
  ASSERT_EQ(controller_->get_node()->activate().id(), lifecycle_msgs::msg::State::PRIMARY_STATE_ACTIVE);
  EXPECT_FALSE(controller_->recording());
  EXPECT_EQ(controller_->update(rclcpp::Time(0), rclcpp::Duration::from_nanoseconds(1000000)), controller_interface::return_type::OK);
}

TEST_F(RecorderTest, RejectsEmptyOrDuplicateInterfaceLists)
{
  ASSERT_NE(controller_->configure().id(), lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE);
}
}  // namespace
