import Foundation

struct DashboardStats: Codable {
    let totalHosts: Int
    let upToDate: Int
    let needUpdates: Int
    let unreachable: Int
    let totalPendingUpdates: Int

    enum CodingKeys: String, CodingKey {
        case totalHosts = "total_hosts"
        case upToDate = "up_to_date"
        case needUpdates = "need_updates"
        case unreachable
        case totalPendingUpdates = "total_pending_updates"
    }
}

struct ChartData: Codable {
    let osDistribution: [OSDistribution]
    let updateTypes: [UpdateType]
    let patchActivity: [PatchActivity]

    enum CodingKeys: String, CodingKey {
        case osDistribution = "os_distribution"
        case updateTypes = "update_types"
        case patchActivity = "patch_activity"
    }
}

struct OSDistribution: Codable, Identifiable {
    var id: String { os }
    let os: String
    let count: Int
}

struct UpdateType: Codable, Identifiable {
    var id: String { type }
    let type: String
    let count: Int
}

struct PatchActivity: Codable, Identifiable {
    var id: String { day }
    let day: String
    let patched: Int
    let failed: Int
    let byOs: [String: Int]?

    enum CodingKeys: String, CodingKey {
        case day, patched, failed
        case byOs = "by_os"
    }
}

/// Flattened row used for per-OS stacked chart rendering
struct PatchActivityOSEntry: Identifiable {
    var id: String { "\(day)-\(os)" }
    let day: String
    let os: String
    let count: Int
}

struct SidebarStats: Codable {
    let load1: Double
    let load5: Double
    let load15: Double
    let uptime: String
    let hostCount: Int
    let packageCount: Int
    let historyCount: Int
    let alertCount: Int

    enum CodingKeys: String, CodingKey {
        case load1 = "load_1"
        case load5 = "load_5"
        case load15 = "load_15"
        case uptime
        case hostCount = "host_count"
        case packageCount = "package_count"
        case historyCount = "history_count"
        case alertCount = "alert_count"
    }
}
