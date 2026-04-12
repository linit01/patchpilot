import Foundation

struct PatchHistoryRecord: Codable, Identifiable {
    let id: String
    let hostId: String?
    let hostname: String?
    let status: String
    let packagesUpdated: Int?
    let executionTime: AnyCodable?  // Can be int (duration) or string
    let durationSeconds: Int?
    let errorMessage: String?
    let output: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, hostname, status, output
        case hostId = "host_id"
        case packagesUpdated = "packages_updated"
        case executionTime = "execution_time"
        case durationSeconds = "duration_seconds"
        case errorMessage = "error_message"
        case createdAt = "created_at"
    }

    var isSuccess: Bool {
        status == "success"
    }

    var durationDisplay: String {
        if let secs = durationSeconds, secs > 0 {
            let minutes = secs / 60
            let remaining = secs % 60
            if minutes > 0 {
                return "\(minutes)m \(remaining)s"
            }
            return "\(remaining)s"
        }
        return "—"
    }
}

/// Type-erased Codable to handle polymorphic JSON values
struct AnyCodable: Codable {
    let value: Any

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let intVal = try? container.decode(Int.self) {
            value = intVal
        } else if let stringVal = try? container.decode(String.self) {
            value = stringVal
        } else if let doubleVal = try? container.decode(Double.self) {
            value = doubleVal
        } else {
            value = 0
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        if let intVal = value as? Int {
            try container.encode(intVal)
        } else if let stringVal = value as? String {
            try container.encode(stringVal)
        } else if let doubleVal = value as? Double {
            try container.encode(doubleVal)
        }
    }
}
