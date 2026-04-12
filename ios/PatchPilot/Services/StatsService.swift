import Foundation

/// API calls for dashboard statistics
@MainActor
class StatsService: ObservableObject {

    @Published var stats: DashboardStats?
    @Published var chartData: ChartData?
    @Published var sidebarStats: SidebarStats?
    @Published var isLoading = false

    private let api = APIClient.shared

    func fetchStats() async throws {
        isLoading = true
        defer { isLoading = false }
        stats = try await api.get("/api/stats")
    }

    func fetchChartData() async throws {
        chartData = try await api.get("/api/stats/charts")
    }

    func fetchSidebarStats() async throws {
        sidebarStats = try await api.get("/api/stats/sidebar")
    }

    func fetchAll() async throws {
        isLoading = true
        defer { isLoading = false }

        async let s: DashboardStats = api.get("/api/stats")
        async let c: ChartData = api.get("/api/stats/charts")
        async let sb: SidebarStats = api.get("/api/stats/sidebar")

        let (statsResult, chartResult, sidebarResult) = try await (s, c, sb)
        stats = statsResult
        chartData = chartResult
        sidebarStats = sidebarResult
    }
}
