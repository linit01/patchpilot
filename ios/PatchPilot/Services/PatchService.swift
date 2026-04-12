import Foundation

/// API calls for patch history
@MainActor
class PatchService: ObservableObject {

    @Published var history: [PatchHistoryRecord] = []
    @Published var isLoading = false

    private let api = APIClient.shared

    func fetchHistory(limit: Int = 50) async throws {
        isLoading = true
        defer { isLoading = false }
        history = try await api.get("/api/patch-history?limit=\(limit)")
    }

    func fetchHostHistory(hostId: String, limit: Int = 20) async throws -> [PatchHistoryRecord] {
        try await api.get("/api/patch-history/host/\(hostId)?limit=\(limit)")
    }
}
