#pragma once

#include <string>
#include <vector>

#include "controller_interface/controller_interface.hpp"
#include "hardware_interface/loaned_state_interface.hpp"
#include "runtime_log_recorder/shm_ring.hpp"

namespace runtime_log_recorder
{

// Copies the configured state interfaces into a shared-memory ring every cycle.
//
// Real-time rules: update() only reads the pre-resolved interfaces and copies doubles into the
// pre-faulted, locked segment (no allocation, lock, system call or logging). The segment is
// created in on_configure (non-real-time). If it cannot be created the controller still
// configures and activates but records nothing, so it never blocks the rest of the stack.
// It claims no command interfaces and nothing depends on it.
class RecorderController : public controller_interface::ControllerInterface
{
public:
  controller_interface::CallbackReturn on_init() override;
  controller_interface::InterfaceConfiguration command_interface_configuration() const override;
  controller_interface::InterfaceConfiguration state_interface_configuration() const override;
  controller_interface::CallbackReturn on_configure(const rclcpp_lifecycle::State & previous_state) override;
  controller_interface::CallbackReturn on_activate(const rclcpp_lifecycle::State & previous_state) override;
  controller_interface::CallbackReturn on_deactivate(const rclcpp_lifecycle::State & previous_state) override;
  controller_interface::CallbackReturn on_cleanup(const rclcpp_lifecycle::State & previous_state) override;
  controller_interface::return_type update(const rclcpp::Time & time, const rclcpp::Duration & period) override;

  // For tests and diagnostics.
  bool recording() const { return recording_; }
  const std::string & segment_name() const { return writer_.name(); }
  const std::string & disabled_reason() const { return disabled_reason_; }

private:
  std::vector<std::string> interfaces_;
  std::vector<const hardware_interface::LoanedStateInterface *> sources_;
  runtime_log::ShmRingWriter writer_;
  bool recording_{false};
  std::string disabled_reason_;
};

}  // namespace runtime_log_recorder
