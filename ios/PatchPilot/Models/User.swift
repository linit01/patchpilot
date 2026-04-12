import Foundation

struct User: Codable, Identifiable {
    let id: String
    let username: String
    let email: String?
    let role: UserRole
}

enum UserRole: String, Codable {
    case fullAdmin = "full_admin"
    case admin = "admin"
    case viewer = "viewer"

    var displayName: String {
        switch self {
        case .fullAdmin: return "Full Admin"
        case .admin: return "Admin"
        case .viewer: return "Viewer"
        }
    }

    var canWrite: Bool {
        self != .viewer
    }

    var isFullAdmin: Bool {
        self == .fullAdmin
    }
}

struct LoginRequest: Encodable {
    let username: String
    let password: String
}

struct LoginResponse: Decodable {
    let message: String
    let token: String?   // nil if backend not yet updated; session cookie auth still works
    let user: User
}

/// Envelope returned by GET /api/auth/me
struct MeResponse: Decodable {
    let authenticated: Bool
    let user: User?
}
