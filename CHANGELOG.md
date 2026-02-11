# Changelog

All notable changes to PatchPilot will be documented in this file.

## [2.0.0] - 2026-02-11

### Added
- **Saved SSH Keys Library**: Store and reuse SSH keys across multiple hosts
  - Encrypted storage with AES-256
  - Default key support
  - File upload interface
  - Edit/Delete functionality
- **Real-Time WebSocket Patching Progress**: Live streaming of Ansible task output
  - Progress modal with timestamps
  - Auto-close on completion
  - Connection management
- **Single-Host Check API**: Fast targeted checks (~30 seconds)
  - `/api/check/{hostname}` endpoint
  - Auto-triggers on host creation
- **Auto-Reboot Management**: Per-host configurable automatic reboots
  - Database tracking of reboot requirements
  - Control node protection (never auto-reboots)
  - Conditional execution based on host settings
- **Auto-Check Countdown Timer**: Visual feedback showing next scheduled check
  - 2-minute countdown display
  - Resets on manual refresh
- **SSH Key File Upload**: Upload keys instead of copy/paste
  - Validation of key format
  - Success/error feedback
- **Smart Refresh Polling**: 3-minute timeout with 5-second updates
  - Button stays disabled during poll
  - Prevents multiple concurrent checks
- **macOS System Update Detection**: Apple OS updates via `softwareupdate`
- **App Store Update Detection**: Mac App Store (mas) updates

### Changed
- **Background Check Interval**: Reduced from 5 minutes to 2 minutes
- **SSH ControlMaster**: Disabled to prevent connection pooling conflicts
- **Cache-Busting**: Added cache headers and query params for real-time updates
- **Ubuntu Phased Updates**: Force install deferred packages with `APT::Get::Always-Include-Phased-Updates=true`
- **Debug Logging**: Converted print statements to `logger.debug()` for production-ready logging

### Fixed
- **macOS Package Detection**: Fixed packages showing 'apt' instead of 'brew' label
  - Root cause: `data.get("update_type")` instead of `package.get("update_type")` in app.py
- **SSH Key Encryption**: Fixed BYTEA vs TEXT column type mismatch in saved_ssh_keys table
- **Package Parsing**: Handle complex version formats with epochs, tildes, commas
- **Control Node Detection**: Added warning badge and confirmation dialogs
- **Ansible Output Streaming**: Real-time progress with selective filtering
- **Browser Caching**: Fixed NULL status display issues with cache-busting
- **Logging Errors**: Added missing `logging` imports to app.py and database.py
- **Inventory Configuration**: Removed `ansible_connection=local` for control node

### Security
- All SSH keys encrypted with AES-256 (Fernet)
- Temporary key files use 0600 permissions
- Keys cleaned up after use
- No credentials logged or exposed in UI

**macOS Update Types Now Supported:**
- `brew`: Homebrew packages
- `macos-system`: Apple OS system updates
- `mas`: Mac App Store applications

## [1.0.0] - 2026-01-15

### Added
- Initial release
- Multi-platform support (Debian/Ubuntu, macOS, RHEL)
- Basic host management
- Package-level update details
- Encrypted SSH credential storage
- Dashboard with statistics
- Settings interface

---

**Legend:**
- **Added**: New features
- **Changed**: Changes in existing functionality
- **Deprecated**: Soon-to-be removed features
- **Removed**: Removed features
- **Fixed**: Bug fixes
- **Security**: Security improvements
