import Foundation

struct Host: Codable, Identifiable {
    let id: String
    let hostname: String
    let ipAddress: String?
    let sshUser: String?
    let sshPort: Int?
    let osFamily: String?
    let osVersion: String?
    let status: HostStatus
    let totalUpdates: Int?
    let lastChecked: String?
    let rebootRequired: Bool?
    let ownerUsername: String?

    /// Why the last check marked this host unreachable. Two very different
    /// failures land on the same status: SSH genuinely failed, or SSH worked
    /// and fact gathering failed (e.g. an unaccepted Xcode licence breaking
    /// /usr/bin/python3). Nil when the last check succeeded.
    let checkFailReason: String?
    /// First failure in a run of consecutive failures, not the latest one.
    let checkFailAt: String?
    /// Server-derived fix for known recurring causes, so this app does not
    /// re-implement the backend's pattern matching.
    let checkFailHint: String?

    enum CodingKeys: String, CodingKey {
        case id, hostname, status
        case ipAddress = "ip_address"
        case sshUser = "ssh_user"
        case sshPort = "ssh_port"
        case osFamily = "os_family"
        case osVersion = "os_version"
        case totalUpdates = "total_updates"
        case lastChecked = "last_checked"
        case rebootRequired = "reboot_required"
        case ownerUsername = "owner_username"
        case checkFailReason = "check_fail_reason"
        case checkFailAt = "check_fail_at"
        case checkFailHint = "check_fail_hint"
    }
}

enum HostStatus: String, Codable {
    case upToDate = "up-to-date"
    case updatesAvailable = "updates-available"
    case unreachable = "unreachable"
    case pending = "pending"
    case checking = "checking"

    var displayName: String {
        switch self {
        case .upToDate: return "Up to Date"
        case .updatesAvailable: return "Updates Available"
        case .unreachable: return "Unreachable"
        case .pending: return "Pending"
        case .checking: return "Checking"
        }
    }
}

struct Package: Codable, Identifiable {
    var id: String { "\(hostId ?? "")-\(packageName)" }
    let hostId: String?
    let packageName: String       // API returns "package_name", not "name"
    let currentVersion: String?
    let availableVersion: String?
    let updateType: String?
    let packageId: String?        // MAS numeric ID or winget Package.Id for exclusion config

    // Convenience alias so views can use .name
    var name: String { packageName }

    enum CodingKeys: String, CodingKey {
        case hostId = "host_id"
        case packageName = "package_name"
        case currentVersion = "current_version"
        case availableVersion = "available_version"
        case updateType = "update_type"
        case packageId = "package_id"
    }
}
