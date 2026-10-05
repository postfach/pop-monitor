cmake_minimum_required(VERSION 3.16)
project(pop_monitor C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()
add_compile_options(-Wall -Wextra -Wno-unused-parameter)

option(POP_BUILD_GATEWAY "Build the gateway service (needs FFmpeg, libcurl, libturbojpeg, cJSON, OpenSSL)" ON)
option(POP_BUILD_TOOLS   "Build popreplay (needs FFmpeg)" ON)

# ---- detection core (no dependencies) ----
add_library(popcore STATIC
  core/src/warp.c core/src/features.c core/src/detector.c
  core/src/faults.c core/src/score.c)
target_include_directories(popcore PUBLIC core/include)
target_link_libraries(popcore PUBLIC m)

enable_testing()
add_executable(test_core core/tests/test_core.c)
target_link_libraries(test_core popcore)
target_compile_options(test_core PRIVATE -Wno-missing-field-initializers)
add_test(NAME popcore COMMAND test_core)

find_package(PkgConfig)

if(POP_BUILD_GATEWAY)
  pkg_check_modules(GW REQUIRED IMPORTED_TARGET
    libavformat libavcodec libavutil libswscale libcurl libturbojpeg libcjson openssl)
  find_package(Threads REQUIRED)
  add_executable(pop-gateway
    gateway/src/main.c gateway/src/config.c gateway/src/camera.c gateway/src/stream.c
    gateway/src/vapix.c gateway/src/jpeg.c gateway/src/queue.c gateway/src/uploader.c
    gateway/src/alerts.c gateway/src/util.c gateway/src/control.c)
  target_link_libraries(pop-gateway popcore PkgConfig::GW Threads::Threads)
  install(TARGETS pop-gateway RUNTIME DESTINATION bin)
endif()

if(POP_BUILD_TOOLS)
  pkg_check_modules(AV REQUIRED IMPORTED_TARGET libavformat libavcodec libavutil libswscale)
  find_package(Threads REQUIRED)
  add_executable(popreplay tools/popreplay.c gateway/src/stream.c gateway/src/util.c)
  target_include_directories(popreplay PRIVATE gateway/src)
  target_link_libraries(popreplay popcore PkgConfig::AV Threads::Threads)
  install(TARGETS popreplay RUNTIME DESTINATION bin)
endif()
