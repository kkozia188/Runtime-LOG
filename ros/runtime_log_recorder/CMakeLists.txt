cmake_minimum_required(VERSION 3.16)
project(runtime_log_recorder LANGUAGES CXX)

find_package(ament_cmake REQUIRED)
set(dependencies
  controller_interface
  hardware_interface
  pluginlib
  rclcpp
  rclcpp_lifecycle
)
foreach(dependency IN LISTS dependencies)
  find_package(${dependency} REQUIRED)
endforeach()

add_library(runtime_log_recorder SHARED src/recorder_controller.cpp)
target_compile_features(runtime_log_recorder PUBLIC cxx_std_17)
target_compile_options(runtime_log_recorder PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror)
target_include_directories(runtime_log_recorder PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)
ament_target_dependencies(runtime_log_recorder PUBLIC ${dependencies})
target_link_libraries(runtime_log_recorder PUBLIC rt)
pluginlib_export_plugin_description_file(controller_interface runtime_log_recorder_plugins.xml)

# Stand-alone micro-benchmark / sample writer (no ROS dependency).
add_executable(runtime_log_shm_bench tools/shm_bench.cpp)
target_compile_features(runtime_log_shm_bench PRIVATE cxx_std_17)
target_include_directories(runtime_log_shm_bench PRIVATE include)
target_link_libraries(runtime_log_shm_bench rt)

install(TARGETS runtime_log_recorder EXPORT export_runtime_log_recorder
  ARCHIVE DESTINATION lib LIBRARY DESTINATION lib RUNTIME DESTINATION bin)
install(TARGETS runtime_log_shm_bench RUNTIME DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY include/ DESTINATION include)
install(FILES runtime_log_recorder_plugins.xml DESTINATION share/${PROJECT_NAME})

ament_export_targets(export_runtime_log_recorder HAS_LIBRARY_TARGET)
ament_export_dependencies(${dependencies})

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  ament_add_gtest(test_recorder_controller test/test_recorder_controller.cpp)
  target_link_libraries(test_recorder_controller runtime_log_recorder)
  ament_target_dependencies(test_recorder_controller ${dependencies})
endif()

ament_package()
