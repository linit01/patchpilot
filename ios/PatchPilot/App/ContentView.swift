import SwiftUI

/// Root view: routes between server config, login, and main app
struct ContentView: View {
    @EnvironmentObject var apiClient: APIClient
    @EnvironmentObject var authService: AuthService

    var body: some View {
        Group {
            if !apiClient.isConfigured {
                ServerConfigView()
            } else if !authService.isAuthenticated {
                LoginView()
            } else {
                MainTabView()
            }
        }
        .preferredColorScheme(.dark)
        .task {
            // Validate existing session on launch
            if apiClient.isConfigured && authService.isAuthenticated {
                await authService.validateSession()
            }
        }
    }
}

/// Main app with bottom tab bar
struct MainTabView: View {
    @EnvironmentObject var authService: AuthService

    var body: some View {
        TabView {
            DashboardView()
                .tabItem {
                    Label("Dashboard", systemImage: "chart.bar.fill")
                }

            HostListView()
                .tabItem {
                    Label("Hosts", systemImage: "desktopcomputer")
                }

            PatchHistoryView()
                .tabItem {
                    Label("History", systemImage: "clock.fill")
                }

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gearshape.fill")
                }
        }
        .tint(Theme.cyan)
    }
}
