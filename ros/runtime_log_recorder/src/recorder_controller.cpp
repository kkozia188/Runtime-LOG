#include "runtime_log_recorder/recorder_controller.hpp"

#include <unistd.h>

#include <cstdlib>
#include <set>
#include <string>
#include <vector>

#include "pluginlib/class_list_macros.hpp"

namespace runtime_log_recorder
{

using controller_interface::CallbackReturn;
using controller_interface::InterfaceConfiguration;
using controller_interface::interface_configuration_type;

CallbackReturn RecorderController::on_init()
{
  auto_declare<std::vector<std::string>>("interfaces", {});
  auto_declare<int>("slots", 2000);
  auto_declare<std::string>("segment_prefix", "/runtime-log");
  return CallbackReturn::SUCCESS;
}

InterfaceConfiguration RecorderController::command_interface_configuration() const
{
  return {interface_configuration_type::NONE, {}};
}

InterfaceConfiguration RecorderController::state_interface_configuration() const
{
  return {interface_configuration_type::INDIVIDUAL, interfaces_};
}

CallbackReturn RecorderController::on_configure(const rclcpp_lifecycle::State &)
{
  auto logger = get_node()->get_logger();
  recording_ = false;
  disabled_reason_.clear();
  interfaces_ = get_node()->get_parameter("interfaces").as_string_array();
  const auto slots = get_node()->get_parameter("slots").as_int();
  const auto prefix = get_node()->get_parameter("segment_prefix").as_string();
  if (interfaces_.empty()) {
    RCLCPP_ERROR(logger, "parameter 'interfaces' is empty; nothing to record");
    return CallbackReturn::ERROR;
  }
  if (std::set<std::string>(interfaces_.begin(), interfaces_.end()).size() != interfaces_.size()) {
    RCLCPP_ERROR(logger, "parameter 'interfaces' contains duplicates");
    return CallbackReturn::ERROR;
  }
  if (slots < 100 || slots > 100000) {
    RCLCPP_ERROR(logger, "parameter 'slots' must be in [100, 100000], got %ld", static_cast<long>(slots));
    return CallbackReturn::ERROR;
  }
  sources_.clear();
  sources_.reserve(interfaces_.size());  // filled in on_activate without reallocation

  const std::string shm_name = prefix + "." + std::to_string(::getpid()) + "." + get_node()->get_name();
  const char * domain = std::getenv("ROS_DOMAIN_ID");
  const std::string info = std::string("ROS_DOMAIN_ID=") + (domain != nullptr ? domain : "0") +
    " node=" + get_node()->get_namespace() + "/" + get_node()->get_name();
  std::string error;
  if (!writer_.create(shm_name, get_node()->get_name(), info, interfaces_, static_cast<uint32_t>(slots), &error)) {
    // Recording is optional: stay configurable so the control stack starts normally.
    disabled_reason_ = error;
    RCLCPP_ERROR(logger, "RUNTIME_LOG_RECORDER_DISABLED shared memory unavailable: %s", error.c_str());
    return CallbackReturn::SUCCESS;
  }
  RCLCPP_INFO(
    logger, "RUNTIME_LOG_RECORDER_CONFIGURED segment=%s fields=%zu slots=%ld bytes=%zu locked=%d", shm_name.c_str(),
    interfaces_.size(), static_cast<long>(slots), writer_.size_bytes(), writer_.locked() ? 1 : 0);
  return CallbackReturn::SUCCESS;
}

CallbackReturn RecorderController::on_activate(const rclcpp_lifecycle::State &)
{
  // Runs in the update thread on Humble: only pointer resolution, no allocation (capacity reserved).
  sources_.clear();
  for (const auto & name : interfaces_) {
    const hardware_interface::LoanedStateInterface * found = nullptr;
    for (const auto & loaned : state_interfaces_) {
      if (loaned.get_name() == name) {
        found = &loaned;
        break;
      }
    }
    if (found == nullptr) {
      RCLCPP_ERROR(get_node()->get_logger(), "state interface '%s' was not assigned", name.c_str());
      return CallbackReturn::ERROR;
    }
    sources_.push_back(found);
  }
  recording_ = writer_.ok();
  if (recording_) {
    writer_.set_state(runtime_log::WriterState::kActive);
  }
  return CallbackReturn::SUCCESS;
}

CallbackReturn RecorderController::on_deactivate(const rclcpp_lifecycle::State &)
{
  if (recording_) {
    writer_.set_state(runtime_log::WriterState::kInactive);
  }
  recording_ = false;
  sources_.clear();
  return CallbackReturn::SUCCESS;
}

CallbackReturn RecorderController::on_cleanup(const rclcpp_lifecycle::State &)
{
  writer_.close();  // the segment stays in /dev/shm for the reader
  return CallbackReturn::SUCCESS;
}

controller_interface::return_type RecorderController::update(const rclcpp::Time & time, const rclcpp::Duration & period)
{
  if (!recording_) {
    return controller_interface::return_type::OK;
  }
  double * values = writer_.begin();
  const size_t count = sources_.size();
  for (size_t i = 0; i < count; ++i) {
    values[i] = sources_[i]->get_value();
  }
  writer_.commit(time.nanoseconds(), period.nanoseconds());
  return controller_interface::return_type::OK;
}

}  // namespace runtime_log_recorder

PLUGINLIB_EXPORT_CLASS(runtime_log_recorder::RecorderController, controller_interface::ControllerInterface)
