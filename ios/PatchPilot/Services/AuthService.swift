import Foundation

/// Manages authentication state and session lifecycle
@MainActor
class AuthService: ObservableObject {

    static let shared = AuthService()

    @Published var currentUser: User?
    @Published var isAuthenticated = false
    @Published var isLoading = false

    private let api = APIClient.shared

    init() {
        // Check for existing session on launch
        if KeychainHelper.sessionToken != nil {
            isAuthenticated = true
        }
    }

    // MARK: - Login

    func login(username: String, password: String) async throws {
        isLoading = true
        defer { isLoading = false }

        let request = LoginRequest(username: username, password: password)
        let response: LoginResponse = try await api.post("/api/auth/login", body: request)

        // Store token securely if backend returns it (requires updated auth.py)
        if let token = response.token {
            KeychainHelper.sessionToken = token
        }
        currentUser = response.user
        isAuthenticated = true
    }

    // MARK: - Validate Session

    func validateSession() async {
        do {
            let response: MeResponse = try await api.get("/api/auth/me")
            if response.authenticated, let user = response.user {
                currentUser = user
                isAuthenticated = true
            } else {
                logout()
            }
        } catch {
            logout()
        }
    }

    // MARK: - Logout

    func logout() {
        // Try to notify server (fire-and-forget)
        if KeychainHelper.sessionToken != nil {
            Task {
                try? await api.postVoid("/api/auth/logout")
            }
        }

        KeychainHelper.sessionToken = nil
        currentUser = nil
        isAuthenticated = false
    }

    // MARK: - Change Password

    func changePassword(currentPassword: String, newPassword: String) async throws {
        struct ChangePasswordRequest: Encodable {
            let current_password: String
            let new_password: String
        }

        let request = ChangePasswordRequest(
            current_password: currentPassword,
            new_password: newPassword
        )

        let _: [String: String] = try await api.post("/api/auth/change-password", body: request)
    }
}
