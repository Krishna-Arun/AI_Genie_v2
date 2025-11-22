AI_Genie_v2 – Code Structure Overview

Generated at: 2025-11-22 04:05:35Z
Commit: 65013f1

Purpose
- High-level map of the repository to help you navigate quickly.
- Descriptions focus on what lives in each top-level directory and common entry points.

Top-level Directory Guide
- 3rdParty: Third-party dependency build helpers and custom vcpkg ports.
- android: Android client sources, build scripts, and CI for mobile.
- api: BOINC application-side API (C/C++), OpenCL bindings, graphics helpers.
- apps: Small sample applications used for examples and tests.
- ci_tools: Scripts used by CI to validate and maintain the codebase.
- client: Core client/daemon that runs tasks and manages scheduling on hosts.
- clientctrl: Service control utilities (primarily Windows service control shim).
- clientgui: GUI Manager (wxWidgets-based) for desktop interaction.
- clientscr: Screensaver sources and assets.
- clientsetup: Windows client setup/installer integration layer.
- clienttray: Windows tray application.
- coprocs: Co-processor related headers/libraries (e.g., NVIDIA).
- curl: Certificates or curl-related assets used by the project.
- db: Database schemas and server DB-related utilities.
- deploy: Deployment helper scripts.
- doc: Docs and developer notes.
- drupal: Drupal-based legacy portal code used by some deployments.
- fastlane: Mobile store metadata and assets (Android/iOS).
- html: PHP-based project website/server (user portal, ops pages, APIs).
- installer: Windows MSI installer generator sources (C++).
- lib: Shared libraries, utilities, and platform abstractions used across components.
- linux: Linux-specific helper scripts.
- locale: Translations (.po/.mo) and localization assets.
- m4: Autotools macros.
- mac_build: Xcode schemes and Mac build helpers.
- mac_installer: Mac installer resources and scripts.
- mingw: MinGW build helpers.
- osx: macOS build scripts.
- packages: Packaging metadata and man pages.
- py: Python utilities and scripts.
- release_tools: Release engineering tools and scripts.
- samples: Example programs (OpenGL/CL demos, wrappers, integrations).
- sched: Server-side scheduler and work generation/validation components.
- snap: Snapcraft packaging for Linux.
- stripchart: System/performance monitoring helper tools.
- tests: Unit, integration, and server tests.
- tools: Admin/ops/CLI tools (submission, project maintenance, etc.).
- vda: Video/data analysis utilities (project-specific tools).
- win_build: Visual Studio solutions and Windows build configuration.
- windows: Windows build/batch scripts.
- xcompile: Cross-compile helpers.
- zip: Zip library and related build files.

Common Entry Points
- Client (daemon): client/ (C/C++ sources for the BOINC core client).
- GUI Manager: clientgui/ (wxWidgets application).
- Android app: android/BOINC/ (Kotlin/Java) and android build scripts.
- Server website/API: html/ (PHP), sched/ (C++ server components).
- App API for projects: api/ (headers and libs linked by BOINC apps).
- Windows installers: installer/ (MSI generator), clientsetup/ and win_build/.
- macOS build/installer: mac_build/ and mac_installer/.

Build and Tooling (high-level)
- Autotools: configure.ac, m4/, Makefile.am files across modules.
- CMake/Vcpkg: 3rdParty/vcpkg_ports and tests/vcpkg; Visual Studio projects in win_build/.
- Android: android/ scripts for NDK/Gradle; fastlane/ store assets.
- Snapcraft: snap/snapcraft.yaml for snap packaging.

Directory Tree (one level)
```text
3rdParty/
android/
api/
apps/
ci_tools/
client/
clientctrl/
clientgui/
clientscr/
clientsetup/
clienttray/
coprocs/
curl/
db/
deploy/
doc/
drupal/
fastlane/
html/
installer/
lib/
linux/
locale/
m4/
mac_build/
mac_installer/
mingw/
osx/
packages/
py/
release_tools/
samples/
sched/
snap/
stripchart/
tests/
tools/
vda/
win_build/
windows/
xcompile/
zip/
```

Notes
- The html/ and sched/ trees together form the server-side of a BOINC project.
- The client/ and clientgui/ trees form the BOINC client runtime and desktop UI.
- The api/ tree is linked by science apps to talk to the BOINC runtime.
- Platform-specific packaging/build folders (win_build/, mac_*, linux/, snap/) provide OS distribution paths.


