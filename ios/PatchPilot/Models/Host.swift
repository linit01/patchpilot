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
    var id: String { "\(hostId ?? "")-\(name)" }
    let hostId: String?
    let name: String
    let currentVersion: String?
    let availableVersion: String?
    let updateType: String?

    enum CodingKeys: String, CodingKey {
        case name
        case hostId = "host_id"
        case currentVersion = "current_version"
        case availableVersion = "available_version"
        case updateType = "update_type"
    }
}
