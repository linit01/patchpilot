import Foundation

/// API calls for host management
@MainActor
class HostService: ObservableObject {

    @Published var hosts: [Host] = []
    @Published var isLoading = false

    private let api = APIClient.shared

    func fetchHosts() async throws {
        isLoading = true
        defer { isLoading = false }
        hosts = try await api.get("/api/hosts")
    }

    func fetchHost(hostname: String) async throws -> Host {
        try await api.get("/api/hosts/\(hostname)")
    }

    func fetchPackages(hostname: String) async throws -> [Package] {
        try await api.get("/api/hosts/\(hostname)/packages")
    }

    func checkAllHosts() async throws {
        let _: [String: String] = try await api.post("/api/check")
    }

    func checkHost(hostname: String) async throws {
        let _: [String: String] = try await api.post("/api/check/\(hostname)")
    }

    func patchHosts(hostnames: [String], becomePassword: String?) async throws {
        struct PatchRequest: Encodable {
            let hostnames: [String]
            let become_password: String?
        }
        struct PatchResponse: Decodable {
            let message: String
            let status: String
            let hosts: [String]
        }
        let request = PatchRequest(hostnames: hostnames, become_password: becomePassword)
        let _: PatchResponse = try await api.post("/api/patch", body: request)
    }

    func getPatchStatus() async throws -> [String: Any] {
        // Returns dynamic JSON, so decode as dictionary
        let data: [String: AnyCodable] = try await api.get("/api/patch/status")
        var result: [String: Any] = [:]
        for (key, value) in data {
            result[key] = value.value
        }
        return result
    }
}
