import SwiftUI

/// Searchable list of all managed hosts
struct HostListView: View {
    @StateObject private var hostService = HostService()
    @EnvironmentObject var authService: AuthService
    @State private var searchText = ""
    @State private var selectedHosts: Set<String> = []
    @State private var showPatchSheet = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            List {
                if hostService.isLoading && hostService.hosts.isEmpty {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                        .listRowBackground(Theme.bgBlack)
                } else if filteredHosts.isEmpty {
                    Text(searchText.isEmpty ? "No hosts configured" : "No matching hosts")
                        .foregroundColor(Theme.textMuted)
                        .listRowBackground(Theme.bgBlack)
                } else {
                    ForEach(filteredHosts) { host in
                        NavigationLink(value: host) {
                            HostRow(host: host)
                        }
                        .listRowBackground(Theme.bgPanel)
                    }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(Theme.bgBlack)
            .searchable(text: $searchText, prompt: "Search hosts...")
            .navigationTitle("Hosts")
            .navigationDestination(for: Host.self) { host in
                HostDetailView(host: host)
            }
            .toolbar {
                if authService.currentUser?.role.canWrite == true && !hostService.hosts.isEmpty {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Patch All") {
                            showPatchSheet = true
                        }
                        .foregroundColor(Theme.cyan)
                    }
                }
            }
            .refreshable {
                do {
                    try await hostService.fetchHosts()
                } catch {
                    errorMessage = error.localizedDescription
                }
            }
            .task {
                do {
                    try await hostService.fetchHosts()
                } catch {
                    errorMessage = error.localizedDescription
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
                Task {
                    try? await hostService.fetchHosts()
                }
            }
            .sheet(isPresented: $showPatchSheet) {
                PatchConfirmSheet(
                    allHosts: hostService.hosts,
                    onDismiss: { showPatchSheet = false }
                )
            }
        }
    }

    private var filteredHosts: [Host] {
        if searchText.isEmpty { return hostService.hosts }
        let query = searchText.lowercased()
        return hostService.hosts.filter {
            $0.hostname.lowercased().contains(query) ||
            ($0.ipAddress?.lowercased().contains(query) ?? false) ||
            ($0.osFamily?.lowercased().contains(query) ?? false)
        }
    }
}

extension Host: Hashable {
    static func == (lhs: Host, rhs: Host) -> Bool { lhs.id == rhs.id }
    func hash(into hasher: inout Hasher) { hasher.combine(id) }
}
